"""The sleep timer: "in 45 minutes, pause what's playing" (or mute, lock,
sleep or shut down the PC). One timer at a time; it survives the server
restarting, and it warns a minute before it does anything drastic.
"""
import json
import os
import threading
import time

import paths

ACTIONS = {
    "pause": "pause what's playing",
    "mute": "mute the PC",
    "screenoff": "turn the screen off",
    "lock": "lock the PC",
    "sleep": "put the PC to sleep",
    "shutdown": "shut the PC down",
}
POWER = {"sleep", "shutdown"}            # set with Face ID / PIN
STATE = paths.data("timer.json")
WARN = 60

_lock = threading.Lock()
_t = {"v": None}                          # {"action", "at", "set"}
_hooks = {"run": None, "notify": None}
_started = False


def configure(run, notify=None):
    """run(action) does it; notify(kind, title, body) tells the phone."""
    _hooks["run"], _hooks["notify"] = run, notify
    _load()
    _start()


def _load():
    try:
        with open(STATE, encoding="utf-8") as f:
            v = json.load(f)
        if v and v.get("at", 0) > time.time() - 30 and v.get("action") in ACTIONS:
            _t["v"] = v
    except Exception:
        pass


def _save():
    try:
        if _t["v"]:
            with open(STATE, "w", encoding="utf-8") as f:
                json.dump(_t["v"], f)
        elif os.path.isfile(STATE):
            os.remove(STATE)
    except OSError:
        pass


def info():
    with _lock:
        v = _t["v"]
        if not v:
            return {"on": False}
        left = max(0, int(v["at"] - time.time()))
        return {"on": True, "action": v["action"], "what": ACTIONS[v["action"]],
                "at": v["at"], "left": left, "minutes": v.get("minutes")}


def start(minutes, action):
    if action not in ACTIONS:
        raise ValueError("unknown timer action")
    minutes = float(minutes)
    if not 0 < minutes <= 24 * 60:
        raise ValueError("pick between 1 minute and 24 hours")
    with _lock:
        _t["v"] = {"action": action, "at": time.time() + minutes * 60,
                   "set": time.time(), "minutes": minutes, "warned": False}
        _save()
    return info()


def cancel():
    with _lock:
        _t["v"] = None
        _save()
    return info()


def _loop():
    while True:
        time.sleep(1)
        fire = None
        with _lock:
            v = _t["v"]
            if not v:
                continue
            left = v["at"] - time.time()
            if left <= WARN and not v.get("warned") and v["action"] in POWER | {"lock"}:
                v["warned"] = True
                if _hooks["notify"]:
                    try:
                        _hooks["notify"]("timer", "Sleep timer",
                                         "In 1 minute the PC will %s. Open the app to cancel."
                                         % {"sleep": "go to sleep", "shutdown": "shut down",
                                            "lock": "lock"}[v["action"]])
                    except Exception:
                        pass
            if left <= 0:
                fire = v["action"]
                _t["v"] = None
                _save()
        if fire and _hooks["run"]:
            try:
                _hooks["run"](fire)
            except Exception:
                pass


def _start():
    global _started
    if not _started:
        _started = True
        threading.Thread(target=_loop, name="sleep-timer", daemon=True).start()
