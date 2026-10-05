"""Kiln - Aether Forge's coding agent.

You talk to it from the phone (Chat > Kiln). It builds in its own sandbox
(see forge.py): writes the files, runs them, reads the errors, fixes them,
and shows you the result - a web page right on your phone, a script's
output, anything.

It has its own model, set in the PC app (Settings > Chatbox > Kiln): local
(Ollama) by default, or Anthropic / OpenAI / OpenRouter with a key. Coding is
the hardest thing you can ask a model to do, so a bigger model does much
better here - local models are fine for pages, scripts and small tools.

The key stays on this PC and is never put inside the sandbox - the code
Kiln runs can't read it.

A job keeps running if the phone locks or loses signal; the phone picks the
live feed back up (every event is kept, numbered) when it reconnects.
"""
import json
import os
import re
import secrets
import threading
import time

import chat
import forge
import memory
import paths
import skills

CONF = paths.data("kiln.json")
MAX_ROUNDS = 30

DEFAULT_SYSTEM = """You are Kiln, the coding agent of Aether Forge. You build things for the user inside an \
isolated Linux sandbox (Debian with Python 3, pip, Node.js, npm, git and curl; internet access for installing \
packages; no access to the user's PC or local network).

The project folder is /work and every path you use is relative to it. Build what the user asks, end to end:
1. Look at what's already in the project when it matters (list_files, read_file).
2. Write the files (write_file for new or rewritten files, edit_file for small changes to an existing file).
3. Run it to check it works (run) - read the errors and fix them. Install what you need (pip install, npm install).
4. Finish with a SHORT plain-language summary: what you made, how to use it. No code dumps - the user can open \
the files.

Rules:
- Commands must finish: never start servers, watchers or anything interactive (they're killed after the timeout).
- Web pages: make index.html (self-contained, or with local .css/.js files next to it). It must look good and \
work on a phone screen. The user previews it on their phone.
- Keep the project tidy: sensible file names, no stray test files left behind.
- Don't ask permission for things inside the sandbox - just do them. Ask the user only when what they want is \
genuinely unclear.
- The user is on a phone: keep messages brief."""

STATE_FOR = {"write_file": "forge", "edit_file": "forge", "delete_path": "forge", "run": "exec",
             "read_file": "think", "list_files": "think"}

_lock = threading.Lock()
JOBS = {}
LOG = lambda msg: None          # remote.py points this at its log
PREVIEW = {}            # token -> project id (capability URLs for the sandboxed preview)


# --------------------------------------------------------------- settings

def _load():
    try:
        with open(CONF, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(c):
    tmp = CONF + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f)
    os.replace(tmp, CONF)


def config():
    c = _load()
    c.setdefault("enabled", False)
    c.setdefault("provider", "ollama")
    c.setdefault("base_url", "")
    c.setdefault("model", "gemma4:12b")
    c.setdefault("api_key", "")
    c.setdefault("system", "")
    c.setdefault("root", "")
    c.setdefault("export_dir", "")
    c.setdefault("learn", True)
    return c


def export_dir(c=None):
    c = c or config()
    d = (c.get("export_dir") or "").strip()
    return d or os.path.join(os.path.expanduser("~"), "Documents", "Aether Forge")


def ready(c=None):
    c = c or config()
    p = chat.PROVIDERS.get(c["provider"])
    return bool(p and c["model"] and chat._base(c) and (c["api_key"] or not p["key"]))


def public(local=False):
    c = config()
    st = forge.status(sizes=local, live=local)
    out = {"enabled": c["enabled"], "ready": bool(c["enabled"] and ready(c) and st["installed"]),
           "modelReady": ready(c), "provider": c["provider"],
           "providerName": chat.PROVIDERS.get(c["provider"], {}).get("name", ""), "model": c["model"],
           "sandbox": st, "skills": skills.count("kiln")}
    if local:
        k = c["api_key"]
        out.update(base_url=c["base_url"], system=c["system"], defaultSystem=DEFAULT_SYSTEM,
                   hasKey=bool(k), keyHint=("…" + k[-4:]) if len(k) >= 8 else ("set" if k else ""),
                   providers=chat.PROVIDERS, exportDir=export_dir(c), wsl=None, log=forge._st["log"][-12:],
                   learn=c["learn"], skillCount=skills.count("kiln"), skillsDir=skills.ROOT)
    return out


