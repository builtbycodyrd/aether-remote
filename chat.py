"""The AI chat: your own AI model, set up once on the PC, talked to from the
phone - with tools it can use, each one switched on or off by you.

Set up in the PC app (Settings > Chatbox): pick a provider, paste an API
key, pick a model. The key stays in this install's data folder; the phone
never sees it (the Files tab can't read that folder either).

Providers: Anthropic (Claude) natively, and anything that speaks the OpenAI
chat API - OpenAI itself, OpenRouter, and local models through Ollama or LM
Studio (no key needed).

Tools, all built in, none needing anything installed:
  web_search    Brave Search or Tavily (free keys, reliable), or DuckDuckGo
                with no key at all (it sometimes turns automated searches away)
  read_webpage  the text of a page - ONLY one that came from a search result
                or that you typed yourself. A page can't send the AI off to an
                address of its own choosing, which is how a hostile page
                would try to smuggle your data out.
  pc_status     CPU/GPU/RAM, volume, what's playing, what's running
  media         play/pause, skip, jump back/forward
  volume        set the volume, mute
  open_app      open something from your library (games, apps)
  files         search your files by name and read text files (read-only)
Nothing can shut down, delete, type, click or run commands. Ever.
"""
import html
import ipaddress
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import paths

CONF = paths.data("chat.json")
HIST = paths.data("chat_history.json")
MAX_ROUNDS = 8
MAX_TOKENS = 2048
TIMEOUT = 90

PROVIDERS = {
    "anthropic": {"name": "Anthropic (Claude)", "base": "https://api.anthropic.com", "key": True,
                  "keyUrl": "https://console.anthropic.com/settings/keys"},
    "openai": {"name": "OpenAI", "base": "https://api.openai.com/v1", "key": True,
               "keyUrl": "https://platform.openai.com/api-keys"},
    "openrouter": {"name": "OpenRouter", "base": "https://openrouter.ai/api/v1", "key": True,
                   "keyUrl": "https://openrouter.ai/keys"},
    "ollama": {"name": "Ollama (on this PC)", "base": "http://127.0.0.1:11434/v1", "key": False,
               "keyUrl": "https://ollama.com/download"},
    "custom": {"name": "Other (OpenAI-compatible)", "base": "", "key": False, "keyUrl": ""},
}

TOOLS = [
    # id, name, what it lets the AI do, on by default
    ("web", "Web search", "Search the web and read pages from the results", True),
    ("pc", "PC status", "See CPU/GPU/RAM, the volume, what's playing and what's open", True),
    ("media", "Media", "Play/pause, skip and jump back or forward", False),
    ("volume", "Volume", "Change the volume and mute", False),
    ("apps", "Open apps", "Open games and apps from your library", False),
    ("files", "Files", "Search your files by name and read text files (never changes anything)", False),
]
TOOL_IDS = {t[0] for t in TOOLS}
SEARCHES = {
    "duckduckgo": {"name": "Free (DuckDuckGo, then Bing)", "key": False, "keyUrl": ""},
    "brave": {"name": "Brave Search", "key": True, "keyUrl": "https://api-dashboard.search.brave.com/app/keys"},
    "tavily": {"name": "Tavily", "key": True, "keyUrl": "https://app.tavily.com/home"},
}

DEFAULT_SYSTEM = ("You are a helpful assistant living in Aether Remote, an app that controls the "
                  "user's Windows PC from their phone. Answer clearly and briefly - the user is on "
                  "a phone. Use the tools you have when they help; say so when you can't do "
                  "something.")

_lock = threading.Lock()


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
    c.setdefault("provider", "anthropic")
    c.setdefault("base_url", "")
    c.setdefault("model", "")
    c.setdefault("system", "")
    c.setdefault("api_key", "")
    tools = c.get("tools") if isinstance(c.get("tools"), dict) else {}
    c["tools"] = {t[0]: bool(tools.get(t[0], t[3])) for t in TOOLS}
    s = c.get("search") if isinstance(c.get("search"), dict) else {}
    c["search"] = {"provider": s.get("provider") if s.get("provider") in SEARCHES else "duckduckgo",
                   "key": str(s.get("key") or "")}
    return c


