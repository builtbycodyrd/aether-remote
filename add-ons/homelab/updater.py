"""Update checks for the homelab add-on.

Versions are git tags  homelab-vX.Y.Z  on the aether-remote repo (see
version.py for why they are tags and not Releases). This module answers
"is there a newer one", fetches its changelog for More info, and hands the
actual swap to update.sh, which runs detached so it survives the restart.

Also a tiny CLI, so update.sh shares this version logic instead of copying it:
    python3 updater.py latest      -> prints the newest tag, e.g. homelab-v0.2.0
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

import paths
import version

API = os.environ.get("AETHER_HL_UPDATE_API",
                     "https://api.github.com/repos/%s/tags?per_page=100" % version.REPO)
RAW = os.environ.get("AETHER_HL_RAW",
                     "https://raw.githubusercontent.com/%s" % version.REPO)
ALLOWED = ("https://api.github.com/", "https://raw.githubusercontent.com/")
STATE_PATH = paths.data("update.json")
CHECK_EVERY = 6 * 3600          # a normal check
RETRY_AFTER = 30 * 60           # after a failure, don't hammer GitHub

_lock = threading.Lock()


def _parse(tag):
    m = re.fullmatch(re.escape(version.TAG_PREFIX) + r"(\d+)\.(\d+)\.(\d+)", tag or "")
    return tuple(int(x) for x in m.groups()) if m else None


def _get(url, timeout=10):
    # Only ever talk to GitHub (or a test override pointed at localhost).
    test = url.startswith("http://127.0.0.1") or url.startswith("http://localhost")
    if not (url.startswith(ALLOWED) or test):
        raise ValueError("refusing to fetch from %s" % url)
    req = urllib.request.Request(url, headers={"User-Agent": "aether-homelab",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def latest_tag():
    tags = json.loads(_get(API).decode("utf-8"))
    best = None
    for t in tags:
        v = _parse(t.get("name"))
        if v and (best is None or v > best[0]):
            best = (v, t["name"])
    return best[1] if best else None


def _load():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_PATH)


def check(force=False):
    with _lock:
        st = _load()
        now = time.time()
        wait = RETRY_AFTER if st.get("error") else CHECK_EVERY
        if not force and now - st.get("checked", 0) < wait:
            return st
        try:
            st["latest"] = latest_tag()
            st.pop("error", None)
        except Exception as e:
            st["error"] = str(e)[:200]
        st["checked"] = now
        _save(st)
        return st


def state():
    st = check()
    cur = version.VERSION
    latest = st.get("latest")
    lv = _parse(latest)
    newer = bool(lv and lv > _parse(version.TAG))
    ver = latest[len(version.TAG_PREFIX):] if latest else None
    return {"current": cur, "latest": ver, "tag": latest, "newer": newer,
            "available": newer and st.get("skipped") != ver,
            "error": st.get("error"),
            # an install in the last 5 minutes; a fresh process after the
            # swap sees this lapse on its own, so it can't stick.
            "busy": time.time() - st.get("busy", 0) < 300}


def notes(tag):
    if not _parse(tag):
        raise ValueError("bad tag")
    url = "%s/%s/add-ons/homelab/CHANGELOG.md" % (RAW, tag)
    return _get(url).decode("utf-8", "replace")


def skip(ver, clear=False):
    with _lock:
        st = _load()
        if clear:
            st.pop("skipped", None)
        else:
            st["skipped"] = ver
        _save(st)


def install():
    """Start update.sh detached. It swaps the program folder, restarts the
    service, checks it came back, and rolls back if it didn't."""
    script = paths.asset("update.sh")
    if not os.path.isfile(script):
        raise RuntimeError("update.sh is missing")
    log = open(paths.data("update.log"), "ab")
    subprocess.Popen(["/bin/sh", script], stdout=log, stderr=log,
                     start_new_session=True, close_fds=True)
    with _lock:
        st = _load()
        st["busy"] = time.time()
        _save(st)
    return True


def clear_busy():
    """Called at server start: by the time a fresh process is up, the swap
    either finished or rolled back, so 'updating' is no longer true."""
    with _lock:
        st = _load()
        if st.pop("busy", None) is not None:
            _save(st)


def start_auto(cfg):
    """If auto_update is on, check daily and install anything newer."""
    if not cfg.get("auto_update"):
        return

    def loop():
        time.sleep(120)                      # let the server settle first
        while True:
            try:
                check(force=True)
                if state()["newer"]:
                    install()
                    return
            except Exception:
                pass
            time.sleep(24 * 3600)

    threading.Thread(target=loop, daemon=True).start()


if __name__ == "__main__":
    if sys.argv[1:] == ["latest"]:
        print(latest_tag() or "")
    else:
        print(json.dumps(state(), indent=1))
