"""What the chat remembers about you - and the map of it.

The AI saves a fact only when you tell it something lasting (a preference, a
person, a project, your setup) - each filed under one to three topics. It
lives on this PC only: data/chat_memory.json, mirrored as a folder of
Markdown notes linked with [[wikilinks]] (chat-memory/), so the same memory
opens as an Obsidian vault, or feeds a graph tool like graphify, as it is.
The phone shows it as a graph, where you can read and delete any of it.
"""
import json
import os
import re
import threading
import time
import uuid

import paths

STORE = paths.data("chat_memory.json")
VAULT = paths.data("chat-memory")
MAX_FACTS = 400
# Never kept, whatever the model is told: things that look like secrets.
SECRET = re.compile(r"(?i)pass(word|code|phrase)|\bpin\b|api[ _-]?key|secret|token|social security|\bssn\b"
                    r"|credit card|card number|cvv|\b\d{12,19}\b|\b\d{3}-\d{2}-\d{4}\b")
_lock = threading.Lock()


def _load():
    try:
        with open(STORE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    d.setdefault("facts", [])
    return d


def _save(d):
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, STORE)
    try:
        _write_vault(d)
    except Exception:
        pass


def _topic(t):
    t = re.sub(r"[^\w &+'.-]", "", str(t or "")).strip(" .-")[:40]
    return (t[:1].upper() + t[1:]) if t else ""


def _topics(topics):
    if isinstance(topics, str):
        topics = topics.split(",")
    out = []
    for t in topics or []:
        t = _topic(t)
        if t and t.lower() not in (o.lower() for o in out) and t.lower() != "you":
            out.append(t)
    return out[:3] or ["General"]


def facts():
    return sorted(_load()["facts"], key=lambda f: -f["ts"])


def count():
    return len(_load()["facts"])


def remember(text, topics=None):
    text = re.sub(r"\s+", " ", str(text or "")).strip()[:300]
    if len(text) < 3:
        raise ValueError("There's nothing to remember")
    if SECRET.search(text):
        raise ValueError("That looks like a password, key or number to keep private - not saved")
    tops = _topics(topics)
    with _lock:
        d = _load()
        for f in d["facts"]:
            if f["text"].lower() == text.lower():
                f["topics"] = (f["topics"] + [t for t in tops if t not in f["topics"]])[:4]
                f["ts"] = time.time()
                _save(d)
                return f
        f = {"id": uuid.uuid4().hex[:10], "text": text, "topics": tops, "ts": time.time()}
        d["facts"] = (d["facts"] + [f])[-MAX_FACTS:]
        _save(d)
        return f


def recall(query="", n=8):
    words = set(re.findall(r"\w{3,}", str(query).lower()))
    fs = facts()
    if not words:
        return fs[:n]
    scored = []
    for f in fs:
        hay = (f["text"] + " " + " ".join(f["topics"])).lower()
        s = sum(1 for w in words if w in hay)
        if s:
            scored.append((s, f["ts"], f))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [x[2] for x in scored[:n]]


def forget(fid=None, match=None):
    """By id (the phone), or every fact containing `match` (the AI, when you
    ask it to forget something) - at most five at a time."""
    with _lock:
        d = _load()
        if fid:
            gone = [f for f in d["facts"] if f["id"] == fid]
        else:
            m = str(match or "").strip().lower()
            gone = [f for f in d["facts"] if len(m) >= 3 and (m in f["text"].lower()
                                                               or m in [t.lower() for t in f["topics"]])][:5]
        if gone:
            ids = {f["id"] for f in gone}
            d["facts"] = [f for f in d["facts"] if f["id"] not in ids]
            _save(d)
        return [f["text"] for f in gone]


def clear():
    with _lock:
        _save({"facts": []})


def prompt(limit=30, chars=2500):
    """What goes into the system prompt: the newest facts, kept short."""
    lines, size = [], 0
    for f in facts()[:limit]:
        line = "- %s [%s]" % (f["text"], ", ".join(f["topics"]))
        if size + len(line) > chars:
            break
        lines.append(line)
        size += len(line)
    return "\n".join(lines)


def graph():
    """Nodes and links for the phone: you at the centre, your topics around
    you, and each fact hanging off the topics it was filed under."""
    fs = facts()
    nodes = [{"id": "you", "label": "You", "kind": "you"}]
    links, topics = [], {}
    for f in fs:
        for t in f["topics"]:
            k = "t:" + t.lower()
            if k not in topics:
                topics[k] = {"id": k, "label": t, "kind": "topic", "n": 0}
            topics[k]["n"] += 1
            links.append({"s": "f:" + f["id"], "t": k})
        nodes.append({"id": "f:" + f["id"], "fid": f["id"], "label": f["text"], "kind": "fact",
                      "ts": f["ts"], "topics": f["topics"]})
    nodes[1:1] = list(topics.values())
    links += [{"s": "you", "t": k} for k in topics]
    return {"nodes": nodes, "links": links, "count": len(fs), "topics": len(topics)}


def _fname(t):
    return re.sub(r'[<>:"/\\|?*]', "", t).strip() or "General"


def _write_vault(d):
    os.makedirs(VAULT, exist_ok=True)
    by = {}
    for f in sorted(d["facts"], key=lambda f: f["ts"]):
        for t in f["topics"]:
            by.setdefault(t, []).append(f)
    keep = {"You.md"}
    with open(os.path.join(VAULT, "You.md"), "w", encoding="utf-8") as fh:
        fh.write("# You\n\nWhat the Aether Remote chat has learned about you.\n\n")
        fh.write("".join("- [[%s]] (%d)\n" % (_fname(t), len(v)) for t, v in sorted(by.items())))
    for t, fl in by.items():
        name = _fname(t) + ".md"
        keep.add(name)
        others = sorted({o for f in fl for o in f["topics"] if o != t})
        with open(os.path.join(VAULT, name), "w", encoding="utf-8") as fh:
            fh.write("# %s\n\nPart of [[You]]%s.\n\n" % (t, "".join(", see [[%s]]" % _fname(o) for o in others)))
            fh.write("".join("- %s  _(%s)_\n" % (f["text"], time.strftime("%Y-%m-%d", time.localtime(f["ts"])))
                             for f in fl))
    for name in os.listdir(VAULT):
        if name.endswith(".md") and name not in keep:
            try:
                os.remove(os.path.join(VAULT, name))
            except OSError:
                pass