def _base(c):
    b = (c.get("base_url") or PROVIDERS.get(c["provider"], {}).get("base") or "").rstrip("/")
    return b


def ready(c=None):
    c = c or config()
    p = PROVIDERS.get(c["provider"])
    return bool(c["enabled"] and p and c["model"] and _base(c) and (c["api_key"] or not p["key"]))


def public(local=False):
    """What the phone (or the PC app) may see. Never the keys."""
    c = config()
    out = {"ready": ready(c), "enabled": c["enabled"], "provider": c["provider"],
           "providerName": PROVIDERS.get(c["provider"], {}).get("name", ""),
           "model": c["model"],
           "tools": [{"id": t[0], "name": t[1], "on": c["tools"][t[0]]} for t in TOOLS]}
    if local:
        k = c["api_key"]
        out.update(base_url=c["base_url"], system=c["system"], defaultSystem=DEFAULT_SYSTEM,
                   hasKey=bool(k), keyHint=("…" + k[-4:]) if len(k) >= 8 else ("set" if k else ""),
                   searchProvider=c["search"]["provider"], hasSearchKey=bool(c["search"]["key"]),
                   searches=SEARCHES,
                   providers=PROVIDERS, toolList=[{"id": t[0], "name": t[1], "desc": t[2]} for t in TOOLS])
    return out


def update(b):
    """From the PC app. A missing api_key keeps the saved one; "" clears it."""
    c = config()
    if b.get("provider") in PROVIDERS:
        if b["provider"] != c["provider"] and "api_key" not in b:
            c["api_key"] = ""                  # a key is for one provider
        c["provider"] = b["provider"]
    if "base_url" in b:
        u = str(b.get("base_url") or "").strip().rstrip("/")
        if u and not re.match(r"^https?://[^\s/]+(/\S*)?$", u):
            raise ValueError("That address doesn't look right (it should start with https://)")
        c["base_url"] = u[:300]
    if "model" in b:
        c["model"] = str(b.get("model") or "").strip()[:120]
    if "system" in b:
        c["system"] = str(b.get("system") or "")[:4000]
    if "api_key" in b:
        c["api_key"] = str(b.get("api_key") or "").strip()[:400]
    if isinstance(b.get("tools"), dict):
        for k, v in b["tools"].items():
            if k in TOOL_IDS:
                c["tools"][k] = bool(v)
    if isinstance(b.get("search"), dict):
        s = b["search"]
        if s.get("provider") in SEARCHES:
            if s["provider"] != c["search"]["provider"] and "key" not in s:
                c["search"]["key"] = ""          # a key belongs to one search service
            c["search"]["provider"] = s["provider"]
        if "key" in s:
            c["search"]["key"] = str(s.get("key") or "").strip()[:200]
    if "enabled" in b:
        c["enabled"] = bool(b["enabled"])
    with _lock:
        _save(c)
    return public(local=True)


# --------------------------------------------------------------- talking HTTP

def _open(url, body=None, headers=None, timeout=TIMEOUT, method=None):
    data = None if body is None else json.dumps(body).encode()
    h = {"Content-Type": "application/json", "User-Agent": "AetherRemote"}
    h.update(headers or {})
    r = urllib.request.Request(url, data=data, headers=h, method=method or ("POST" if data else "GET"))
    try:
        return urllib.request.urlopen(r, timeout=timeout, context=ssl.create_default_context())
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read(20000) or b"{}")
            msg = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else j.get("error")
            msg = msg or j.get("message") or str(e)
        except Exception:
            msg = str(e)
        if e.code in (401, 403):
            msg = "The API key was refused (%s)" % msg
        raise RuntimeError(str(msg)[:300])
    except urllib.error.URLError as e:
        raise RuntimeError("Couldn't reach %s (%s)" % (urllib.parse.urlparse(url).netloc, e.reason))


def _headers(c):
    if c["provider"] == "anthropic":
        return {"x-api-key": c["api_key"], "anthropic-version": "2023-06-01"}
    h = {}
    if c["api_key"]:
        h["Authorization"] = "Bearer " + c["api_key"]
    if c["provider"] == "openrouter":
        h["HTTP-Referer"] = "https://github.com/builtbycodyrd/aether-remote"
        h["X-Title"] = "Aether Remote"
    return h


