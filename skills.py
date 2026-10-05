"""Skills: what the chat and Kiln learn from doing things - the idea behind
Hermes Agent's learning loop, kept small and on this PC.

After a task that took real work (several tool calls, or a mistake it had to
recover from), the model looks back at what it did and writes - or improves -
a short how-to: when to use it, the steps, the pitfalls. Next time, the names
and one-line descriptions of everything it has learned are in its
instructions, and it reads the full how-to when a task matches. Using a skill
and hitting a new snag improves that skill.

Each skill is a plain Markdown file (skills/chat/*.md, skills/kiln/*.md) -
readable, editable, and they open in Obsidian like the memory notes do. The
phone lists them (Chat > Skills) and can delete any of them.

Also here: searching past chats and Kiln projects ("did we do this before?"),
so neither starts from zero.

Skills only describe how to use the tools the user already allowed. A skill
can't switch a tool on, and anything that needs Face ID / PIN still does.
"""
import json
import os
import re
import threading
import time

import paths

ROOT = paths.data("skills")
SCOPES = ("chat", "kiln")
MAX_SKILLS = 150
_lock = threading.Lock()
# Never written into a skill: things that look like real secrets.
SECRETISH = re.compile(r"(?i)\bsk-[a-z0-9_-]{12,}|\bghp_[a-z0-9]{20,}|\bAKIA[0-9A-Z]{16}\b|\b\d{13,19}\b"
                       r"|\b(password|passwd|pwd)\s*[:=]\s*\S{4,}|\b[A-Za-z0-9+/]{40,}={0,2}\b")


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:48]


def _path(scope, name):
    if scope not in SCOPES:
        raise ValueError("bad scope")
    s = _slug(name)
    if not s:
        raise ValueError("A skill needs a name")
    return os.path.join(ROOT, scope, s + ".md")


def _parse(p):
    with open(p, encoding="utf-8") as f:
        raw = f.read()
    meta, body = {}, raw
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", raw, re.S)
    if m:
        body = m.group(2)
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            v = v.strip()
            try:
                meta[k.strip()] = json.loads(v)
            except ValueError:
                meta[k.strip()] = v
    return meta, body.strip()


def _write(p, meta, body):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    head = "\n".join("%s: %s" % (k, json.dumps(v)) for k, v in meta.items())
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("---\n%s\n---\n\n%s\n" % (head, body.strip()))
    os.replace(tmp, p)


def listing(scope=None):
    out = []
    for sc in ([scope] if scope else SCOPES):
        d = os.path.join(ROOT, sc)
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if not n.endswith(".md"):
                continue
            try:
                meta, body = _parse(os.path.join(d, n))
            except Exception:
                continue
            out.append({"name": n[:-3], "scope": sc, "description": str(meta.get("description", ""))[:200],
                        "uses": int(meta.get("uses", 0) or 0), "version": int(meta.get("version", 1) or 1),
                        "updated": float(meta.get("updated", 0) or 0), "created": float(meta.get("created", 0) or 0)})
    return sorted(out, key=lambda s: (-s["uses"], -s["updated"]))


def count(scope=None):
    return len(listing(scope))


def get(scope, name):
    p = _path(scope, name)
    if not os.path.isfile(p):
        return None
    meta, body = _parse(p)
    return {"name": _slug(name), "scope": scope, "description": meta.get("description", ""), "body": body,
            "uses": meta.get("uses", 0), "version": meta.get("version", 1), "updated": meta.get("updated", 0)}


