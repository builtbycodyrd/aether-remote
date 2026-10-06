"""Play PC games from the phone: the on-screen controller's other half.

Two ways to reach the game, picked per game on the phone:

  xbox  A virtual Xbox 360 controller, through the ViGEmBus driver - the game
        sees a real gamepad (analog sticks, triggers, rumble-free). Needs
        ViGEmBus installed (many controller tools install it; it's a one-time
        install from github.com/nefarius/ViGEmBus otherwise).
  kbm   Keyboard and mouse: buttons hold keys down, a stick can be WASD /
        arrows or mouse-look. Works with every game, no driver.

The phone sends its WHOLE state every time (held buttons, stick positions) -
never "press" or "release" on their own - so a lost or late message can't
leave a key stuck down. And if the phone goes quiet for over a second while
anything is held (signal lost, phone locked), everything is let go.
"""
import ctypes
import os
import threading
import time
from ctypes import Structure, c_short, c_ubyte, c_uint, c_ushort, c_void_p

import paths
import stream

user32 = ctypes.windll.user32
QUIET = 1.0                 # seconds of silence before everything is let go

# ------------------------------------------------------------------ keyboard + mouse

KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE, KEYEVENTF_EXTENDEDKEY = 0x0002, 0x0008, 0x0001
EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0xA3, 0xA5}
MOUSE = {"lmb": (0x0002, 0x0004), "rmb": (0x0008, 0x0010), "mmb": (0x0020, 0x0040)}
KEYS = dict(stream.VK)
KEYS.update({"lshift": 0xA0, "lctrl": 0xA2, "lalt": 0xA4, "rctrl": 0xA3, "ralt": 0xA5,
             "insert": 0x2D, "capslock": 0x14, "tilde": 0xC0, "minus": 0xBD, "equals": 0xBB,
             "comma": 0xBC, "period": 0xBE, "slash": 0xBF, "semicolon": 0xBA, "quote": 0xDE,
             "lbracket": 0xDB, "rbracket": 0xDD, "backslash": 0xDC, "f12": 0x7B})
for _k in ("ctrl", "alt", "shift"):          # games read the left-hand ones
    KEYS[_k] = KEYS["l" + _k]
KEY_NAMES = sorted(k for k in KEYS if not (len(k) == 1 and k.isupper())) + sorted(MOUSE)


def _key(vk, up):
    """One key down or up. Games read scan codes (DirectInput / raw input),
    so that's what's sent - with the virtual-key code only as a fallback."""
    scan = user32.MapVirtualKeyW(vk, 0)
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if vk in EXTENDED else 0)
    ki = stream.KEYBDINPUT(0, scan, flags | KEYEVENTF_SCANCODE, 0, None) if scan else \
        stream.KEYBDINPUT(vk, 0, flags, 0, None)
    return stream.INPUT(type=1, u=stream._IU(ki=ki))


# ------------------------------------------------------------------ the Xbox controller (ViGEm)

class XUSB_REPORT(Structure):
    _fields_ = [("wButtons", c_ushort), ("bLeftTrigger", c_ubyte), ("bRightTrigger", c_ubyte),
                ("sThumbLX", c_short), ("sThumbLY", c_short), ("sThumbRX", c_short), ("sThumbRY", c_short)]


XBTN = {"up": 0x0001, "down": 0x0002, "left": 0x0004, "right": 0x0008, "start": 0x0010, "back": 0x0020,
        "ls": 0x0040, "rs": 0x0080, "lb": 0x0100, "rb": 0x0200, "guide": 0x0400,
        "a": 0x1000, "b": 0x2000, "x": 0x4000, "y": 0x8000}
OK = 0x20000000