def list_models(b=None):
    """The models this key can use - so nobody has to type a model name."""
    c = config()
    if b:
        c = dict(c)
        if b.get("provider") in PROVIDERS:
            if b["provider"] != config()["provider"]:
                c["api_key"] = ""
            c["provider"] = b["provider"]
        if "base_url" in b:
            c["base_url"] = str(b.get("base_url") or "").strip().rstrip("/")
        if b.get("api_key"):
            c["api_key"] = str(b["api_key"]).strip()
    base = _base(c)
    if not base:
        raise ValueError("Enter the address first")
    url = base + ("/v1/models?limit=100" if c["provider"] == "anthropic" else "/models")
    with _open(url, headers=_headers(c), timeout=20) as r:
        j = json.loads(r.read(4 * 1024 * 1024))
    items = j.get("data") or j.get("models") or []
    out = []
    for m in items:
        mid = m.get("id") or m.get("name")
        if not mid:
            continue
        if c["provider"] == "openai" and not re.match(r"^(gpt|o\d|chatgpt)", mid):
            continue            # embeddings, whisper, dall-e... aren't chat models
        out.append({"id": mid, "name": m.get("display_name") or m.get("name") or mid})
    if c["provider"] != "anthropic":
        out.sort(key=lambda m: m["id"])
    return out[:300]


_pm = {"at": 0.0, "key": None, "v": []}
NOT_CHAT = re.compile(r"embed|whisper|tts|rerank|moderation|dall-e|transcribe", re.I)


def phone_models():
    """The chat models the phone may pick from (a short cache - the picker
    opens often, the list rarely changes). For Ollama, each says how big it
    is and whether it's already loaded, i.e. answers instantly."""
    c = config()
    key = (c["provider"], _base(c), c["api_key"][-6:])
    if _pm["key"] != key or time.time() - _pm["at"] > 30:
        out = [m for m in list_models() if not NOT_CHAT.search(m["id"])]
        if c["provider"] == "ollama":
            root = re.sub(r"/v1/?$", "", _base(c))
            try:
                with _open(root + "/api/tags", timeout=5) as r:
                    tags = {m.get("name"): m for m in json.loads(r.read()).get("models", [])}
                for m in out:
                    t = tags.get(m["id"]) or {}
                    d = t.get("details") or {}
                    bits = [d.get("parameter_size"), ("%.1f GB" % (t["size"] / 1e9)) if t.get("size") else None]
                    m["detail"] = " · ".join(b for b in bits if b)
            except Exception:
                pass
        _pm.update(at=time.time(), key=key, v=out)
    out = [dict(m) for m in _pm["v"]]
    if c["provider"] == "ollama":
        try:
            with _open(re.sub(r"/v1/?$", "", _base(c)) + "/api/ps", timeout=3) as r:
                live = {m.get("name") for m in json.loads(r.read()).get("models", [])}
            for m in out:
                m["loaded"] = m["id"] in live
        except Exception:
            pass
    return out


def pick_model(mid):
    """Switch models from the phone - only to one the provider actually has."""
    mid = str(mid or "").strip()
    if not any(m["id"] == mid for m in phone_models()):
        _pm["at"] = 0                              # maybe it was just installed
        if not any(m["id"] == mid for m in phone_models()):
            raise ValueError("That model isn't available on this PC's AI service")
    with _lock:
        c = config()
        c["model"] = mid
        _save(c)
    return public()


# --------------------------------------------------------------- tools