def update(b):
    c = config()
    if b.get("provider") in chat.PROVIDERS:
        if b["provider"] != c["provider"] and "api_key" not in b:
            c["api_key"] = ""
        c["provider"] = b["provider"]
    if "base_url" in b:
        u = str(b.get("base_url") or "").strip().rstrip("/")
        if u and not re.match(r"^https?://[^\s/]+(/\S*)?$", u):
            raise ValueError("That address doesn't look right (it should start with https://)")
        c["base_url"] = u[:300]
    if "model" in b:
        c["model"] = str(b.get("model") or "").strip()[:120]
    if "api_key" in b:
        c["api_key"] = str(b.get("api_key") or "").strip()[:400]
    if "system" in b:
        c["system"] = str(b.get("system") or "")[:6000]
    if "export_dir" in b:
        d = str(b.get("export_dir") or "").strip()
        if d and not os.path.isabs(d):
            raise ValueError("The export folder must be a full path, like C:\\Users\\you\\Documents\\Forge")
        c["export_dir"] = d[:400]
    if "root" in b:
        r = str(b.get("root") or "").strip()
        if r != c.get("root", ""):
            if forge.status()["installed"]:
                raise ValueError("Remove Forge first to move where it lives")
            if r and not os.path.isabs(r):
                raise ValueError("Use a full path, like E:\\Aether Forge")
            c["root"] = r[:400]
    if "learn" in b:
        c["learn"] = bool(b["learn"])
    if "enabled" in b:
        c["enabled"] = bool(b["enabled"])
    with _lock:
        _save(c)
    return public(local=True)


def list_models(b=None):
    return chat.list_models(b, base_conf=config())


def phone_models():
    return chat.phone_models(config())


def pick_model(mid):
    mid = str(mid or "").strip()
    c = config()
    if not any(m["id"] == mid for m in chat.phone_models(c)):
        chat._pm["at"] = 0
        if not any(m["id"] == mid for m in chat.phone_models(c)):
            raise ValueError("That model isn't available")
    with _lock:
        c = config()
        c["model"] = mid
        _save(c)
    return public()


def test():
    c = dict(config(), max_tokens=60)
    t0 = time.time()
    text, _, _ = (chat._anthropic_turn if c["provider"] == "anthropic" else chat._openai_turn)(
        c, "Reply with one short sentence.", [{"role": "user", "content": "Say hi to the user from Kiln."}], [], lambda e: None)
    return {"ok": True, "reply": text.strip()[:200], "ms": int((time.time() - t0) * 1000)}


# --------------------------------------------------------------- projects as the phone sees them

def preview_token(pid):
    for t, p in PREVIEW.items():
        if p == pid:
            return t
    t = secrets.token_urlsafe(18)
    PREVIEW[t] = pid
    return t


def preview_file(token, path):
    pid = PREVIEW.get(token)
    if not pid:
        raise ValueError("That preview has expired")
    p = forge.safe(pid, path or "index.html")
    if os.path.isdir(p):
        p = os.path.join(p, "index.html")
    if not os.path.isfile(p):
        raise ValueError("No such file")
    return p


def _entry(pid):
    files, more = forge.tree(pid)
    pages = [f["path"] for f in files if f["path"].lower().endswith((".html", ".htm"))]
    pages.sort(key=lambda p: (p.count("/"), p.lower() != "index.html", p))
    return files, more, pages


def project(pid):
    m = forge.meta(pid)
    if not m:
        raise ValueError("No such project")
    files, more, pages = _entry(pid)
    j = JOBS.get(pid)
    return {"id": pid, "name": m["name"], "msgs": m.get("msgs", [])[-60:], "files": files, "moreFiles": more,
            "pages": pages, "preview": "/kp/%s/" % preview_token(pid),
            "job": j.info() if j else None}


# --------------------------------------------------------------- the agent's tools

