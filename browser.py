"""A real browser for the chat's read_webpage, through PinchTab.

Plain fetching can't read pages that build themselves with JavaScript (live
scores, fixtures, most shops). When PinchTab is installed
(`npm install -g pinchtab`), such pages are opened in a headless browser of
our own instead - a separate profile, never the user's Chrome session - and
read as text. Nothing to set up: it starts on first use and stops itself
after a while idle.

PinchTab only listens on 127.0.0.1, behind a token kept in our data folder.
Which pages get opened is still decided by chat.py (search results or links
the user sent), and every address is checked to be on the public internet
before and after loading. The text comes back marked as untrusted web
content, which helps the model not take orders from a page.
"""
import glob
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

import paths

PORT = 9877                    # not PinchTab's default 9867, so a user's own PinchTab can run too
BASE = "http://127.0.0.1:%d" % PORT
DIR = paths.data("pinchtab")
CONF = os.path.join(DIR, "config.json")
IDLE = 600                     # stop the browser after 10 idle minutes

_lock = threading.Lock()
_proc = {"pid": None, "used": 0.0, "token": None, "reaper": False}
PIDFILE = os.path.join(DIR, "bridge.pid")


def binary():
    env = os.environ.get("PINCHTAB_BIN")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("pinchtab.exe")
    if found:
        return found
    npm = os.path.join(os.environ.get("APPDATA", ""), "npm", "node_modules", "pinchtab", ".managed-bin")
    exes = sorted(glob.glob(os.path.join(npm, "*", "pinchtab-windows-amd64.exe")), key=os.path.getmtime)
    return exes[-1] if exes else None


def chrome():
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.environ.get("LOCALAPPDATA", "")):
        for rel in (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe"):
            p = os.path.join(base, rel)
            if base and os.path.isfile(p):
                return p
    return None


def status():
    b = binary()
    return {"installed": bool(b), "browser": bool(chrome()), "ready": bool(b and chrome()),
            "running": bool(_proc["token"]) and _alive()}


def _run(args, env):
    return subprocess.run([binary()] + args, env=env, capture_output=True, timeout=30,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _configure():
    os.makedirs(DIR, exist_ok=True)
    env = dict(os.environ, PINCHTAB_CONFIG=CONF)
    if not os.path.isfile(CONF):
        _run(["config", "init"], env)
    sets = [("server.port", str(PORT)), ("server.bind", "127.0.0.1"),
            ("server.stateDir", os.path.join(DIR, "state")),
            ("browser.binary", chrome() or ""),
            # Which sites may be read is decided by chat.py, per request.
            ("security.allowedDomains", "*"),
            ("instanceDefaults.blockImages", "true"), ("instanceDefaults.blockMedia", "true"),
            ("instanceDefaults.blockAds", "true"), ("instanceDefaults.mode", "headless")]
    try:
        with open(CONF, encoding="utf-8") as f:
            cur = json.load(f)
    except Exception:
        cur = {}
    for k, v in sets:
        node = cur
        for part in k.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if str(node).lower() != v.lower() and not (k == "security.allowedDomains" and node == ["*"]):
            _run(["config", "set", k, v], env)
    with open(CONF, encoding="utf-8") as f:
        _proc["token"] = json.load(f)["server"]["token"]
    return env


def _call(method, path, body=None, timeout=45):
    r = urllib.request.Request(BASE + path, method=method,
                               data=None if body is None else json.dumps(body).encode(),
                               headers={"Authorization": "Bearer " + (_proc["token"] or ""),
                                        "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read(8 * 1024 * 1024) or b"{}")


def _alive():
    if not _proc["token"]:
        return False
    try:
        _call("GET", "/health", timeout=2)
        return True
    except Exception:
        return False


def _ensure():
    with _lock:
        if _proc["token"] is None and os.path.isfile(CONF):
            try:
                with open(CONF, encoding="utf-8") as f:
                    _proc["token"] = json.load(f)["server"]["token"]
            except Exception:
                pass
        if _alive():
            # Started earlier (maybe by an older run of the app): adopt it, so
            # it still gets stopped when idle.
            if not _proc["pid"]:
                try:
                    with open(PIDFILE) as f:
                        _proc["pid"] = int(f.read().strip())
                except Exception:
                    pass
            _watch()
            return
        if not binary():
            raise RuntimeError("PinchTab isn't installed")
        if not chrome():
            raise RuntimeError("No Chrome or Edge to read pages with")
        env = _configure()
        log = open(os.path.join(DIR, "bridge.log"), "ab")
        p = subprocess.Popen([binary(), "bridge"], env=env, stdout=log, stderr=log,
                             stdin=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _proc["pid"] = p.pid
        with open(PIDFILE, "w") as f:
            f.write(str(p.pid))
        for _ in range(60):
            time.sleep(0.25)
            if _alive():
                break
        else:
            raise RuntimeError("The page reader didn't start")
        _watch()


def _watch():
    if not _proc["reaper"]:
        _proc["reaper"] = True
        threading.Thread(target=_reaper, daemon=True).start()


def _reaper():
    try:
        while True:
            time.sleep(30)
            if time.time() - _proc["used"] > IDLE:
                stop()
                return
    finally:
        _proc["reaper"] = False


def stop():
    """Close the browser (and the Chrome it runs) - it starts again when needed."""
    pid, _proc["pid"] = _proc["pid"], None
    if pid:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass
    try:
        os.remove(PIDFILE)
    except OSError:
        pass


def read(url, is_private):
    """Open url in the headless browser and return {title, url, text}.
    is_private(host) -> True refuses pages that end up on the local network."""
    import urllib.parse
    _proc["used"] = time.time()
    _ensure()
    tab = None
    try:
        nav = _call("POST", "/navigate", {"url": url, "newTab": True}, timeout=70)
        tab = nav.get("tabId")
        final = urllib.parse.urlparse(nav.get("url") or url)
        if final.hostname and is_private(final.hostname):
            return {"error": "That page redirected somewhere private."}
        t = _call("GET", "/text?tabId=" + urllib.parse.quote(str(tab or "")), timeout=45)
        return {"title": nav.get("title", ""), "url": nav.get("url") or url, "text": t.get("text", "")}
    finally:
        _proc["used"] = time.time()
        if tab:
            try:
                _call("POST", "/tabs/%s/close" % urllib.parse.quote(str(tab)), {}, timeout=10)
            except Exception:
                pass