def _private_host(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True
    for i in infos:
        ip = ipaddress.ip_address(i[4][0].split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or \
                ip.is_multicast or ip.is_unspecified or (ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10")):
            return True
    return False


def _strip_html(raw):
    raw = re.sub(r"(?is)<(script|style|noscript|svg|head|nav|footer)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>|</tr>", "\n", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
    return re.sub(r"\n\s*\n+", "\n\n", raw).strip()


def web_search(q, c):
    q = str(q)[:300]
    if c["search"]["provider"] == "tavily" and c["search"]["key"]:
        with _open("https://api.tavily.com/search", {"query": q, "max_results": 8},
                   headers={"Authorization": "Bearer " + c["search"]["key"]}, timeout=30) as r:
            j = json.loads(r.read())
        return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": (x.get("content") or "")[:400]}
                for x in j.get("results", [])][:8]
    if c["search"]["provider"] == "brave" and c["search"]["key"]:
        with _open("https://api.search.brave.com/res/v1/web/search?count=8&q=" + urllib.parse.quote(q),
                   headers={"X-Subscription-Token": c["search"]["key"], "Accept": "application/json"},
                   timeout=20) as r:
            j = json.loads(r.read())
        return [{"title": x.get("title", ""), "url": x.get("url", ""),
                 "snippet": _strip_html(x.get("description", ""))} for x in (j.get("web") or {}).get("results", [])][:8]
    # No key: free search pages, in turn. Any one of them can turn away an
    # automated search for a while, so a block just moves on to the next.
    for engine in (_ddg_html, _ddg_lite, _bing_rss):
        try:
            out = engine(q)
        except Exception:
            out = None
        if out:
            return out
    raise RuntimeError("Web search isn't answering right now (DuckDuckGo and Bing both turned it away). "
                       "Try again in a minute - or add a free Brave Search or Tavily key in the PC app "
                       "(Settings > Chatbox) to make search reliable.")


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/141.0 Safari/537.36")


def _page(url, form=None):
    r = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode() if form else None,
                               headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    with urllib.request.urlopen(r, timeout=15, context=ssl.create_default_context()) as resp:
        return resp.read(2 * 1024 * 1024).decode("utf-8", "replace")


def _unwrap(href):
    href = html.unescape(href)
    if "uddg=" in href:
        href = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0])
    if href.startswith("//"):
        href = "https:" + href
    return href


def _keep(out, title, href, snippet):
    if not href.startswith("http") or "duckduckgo.com/y.js" in href or "bing.com/aclick" in href:
        return                                  # ads
    if any(o["url"] == href for o in out):
        return
    out.append({"title": _strip_html(title), "url": href, "snippet": _strip_html(snippet)[:400]})


def _ddg_html(q):
    page = _page("https://html.duckduckgo.com/html/", {"q": q, "b": ""})
    out = []
    for m in re.finditer(r'(?s)<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]+class="result__a"|$)', page):
        sn = re.search(r'(?s)class="result__snippet"[^>]*>(.*?)</a>', m.group(3))
        _keep(out, m.group(2), _unwrap(m.group(1)), sn.group(1) if sn else "")
        if len(out) >= 8:
            break
    return out


def _ddg_lite(q):
    page = _page("https://lite.duckduckgo.com/lite/", {"q": q})
    out = []
    for m in re.finditer(r"(?s)<a[^>]+href=\"([^\"]+)\"[^>]*class='result-link'[^>]*>(.*?)</a>(.*?)(?=class='result-link'|$)", page):
        sn = re.search(r"(?s)class='result-snippet'[^>]*>(.*?)</td>", m.group(3))
        _keep(out, m.group(2), _unwrap(m.group(1)), sn.group(1) if sn else "")
        if len(out) >= 8:
            break
    return out


def _bing_rss(q):
    page = _page("https://www.bing.com/search?format=rss&setlang=en&q=" + urllib.parse.quote(q))
    out = []
    for m in re.finditer(r"(?s)<item>(.*?)</item>", page):
        it = m.group(1)
        t = re.search(r"(?s)<title>(.*?)</title>", it)
        u = re.search(r"(?s)<link>(.*?)</link>", it)
        d = re.search(r"(?s)<description>(.*?)</description>", it)
        if t and u:
            _keep(out, html.unescape(t.group(1)), html.unescape(u.group(1)).strip(),
                  html.unescape(d.group(1)) if d else "")
        if len(out) >= 8:
            break
    return out


def _norm_url(u):
    p = urllib.parse.urlsplit(u.strip())
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", p.query, ""))


def read_webpage(url, allowed):
    url = str(url).strip()
    if _norm_url(url) not in allowed:
        return {"error": "I can only open pages from search results or links the user typed."}
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname or _private_host(u.hostname):
        return {"error": "That address isn't on the public internet."}
    r = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                                             "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1"})
    try:
        with urllib.request.urlopen(r, timeout=20, context=ssl.create_default_context()) as resp:
            # A redirect can't take it somewhere private either.
            final = urllib.parse.urlparse(resp.geturl())
            if final.hostname and _private_host(final.hostname):
                return {"error": "That page redirected somewhere private."}
            ctype = resp.headers.get("Content-Type", "")
            if not re.search(r"text/|json|xml", ctype):
                return {"error": "That isn't a web page (%s)" % ctype.split(";")[0]}
            raw = resp.read(3 * 1024 * 1024).decode(resp.headers.get_content_charset() or "utf-8", "replace")
    except Exception as e:
        return {"error": "Couldn't open it: %s" % e}
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
    text = _strip_html(raw) if "html" in ctype else raw
    return {"title": _strip_html(title.group(1)) if title else "", "url": url, "text": text[:12000],
            "truncated": len(text) > 12000}