class _Vigem:
    def __init__(self):
        self.dll = self.client = self.target = None
        self.error = None
        self.plugged = False

    def _load(self):
        if self.dll:
            return True
        p = paths.asset("vendor", "vigem", "ViGEmClient.dll")
        try:
            d = ctypes.WinDLL(p)
        except OSError as e:
            self.error = "ViGEmClient.dll couldn't load (%s)" % e
            return False
        for name, args, res in (("vigem_alloc", (), c_void_p), ("vigem_free", (c_void_p,), None),
                                ("vigem_connect", (c_void_p,), c_uint), ("vigem_disconnect", (c_void_p,), None),
                                ("vigem_target_x360_alloc", (), c_void_p), ("vigem_target_free", (c_void_p,), None),
                                ("vigem_target_add", (c_void_p, c_void_p), c_uint),
                                ("vigem_target_remove", (c_void_p, c_void_p), c_uint),
                                ("vigem_target_x360_update", (c_void_p, c_void_p, XUSB_REPORT), c_uint)):
            f = getattr(d, name)
            f.argtypes, f.restype = args, res
        self.dll = d
        return True

    def available(self):
        """Is the ViGEmBus driver there? (Connects once to find out.)"""
        if self.client:
            return True
        if not self._load():
            return False
        c = self.dll.vigem_alloc()
        r = self.dll.vigem_connect(c)
        if r != OK:
            self.dll.vigem_free(c)
            self.error = "The ViGEmBus driver isn't installed"
            return False
        self.client = c
        return True

    def plug(self):
        if self.plugged:
            return True
        if not self.available():
            return False
        t = self.dll.vigem_target_x360_alloc()
        r = self.dll.vigem_target_add(self.client, t)
        if r != OK:
            self.dll.vigem_target_free(t)
            self.error = "Couldn't plug in the virtual controller (0x%08x)" % r
            return False
        self.target, self.plugged = t, True
        return True

    def send(self, rep):
        if self.plugged:
            self.dll.vigem_target_x360_update(self.client, self.target, rep)

    def unplug(self):
        if self.plugged:
            try:
                self.dll.vigem_target_x360_update(self.client, self.target, XUSB_REPORT())
                self.dll.vigem_target_remove(self.client, self.target)
                self.dll.vigem_target_free(self.target)
            except Exception:
                pass
        self.plugged, self.target = False, None


# ------------------------------------------------------------------ the pad