def _defs(learn=False):
    extra = [
        ("read_skill", "Read one of your learned skills (a how-to from an earlier task) before doing a matching task.",
         {"name": {"type": "string"}}, ["name"]),
        ("search_history", "Search earlier Kiln projects and chats for something done before (returns short snippets).",
         {"query": {"type": "string"}}, ["query"]),
    ] if learn else []
    return extra + [
        ("list_files", "List the files in the project.", {}, []),
        ("read_file", "Read a text file from the project.",
         {"path": {"type": "string", "description": "path relative to /work"}}, ["path"]),
        ("write_file", "Create a file, or replace a file's whole content. Makes folders as needed.",
         {"path": {"type": "string"}, "content": {"type": "string", "description": "the complete file content"}},
         ["path", "content"]),
        ("edit_file", "Change part of an existing file: replaces old_text (which must appear exactly once) "
         "with new_text. Read the file first.",
         {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
         ["path", "old_text", "new_text"]),
        ("delete_path", "Delete a file or folder in the project.", {"path": {"type": "string"}}, ["path"]),
        ("run", "Run a bash command in /work (Debian: python3, pip, node, npm, git, curl). Returns the output "
         "and exit code. Must finish on its own - no servers or interactive programs.",
         {"command": {"type": "string"},
          "timeout": {"type": "integer", "description": "seconds, default 120, max 600"}}, ["command"]),
    ]


def _label(name, a):
    p = str(a.get("path", ""))[:60]
    return {"list_files": "Looking at the files",
            "read_file": "Reading %s" % p,
            "write_file": "Writing %s" % p,
            "edit_file": "Editing %s" % p,
            "delete_path": "Deleting %s" % p,
            "run": "$ %s" % re.sub(r"\s+", " ", str(a.get("command", "")))[:90],
            "read_skill": "Using what it learned: %s" % str(a.get("name", ""))[:50],
            "search_history": "Looking back for “%s”" % str(a.get("query", ""))[:50]}.get(name, name)


class Job:
    """One request to Kiln: runs in the background; the phone follows its
    numbered events and can reconnect at any point."""

    def __init__(self, pid, text):
        self.pid, self.text = pid, text
        self.events, self.cv = [], threading.Condition()
        self.cancel = threading.Event()
        self.done, self.state = False, "think"
        self.started = time.time()
        self.used_skills = set()

    def info(self):
        return {"running": not self.done, "seq": len(self.events), "state": self.state, "started": self.started}

    def emit(self, ev):
        with self.cv:
            if ev.get("t") == "state":
                self.state = ev["s"]
            ev["n"] = len(self.events)
            self.events.append(ev)
            self.cv.notify_all()

    def wait(self, since, timeout=15):
        with self.cv:
            if len(self.events) <= since and not self.done:
                self.cv.wait(timeout)
            return self.events[since:], self.done


def send(pid, text):
    text = str(text or "").strip()[:12000]
    if not text:
        raise ValueError("Type what you want Kiln to build")
    c = config()
    if not (c["enabled"] and ready(c)):
        raise ValueError("Kiln isn't set up yet - PC app > Settings > Chatbox > Kiln.")
    if not forge.meta(pid):
        raise ValueError("No such project")
    with _lock:
        j = JOBS.get(pid)
        if j and not j.done:
            raise ValueError("Kiln is still working on this project - stop it first")
        j = JOBS[pid] = Job(pid, text)
    threading.Thread(target=_work, args=(j, c), daemon=True).start()
    return j.info()


def stop(pid):
    j = JOBS.get(pid)
    if j and not j.done:
        j.cancel.set()
        return True
    return False


def _store(pid, role, text, steps=None):
    m = forge.meta(pid)
    if not m:
        return
    rec = {"role": role, "text": text, "ts": time.time()}
    if steps is not None:
        rec["steps"] = steps
    m.setdefault("msgs", []).append(rec)
    m["msgs"] = m["msgs"][-120:]
    m["updated"] = time.time()
    forge.save_meta(m)


def _context(pid):
    files, more, _ = _entry(pid)
    if not files:
        return "The project is empty."
    lines = ["%s%s" % (f["path"], "" if f.get("dir") else " (%d bytes)" % f.get("size", 0)) for f in files[:150]]
    return "Files in the project:\n" + "\n".join(lines) + ("\n…and more" if more or len(files) > 150 else "")


def _trim(msgs, budget):
    """Keep the conversation inside the model's memory: older tool results
    and file contents are cut down first, the newest kept whole."""
    def size():
        return sum(len(json.dumps(m)) for m in msgs)
    if size() <= budget:
        return
    for m in msgs[:-6]:
        if size() <= budget:
            return
        if m.get("role") == "tool" and len(m.get("content") or "") > 300:
            m["content"] = m["content"][:200] + "…[trimmed]"
        if isinstance(m.get("content"), list):
            for b in m["content"]:
                if b.get("type") == "tool_result" and len(b.get("content") or "") > 300:
                    b["content"] = b["content"][:200] + "…[trimmed]"
                if b.get("type") == "tool_use" and len(json.dumps(b.get("input"))) > 400:
                    b["input"] = {k: (v[:150] + "…[trimmed]" if isinstance(v, str) and len(v) > 200 else v)
                                  for k, v in b["input"].items()}
        for tc in m.get("tool_calls") or []:
            f = tc.get("function") or {}
            if len(f.get("arguments") or "") > 400:
                try:
                    a = json.loads(f["arguments"])
                    a = {k: (v[:150] + "…[trimmed]" if isinstance(v, str) and len(v) > 200 else v) for k, v in a.items()}
                    f["arguments"] = json.dumps(a)
                except Exception:
                    f["arguments"] = "{}"


def _tool(j, name, a, steps):
    pid = j.pid
    if name == "list_files":
        files, more, _ = _entry(pid)
        return {"files": [f["path"] + ("" if f.get("dir") else "  %dB" % f.get("size", 0)) for f in files],
                "truncated": more}
    if name == "read_file":
        text, cut = forge.read(pid, a.get("path", ""), 60000)
        return {"content": text, "truncated": cut}
    if name == "write_file":
        content = a.get("content")
        if not isinstance(content, str):
            return {"error": "content must be the file's text"}
        n = forge.write(pid, a.get("path", ""), content)
        j.emit({"t": "files", "path": forge.rel(pid, forge.safe(pid, a.get("path", "")))})
        return {"ok": True, "bytes": n}
    if name == "edit_file":
        path = a.get("path", "")
        text, cut = forge.read(pid, path, 2000000)
        old, new = a.get("old_text", ""), a.get("new_text", "")
        if not old:
            return {"error": "old_text is empty"}
        k = text.count(old)
        if k == 0:
            return {"error": "old_text wasn't found in %s - read the file and copy the text exactly" % path}
        if k > 1:
            return {"error": "old_text appears %d times in %s - include more surrounding text so it's unique" % (k, path)}
        forge.write(pid, path, text.replace(old, new, 1))
        j.emit({"t": "files", "path": forge.rel(pid, forge.safe(pid, path))})
        return {"ok": True}
    if name == "delete_path":
        forge.remove(pid, a.get("path", ""))
        j.emit({"t": "files"})
        return {"ok": True}
    if name == "run":
        cmd = str(a.get("command", "")).strip()
        if not cmd:
            return {"error": "No command"}
        last = {"t": 0, "buf": ""}

        def out(t):
            last["buf"] += t
            if time.time() - last["t"] > 0.4:
                j.emit({"t": "out", "d": last["buf"][-4000:]})
                last["buf"], last["t"] = "", time.time()
        code, text = forge.run(pid, cmd, a.get("timeout") or 120, out, j.cancel)
        if last["buf"]:
            j.emit({"t": "out", "d": last["buf"][-4000:]})
        j.emit({"t": "files"})
        tail = text if len(text) <= 12000 else "…[%d characters cut]…\n%s" % (len(text) - 12000, text[-12000:])
        res = {"exit_code": code, "output": tail}
        if code == 124:
            res["note"] = "It was stopped because it didn't finish in time (don't start servers or interactive programs)."
        return res
    if name == "read_skill":
        s = skills.find("kiln", a.get("name", ""))
        if not s:
            return {"error": "No skill by that name", "skills": [x["name"] for x in skills.listing("kiln")]}
        skills.used("kiln", s["name"])
        j.used_skills.add(s["name"])
        return {"name": s["name"], "skill": s["body"]}
    if name == "search_history":
        return {"results": skills.search_history(a.get("query", ""))}
    return {"error": "Unknown tool"}


def _work(j, c):
    pid = j.pid
    forge.touch(+1)
    steps, reply = [], ""
    _store(pid, "user", j.text)
    j.emit({"t": "user", "d": j.text})
    try:
        j.emit({"t": "state", "s": "think", "label": "Starting the sandbox"})
        forge.ensure(pid)
        c = dict(c)
        local = c["provider"] in ("ollama", "custom")
        c["max_tokens"] = 8192 if local else 16000
        c["timeout"] = 300
        budget = 36000 if local else 300000
        m = forge.meta(pid) or {}
        prev = [x for x in m.get("msgs", [])[:-1] if x.get("text")][-12:]
        msgs = [{"role": x["role"], "content": x["text"]} for x in prev]
        msgs.append({"role": "user", "content": j.text})
        system = (c.get("system") or "").strip() or DEFAULT_SYSTEM
        learn = c.get("learn", True)
        if learn:
            known = memory.prompt(limit=20, chars=1500)
            if known:
                system += "\n\nWhat you know about the user (use it when it matters - their taste, their setup):\n" + known
            sk = skills.prompt("kiln")
            if sk:
                system += "\n\n" + sk
        system += "\n\nProject: %s\n%s" % (m.get("name", pid), _context(pid))
        defs = _defs(learn)
        anthropic = c["provider"] == "anthropic"
        turn = chat._anthropic_turn if anthropic else chat._openai_turn

        def text_out(ev):
            if j.cancel.is_set():
                raise InterruptedError()
            if ev.get("t") == "text":
                j.emit(ev)
        for rnd in range(MAX_ROUNDS + 1):
            if j.cancel.is_set():
                raise InterruptedError()
            j.emit({"t": "state", "s": "think"})
            _trim(msgs, budget)
            part, calls, msg = turn(c, system, msgs, defs if rnd < MAX_ROUNDS else [], text_out)
            if not calls and not part.strip() and not reply.strip():
                # Local models now and then hand back nothing at all. Ask
                # again, the second time with a nudge to get on with it.
                for k, wait in enumerate((1, 3)):
                    LOG("kiln: empty answer from %s (%s) - retrying" % (c["model"], json.dumps(chat.LAST)))
                    if j.cancel.is_set():
                        raise InterruptedError()
                    time.sleep(wait)
                    nudged = msgs if k == 0 else msgs + [{"role": "user", "content": "Go ahead - use the tools to build it now."}]
                    part, calls, msg = turn(c, system, nudged, defs, text_out)
                    if calls or part.strip():
                        if k:
                            msgs.append(nudged[-1])
                        break
                else:
                    LOG("kiln: still no answer (%s)" % json.dumps(chat.LAST))
                    raise RuntimeError("The model didn't answer. Try again - or pick a bigger model if it keeps happening.")
            reply += part
            if not calls:
                break
            msgs.append(msg)
            results = []
            for cid, name, args in calls:
                if j.cancel.is_set():
                    raise InterruptedError()
                args = args if isinstance(args, dict) else {}
                label = _label(name, args)
                j.emit({"t": "state", "s": STATE_FOR.get(name, "think")})
                j.emit({"t": "step", "kind": name, "label": label})
                try:
                    res = _tool(j, name, args, steps)
                except InterruptedError:
                    raise
                except Exception as e:
                    res = {"error": str(e)[:400]}
                ok = "error" not in res and res.get("exit_code", 0) == 0
                short = ""
                if name == "run":
                    short = (res.get("output") or "")[-1500:]
                elif "error" in res:
                    short = res["error"]
                j.emit({"t": "step_done", "ok": ok, "out": short})
                steps.append({"kind": name, "label": label, "ok": ok, "out": short[-600:]})
                results.append((cid, res))
            if anthropic:
                msgs.append(chat._anthropic_results(results))
            else:
                msgs.extend(chat._openai_results(results))
            if part and not part.endswith(("\n", " ")):
                reply += "\n\n"
                j.emit({"t": "text", "d": "\n\n"})
        else:
            pass
        work = [s for s in steps if s["kind"] not in ("read_skill", "search_history")]
        if learn and skills.worth_learning(work, j.used_skills) and not j.cancel.is_set():
            # The learning loop: look back, keep what's reusable.
            j.emit({"t": "state", "s": "think", "label": "Saving what it learned"})
            try:
                got = skills.reflect(c, turn, "kiln", j.text, work, reply, j.used_skills)
            except Exception as e:
                got = None
                LOG("kiln: reflection failed: %s" % e)
            if got:
                label = ("Learned a skill: %s" if got["action"] == "learned" else "Improved a skill: %s") % got["name"]
                j.emit({"t": "step", "kind": "learn", "label": label})
                j.emit({"t": "step_done", "ok": True, "out": got["description"]})
                steps.append({"kind": "learn", "label": label, "ok": True, "out": got["description"]})
                LOG("kiln: %s %s" % (got["action"], got["name"]))
        _store(pid, "assistant", reply.strip(), steps)
        LOG("kiln: %s done - %d steps, %.0fs" % (pid, len(steps), time.time() - j.started))
        j.emit({"t": "state", "s": "done"})
        j.emit({"t": "done"})
    except InterruptedError:
        _store(pid, "assistant", (reply.strip() + "\n\n" if reply.strip() else "") + "_Stopped._", steps)
        j.emit({"t": "state", "s": "idle"})
        j.emit({"t": "stopped"})
    except Exception as e:
        msg = str(e)[:400]
        LOG("kiln: %s failed: %s" % (pid, msg))
        _store(pid, "assistant", reply.strip(), steps + [{"kind": "error", "label": msg, "ok": False, "out": ""}])
        j.emit({"t": "state", "s": "error"})
        j.emit({"t": "error", "d": msg})
    finally:
        forge.touch(-1)
        with j.cv:
            j.done = True
            j.cv.notify_all()