def tool_defs(c):
    t = c["tools"]
    defs = []
    if t["web"]:
        defs += [
            ("web_search", "Search the web. Returns titles, URLs and snippets.",
             {"query": {"type": "string", "description": "What to search for"}}, ["query"]),
            ("read_webpage", "Read the text of a web page. Only works for URLs from web_search results or that the user wrote.",
             {"url": {"type": "string"}}, ["url"]),
        ]
    if t["pc"]:
        defs.append(("pc_status", "The PC's current state: CPU/GPU/RAM/disk, volume, what's playing, the game running, open windows.",
                     {}, []))
    if t["media"]:
        defs.append(("media_control", "Control what's playing on the PC.",
                     {"action": {"type": "string", "enum": ["play_pause", "next", "previous", "forward", "back"]},
                      "seconds": {"type": "integer", "description": "for forward/back, default 10"}}, ["action"]))
    if t["volume"]:
        defs.append(("set_volume", "Set the PC's master volume (0-100) and/or mute it.",
                     {"level": {"type": "integer", "minimum": 0, "maximum": 100},
                      "mute": {"type": "boolean"}}, []))
    if t["apps"]:
        defs.append(("open_app", "Open a game or app from the user's library by name.",
                     {"name": {"type": "string"}}, ["name"]))
    if t["files"]:
        defs += [
            ("search_files", "Find files on the PC by name (searches the user's folders).",
             {"query": {"type": "string"}, "folder": {"type": "string", "description": "optional folder to search in"}}, ["query"]),
            ("read_text_file", "Read a text file on the PC (first 256 KB).",
             {"path": {"type": "string"}}, ["path"]),
        ]
    return defs


LABELS = {
    "web_search": lambda a: "Searching the web for “%s”" % str(a.get("query", ""))[:60],
    "read_webpage": lambda a: "Reading %s" % (urllib.parse.urlparse(str(a.get("url", ""))).netloc or "a page"),
    "pc_status": lambda a: "Checking the PC",
    "media_control": lambda a: "Media: %s" % str(a.get("action", "")).replace("_", " "),
    "set_volume": lambda a: "Setting the volume" if a.get("level") is not None else "Muting" if a.get("mute") else "Unmuting",
    "open_app": lambda a: "Opening %s" % str(a.get("name", ""))[:40],
    "search_files": lambda a: "Searching files for “%s”" % str(a.get("query", ""))[:40],
    "read_text_file": lambda a: "Reading %s" % os.path.basename(str(a.get("path", ""))),
}


class Tools:
    """Runs the tools for one request. `hooks` come from remote.py so the
    tools use exactly the same code (and the same rules) as the phone does."""

    def __init__(self, c, hooks, user_text):
        self.c, self.h = c, hooks
        self.allowed = {_norm_url(u) for u in re.findall(r"https?://[^\s<>\"')\]]+", user_text or "")}
        self.names = {d[0] for d in tool_defs(c)}

    def run(self, name, args):
        if name not in self.names:
            return {"error": "That tool is switched off."}
        args = args if isinstance(args, dict) else {}
        try:
            if name == "web_search":
                res = web_search(args.get("query", ""), self.c)
                for r in res:
                    self.allowed.add(_norm_url(r["url"]))
                return {"results": res}
            if name == "read_webpage":
                return read_webpage(args.get("url", ""), self.allowed)
            return self.h[name](args)
        except Exception as e:
            return {"error": str(e)[:300]}


# --------------------------------------------------------------- providers