def find(scope, name):
    """A skill by name, forgiving about spelling (models paraphrase)."""
    s = get(scope, name)
    if s:
        return s
    want = set(_slug(name).split("-"))
    best, score = None, 0
    for k in listing(scope):
        have = set(k["name"].split("-"))
        sc = len(want & have)
        if sc > score:
            best, score = k, sc
    return get(scope, best["name"]) if best and score >= max(1, len(want) // 2) else None


def used(scope, name):
    """A skill was read for a task: count it (the most useful rise to the top)."""
    p = _path(scope, name)
    with _lock:
        if os.path.isfile(p):
            meta, body = _parse(p)
            meta["uses"] = int(meta.get("uses", 0) or 0) + 1
            meta["lastUsed"] = time.time()
            _write(p, meta, body)


def save(scope, name, description, body, source=""):
    name, description, body = _slug(name), re.sub(r"\s+", " ", str(description or "")).strip()[:200], str(body or "").strip()
    if not name or not description or len(body) < 40:
        raise ValueError("A skill needs a name, a description and some steps")
    if SECRETISH.search(body) or SECRETISH.search(description):
        raise ValueError("That looks like it contains a secret - not saved")
    body = body[:6000]
    p = _path(scope, name)
    with _lock:
        if os.path.isfile(p):
            meta, _ = _parse(p)
            meta.update(description=description, updated=time.time(), version=int(meta.get("version", 1) or 1) + 1)
            action = "improved"
        else:
            if count(scope) >= MAX_SKILLS:
                raise ValueError("Too many skills - delete some on the phone first")
            meta = {"name": name, "description": description, "scope": scope, "created": time.time(),
                    "updated": time.time(), "version": 1, "uses": 0, "source": str(source)[:120]}
            action = "learned"
        _write(p, meta, body)
    return {"name": name, "description": description, "action": action}


def delete(scope, name):
    p = _path(scope, name)
    with _lock:
        if not os.path.isfile(p):
            return False
        os.remove(p)
        return True


def prompt(scope, limit=30):
    ks = listing(scope)[:limit]
    if not ks:
        return ""
    return ("Skills you've learned from earlier tasks - when a task matches one, call read_skill with its name "
            "FIRST and follow it:\n" + "\n".join("- %s: %s" % (k["name"], k["description"]) for k in ks))


# --------------------------------------------------------------- learning

REFLECT = """You just finished a task. Look back at it and decide whether there's a reusable SKILL worth saving \
for next time - a short recipe your future self can follow.

{existing}

THE TASK:
{task}

WHAT YOU DID (in order, with results):
{steps}

YOUR FINAL ANSWER:
{reply}

Decide:
- If this was routine and teaches nothing reusable, reply with just: NONE
- If one of the existing skills covers this and you learned something new (a pitfall, a better order, a \
command that works), rewrite THAT skill in full with the same NAME.
- Otherwise write a NEW skill.

Use exactly this format:
NAME: short-kebab-case-name
DESCRIPTION: one line - what kind of task it's for
---
## When to use
(one or two lines)
## Steps
1. ...
## Pitfalls
- ...

Keep it general - no one-off names, numbers or file contents from this task unless they always apply. \
Under 250 words. Never include passwords, keys, tokens or personal details."""


def reflect(c, turn, scope, task, steps, reply, used_names=()):
    """Ask the model to write or improve a skill from what just happened.
    `turn` is chat._openai_turn / chat._anthropic_turn. Returns the saved
    skill ({name, description, action}) or None."""
    ex = []
    for k in listing(scope)[:40]:
        line = "- %s: %s" % (k["name"], k["description"])
        if k["name"] in used_names:
            s = get(scope, k["name"])
            line += "\n  (you used this one - its current text:)\n  " + s["body"].replace("\n", "\n  ")[:2500]
        ex.append(line)
    existing = ("EXISTING SKILLS:\n" + "\n".join(ex)) if ex else "EXISTING SKILLS: none yet."
    lines = []
    for s in steps[-40:]:
        lines.append("- %s -> %s%s" % (s.get("label", s.get("kind", "?")), "ok" if s.get("ok") else "FAILED",
                                      (": " + re.sub(r"\s+", " ", s.get("out", ""))[-240:]) if s.get("out") and not s.get("ok") else ""))
    cc = dict(c, max_tokens=900, timeout=120)
    text, _, _ = turn(cc, "You turn finished tasks into short, reusable how-to notes.",
                      [{"role": "user", "content": REFLECT.format(existing=existing, task=task[:2000],
                                                                   steps="\n".join(lines) or "(none)",
                                                                   reply=(reply or "")[:1500])}],
                      [], lambda e: None)
    text = (text or "").strip()
    if not text or re.match(r"^\W*NONE\b", text, re.I):
        return None
    m = re.search(r"NAME:\s*(.+)", text)
    d = re.search(r"DESCRIPTION:\s*(.+)", text)
    body = text.split("---", 1)[1] if "---" in text else ""
    if not (m and d and body.strip()):
        return None
    try:
        return save(scope, m.group(1).strip().strip("`*"), d.group(1).strip(), body, source=task[:120])
    except ValueError:
        return None


def worth_learning(steps, used_names=()):
    """Hermes saves a skill after real work. Here: four or more steps, or a
    failure it recovered from, or a skill it followed (which may need fixing)."""
    if not steps:
        return False
    recovered = any(not s.get("ok") for s in steps) and steps[-1].get("ok")
    return len(steps) >= 4 or recovered or bool(used_names)


# --------------------------------------------------------------- searching what came before

def search_history(query, n=6):
    """Past chats and Kiln projects that mention these words - newest first
    among equals. Returns short snippets, never whole conversations."""
    words = [w for w in re.findall(r"[a-z0-9]{3,}", str(query or "").lower())][:8]
    if not words:
        return []
    found = []

    def scan(kind, title, ts, msgs, ref):
        best, best_score, best_snip = None, 0, ""
        for m in msgs:
            t = str(m.get("text") or "")
            low = t.lower()
            score = sum(1 for w in words if w in low)
            if score > best_score:
                i = min((low.find(w) for w in words if w in low), default=0)
                best_score, best_snip = score, t[max(0, i - 120): i + 240]
        tl = (title or "").lower()
        best_score += sum(2 for w in words if w in tl)
        if best_score:
            found.append({"where": kind, "title": title, "when": time.strftime("%Y-%m-%d", time.localtime(ts or 0)),
                          "ref": ref, "snippet": re.sub(r"\s+", " ", best_snip).strip(), "_s": best_score, "_t": ts or 0})
    try:
        import chat
        for c in chat._hist():
            scan("chat", c.get("title", ""), c.get("updated", 0), c.get("messages", []), c.get("id"))
    except Exception:
        pass
    try:
        import forge
        for p in forge.listing():
            m = forge.meta(p["id"]) or {}
            scan("kiln project", m.get("name", ""), m.get("updated", 0), m.get("msgs", []), p["id"])
    except Exception:
        pass
    found.sort(key=lambda f: (-f["_s"], -f["_t"]))
    for f in found:
        f.pop("_s"); f.pop("_t")
    return found[:n]