def _axis(v):
    try:
        v = max(-1.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0
    return int(v * 32767)


def _trig(v):
    try:
        return int(max(0.0, min(1.0, float(v))) * 255)
    except (TypeError, ValueError):
        return 0


class Pad:
    def __init__(self):
        self.lock = threading.Lock()
        self.vigem = _Vigem()
        self.keys, self.mouse = set(), set()
        self.rep = XUSB_REPORT()
        self.last = 0.0
        self.seq, self.sid = -1, None
        self.idle_unplug = 90
        threading.Thread(target=self._watch, daemon=True).start()

    def info(self):
        return {"xbox": self.vigem.available(), "xboxError": None if self.vigem.available() else self.vigem.error,
                "keys": KEY_NAMES}

    def update(self, m):
        """Apply the phone's whole controller state."""
        with self.lock:
            seq, sid = m.get("seq"), str(m.get("sid") or "")
            if sid != self.sid:                                 # a new phone / a reopened controller
                self.sid, self.seq = sid, -1
            if isinstance(seq, int):
                if seq <= self.seq:
                    return {"ok": True, "stale": True}          # an older message that arrived late
                self.seq = seq
            self.last = time.time()
            if m.get("mode") == "xbox":
                self._kbm(set(), set())
                fresh = not self.vigem.plugged
                if not self.vigem.plug():
                    return {"ok": False, "error": self.vigem.error}
                if fresh:
                    # A just-plugged pad drops what it's sent until Windows
                    # has finished attaching it (and starts with junk on the
                    # sticks) - so say it again once it's there.
                    threading.Thread(target=self._resend, daemon=True).start()
                r = XUSB_REPORT()
                for b in m.get("buttons") or []:
                    r.wButtons |= XBTN.get(str(b), 0)
                r.bLeftTrigger, r.bRightTrigger = _trig(m.get("lt")), _trig(m.get("rt"))
                # The phone's y grows downwards; a stick's grows upwards.
                r.sThumbLX, r.sThumbLY = _axis(m.get("lx")), -_axis(m.get("ly"))
                r.sThumbRX, r.sThumbRY = _axis(m.get("rx")), -_axis(m.get("ry"))
                self.rep = r
                self.vigem.send(r)
            else:
                self._neutral_pad()
                keys = {str(k) for k in (m.get("keys") or []) if str(k) in KEYS}
                mouse = {str(k) for k in (m.get("keys") or []) if str(k) in MOUSE}
                self._kbm(keys, mouse)
                try:
                    dx, dy = int(m.get("mx") or 0), int(m.get("my") or 0)
                except (TypeError, ValueError):
                    dx = dy = 0
                if dx or dy:
                    dx, dy = max(-400, min(400, dx)), max(-400, min(400, dy))
                    stream._send(stream._mouse(0x0001, dx, dy))       # relative move: what games read
            return {"ok": True}

    def _resend(self):
        for wait in (0.15, 0.3, 0.6):
            time.sleep(wait)
            with self.lock:
                self.vigem.send(self.rep)

    def _kbm(self, keys, mouse):
        ins = []
        for k in self.keys - keys:
            ins.append(_key(KEYS[k], True))
        for k in keys - self.keys:
            ins.append(_key(KEYS[k], False))
        for b in self.mouse - mouse:
            ins.append(stream._mouse(MOUSE[b][1]))
        for b in mouse - self.mouse:
            ins.append(stream._mouse(MOUSE[b][0]))
        if ins:
            stream._send(*ins)
        self.keys, self.mouse = keys, mouse

    def _neutral_pad(self):
        if self.vigem.plugged and (self.rep.wButtons or self.rep.bLeftTrigger or self.rep.bRightTrigger
                                   or self.rep.sThumbLX or self.rep.sThumbLY or self.rep.sThumbRX or self.rep.sThumbRY):
            self.rep = XUSB_REPORT()
            self.vigem.send(self.rep)

    def release(self, unplug=False):
        with self.lock:
            self._kbm(set(), set())
            self._neutral_pad()
            if unplug:
                self.vigem.unplug()

    def held(self):
        r = self.rep
        return bool(self.keys or self.mouse or r.wButtons or r.bLeftTrigger or r.bRightTrigger
                    or r.sThumbLX or r.sThumbLY or r.sThumbRX or r.sThumbRY)

    def _watch(self):
        while True:
            time.sleep(0.25)
            quiet = time.time() - self.last
            if quiet > QUIET and self.held():
                self.release()                                 # the phone went quiet: let go of everything
            if quiet > self.idle_unplug and self.vigem.plugged:
                self.release(unplug=True)                      # nobody's playing: unplug the controller


PAD = Pad()


# ------------------------------------------------------------------ layouts, saved per game on the PC

import json   # noqa: E402
import re     # noqa: E402

STORE = paths.data("controllers.json")
TYPES = {"stick", "dpad", "button"}
STICK_MAPS = {"wasd", "arrows", "mouse", "none", "left", "right"}


def _load():
    try:
        with open(STORE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def game_key(name):
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:60] or "_default"


def layout(game):
    d = _load()
    return d.get(game_key(game)) or d.get("_default")


def _num(v, lo, hi, d):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return d


def save_layout(game, lay):
    """Keep only what the controller understands - it's drawn from this."""
    if not isinstance(lay, dict):
        raise ValueError("bad layout")
    out = {"mode": "xbox" if lay.get("mode") == "xbox" else "kbm",
           "opacity": _num(lay.get("opacity"), 0.15, 1, 0.55),
           "sens": _num(lay.get("sens"), 0.2, 4, 1),
           "tapClick": bool(lay.get("tapClick")),
           "els": []}
    for e in (lay.get("els") or [])[:40]:
        if not isinstance(e, dict) or e.get("t") not in TYPES:
            continue
        el = {"id": re.sub(r"[^a-z0-9_]", "", str(e.get("id", "")))[:16], "t": e["t"],
              "x": _num(e.get("x"), 0, 100, 50), "y": _num(e.get("y"), 0, 100, 50),
              "s": _num(e.get("s"), 6, 40, 14), "hide": bool(e.get("hide"))}
        if e["t"] == "button":
            el["pad"] = str(e.get("pad", "")) if str(e.get("pad", "")) in XBTN or e.get("pad") in ("lt", "rt") else ""
            el["key"] = str(e.get("key", "")) if str(e.get("key", "")) in KEYS or e.get("key") in MOUSE else ""
            el["label"] = str(e.get("label", ""))[:6]
        elif e["t"] == "stick":
            el["pad"] = "right" if e.get("pad") == "right" else "left"
            el["kbm"] = str(e.get("kbm")) if e.get("kbm") in STICK_MAPS else "wasd"
        elif e["t"] == "dpad":
            el["kbm"] = "arrows" if e.get("kbm") == "arrows" else ("wasd" if e.get("kbm") == "wasd" else "digits")
        out["els"].append(el)
    d = _load()
    d[game_key(game)] = out
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, STORE)
    return out


def forget_layout(game):
    d = _load()
    d.pop(game_key(game), None)
    with open(STORE, "w", encoding="utf-8") as f:
        json.dump(d, f)