def _sse(resp):
    """Lines of a server-sent-event stream -> parsed JSON data payloads."""
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        d = line[5:].strip()
        if d == "[DONE]":
            return
        try:
            yield json.loads(d)
        except Exception:
            continue


def _anthropic_turn(c, system, msgs, defs, emit):
    body = {"model": c["model"], "max_tokens": MAX_TOKENS, "system": system,
            "messages": msgs, "stream": True}
    if defs:
        body["tools"] = [{"name": n, "description": d,
                          "input_schema": {"type": "object", "properties": p, "required": r}}
                         for n, d, p, r in defs]
    blocks, stop = [], None
    with _open(_base(c) + "/v1/messages", body, _headers(c)) as resp:
        for ev in _sse(resp):
            t = ev.get("type")
            if t == "content_block_start":
                b = ev["content_block"]
                blocks.append({"type": b["type"], "text": "", "id": b.get("id"), "name": b.get("name"), "json": ""})
            elif t == "content_block_delta":
                d = ev["delta"]
                if d.get("type") == "text_delta":
                    blocks[-1]["text"] += d["text"]
                    emit({"t": "text", "d": d["text"]})
                elif d.get("type") == "input_json_delta":
                    blocks[-1]["json"] += d.get("partial_json", "")
            elif t == "message_delta":
                stop = (ev.get("delta") or {}).get("stop_reason") or stop
            elif t == "error":
                raise RuntimeError((ev.get("error") or {}).get("message", "the model returned an error"))
    content, calls = [], []
    for b in blocks:
        if b["type"] == "text" and b["text"]:
            content.append({"type": "text", "text": b["text"]})
        elif b["type"] == "tool_use":
            try:
                args = json.loads(b["json"] or "{}")
            except Exception:
                args = {}
            content.append({"type": "tool_use", "id": b["id"], "name": b["name"], "input": args})
            calls.append((b["id"], b["name"], args))
    text = "".join(b["text"] for b in blocks if b["type"] == "text")
    return text, calls, {"role": "assistant", "content": content}


def _anthropic_results(results):
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": cid,
                                         "content": json.dumps(res)[:20000]} for cid, res in results]}


def _openai_turn(c, system, msgs, defs, emit):
    body = {"model": c["model"], "stream": True,
            "messages": [{"role": "system", "content": system}] + msgs}
    # OpenAI's newer models only take max_completion_tokens; others the old name.
    body["max_completion_tokens" if c["provider"] == "openai" else "max_tokens"] = MAX_TOKENS
    if c["provider"] == "ollama":
        # Local "thinking" models (Qwen 3.5 and co.) otherwise reason first,
        # silently, for most of a minute - and can spend every token on it.
        body["reasoning_effort"] = "none"
    if defs:
        body["tools"] = [{"type": "function", "function": {
            "name": n, "description": d,
            "parameters": {"type": "object", "properties": p, "required": r}}} for n, d, p, r in defs]
    text, calls = "", {}
    with _open(_base(c) + "/chat/completions", body, _headers(c)) as resp:
        for ev in _sse(resp):
            if ev.get("error"):
                e = ev["error"]
                raise RuntimeError(e.get("message") if isinstance(e, dict) else str(e))
            for ch in ev.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content"):
                    text += d["content"]
                    emit({"t": "text", "d": d["content"]})
                for tc in d.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                    slot["id"] = tc.get("id") or slot["id"]
                    f = tc.get("function") or {}
                    slot["name"] = f.get("name") or slot["name"]
                    slot["args"] += f.get("arguments") or ""
    out = []
    for i in sorted(calls):
        s = calls[i]
        try:
            args = json.loads(s["args"] or "{}")
        except Exception:
            args = {}
        out.append((s["id"] or "call_%d" % i, s["name"], args))
    msg = {"role": "assistant", "content": text or None}
    if out:
        msg["tool_calls"] = [{"id": cid, "type": "function",
                              "function": {"name": n, "arguments": json.dumps(a)}} for cid, n, a in out]
    return text, out, msg


def _openai_results(results):
    return [{"role": "tool", "tool_call_id": cid, "content": json.dumps(res)[:20000]} for cid, res in results]


# --------------------------------------------------------------- history

def _hist():
    try:
        with open(HIST, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_hist(h):
    tmp = HIST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(h[-40:], f)
    os.replace(tmp, HIST)


def conversations():
    return [{"id": c["id"], "title": c["title"], "updated": c["updated"], "n": len(c["messages"])}
            for c in sorted(_hist(), key=lambda c: -c["updated"])]


def conversation(cid):
    for c in _hist():
        if c["id"] == cid:
            return c
    return None


def delete(cid):
    with _lock:
        _save_hist([c for c in _hist() if c["id"] != cid])


def clear():
    with _lock:
        _save_hist([])


def _store(cid, user_text, reply, tools_used):
    with _lock:
        h = _hist()
        conv = next((c for c in h if c["id"] == cid), None)
        if not conv:
            title = re.sub(r"\s+", " ", user_text).strip()
            conv = {"id": cid, "title": (title[:48] + "…") if len(title) > 48 else title,
                    "messages": [], "created": time.time()}
            h.append(conv)
        conv["messages"] += [{"role": "user", "text": user_text, "ts": time.time()},
                             {"role": "assistant", "text": reply, "tools": tools_used, "ts": time.time()}]
        conv["messages"] = conv["messages"][-80:]
        conv["updated"] = time.time()
        _save_hist(h)


# --------------------------------------------------------------- one exchange

def send(cid, text, hooks, emit):
    """Answer one message, streaming events through emit(dict):
      {"t":"start","conv":id} {"t":"text","d":chunk}
      {"t":"tool","name":..,"label":..} {"t":"tool_done","ok":bool}
      {"t":"done"} or {"t":"error","d":message}"""
    c = config()
    if not ready(c):
        raise ValueError("The chat isn't set up yet - open the PC app > Settings > Chatbox.")
    text = str(text or "").strip()[:8000]
    if not text:
        raise ValueError("Type a message first")
    cid = re.sub(r"[^a-z0-9]", "", str(cid or ""))[:32] or uuid.uuid4().hex[:16]
    emit({"t": "start", "conv": cid})
    prev = (conversation(cid) or {}).get("messages", [])[-30:]
    msgs = [{"role": m["role"], "content": m["text"]} for m in prev if m.get("text")]
    msgs.append({"role": "user", "content": text})
    system = (c["system"].strip() or DEFAULT_SYSTEM) + \
        "\nToday is %s." % time.strftime("%A, %B %d, %Y")
    defs = tool_defs(c)
    tools = Tools(c, hooks, text)
    anthropic = c["provider"] == "anthropic"
    reply, used = "", []
    for rnd in range(MAX_ROUNDS + 1):
        turn = _anthropic_turn if anthropic else _openai_turn
        part, calls, msg = turn(c, system, msgs, defs if rnd < MAX_ROUNDS else [], emit)
        reply += part
        if not calls:
            break
        msgs.append(msg)
        results = []
        for cid_, name, args in calls:
            label = LABELS.get(name, lambda a: name)(args)
            emit({"t": "tool", "name": name, "label": label})
            res = tools.run(name, args)
            ok = "error" not in res
            emit({"t": "tool_done", "ok": ok})
            used.append({"name": name, "label": label, "ok": ok})
            results.append((cid_, res))
        if anthropic:
            msgs.append(_anthropic_results(results))
        else:
            msgs.extend(_openai_results(results))
        if part and not part.endswith(("\n", " ")):
            reply += "\n\n"
            emit({"t": "text", "d": "\n\n"})
    _store(cid, text, reply.strip(), used)
    emit({"t": "done", "conv": cid})
    return cid


def test():
    """A tiny real request, so the PC app can say "it works" before you leave."""
    c = config()
    if not ready(dict(c, enabled=True)):
        raise ValueError("Fill in the provider, key and model first")
    c = dict(c, tools={k: False for k in c["tools"]})
    out = []
    turn = _anthropic_turn if c["provider"] == "anthropic" else _openai_turn
    t0 = time.time()
    text, _, _ = turn(c, "Reply with exactly: ok", [{"role": "user", "content": "Say ok."}], [],
                      lambda e: out.append(e))
    return {"ok": True, "reply": text.strip()[:200], "ms": int((time.time() - t0) * 1000)}
