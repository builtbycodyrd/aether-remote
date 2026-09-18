# stream.py - desktop capture + input injection (the Steam-Link-ish bit).
#
# Capture is Pillow's ImageGrab, which on this machine does 1920x1080 in
# ~20ms and a 1280-wide JPEG in ~41ms, so ~15fps streams comfortably.
#
# Coordinates: the browser sends NORMALISED (0..1) positions relative to the
# monitor being streamed. That way the phone never needs to know the
# resolution and nothing breaks when the image is scaled to fit the screen.

import ctypes
import ctypes.wintypes as wt
import io
import threading
import time
from ctypes import POINTER, Structure, byref, c_int, c_long, c_uint, c_ulong, c_ushort

user32 = ctypes.windll.user32

# Must be DPI aware or capture size and cursor coordinates disagree on a
# scaled display.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

from PIL import ImageGrab  # noqa: E402


# ------------------------------------------------------------------ monitors

class RECT(Structure):
    _fields_ = [("left", c_long), ("top", c_long),
                ("right", c_long), ("bottom", c_long)]


MONITORENUMPROC = ctypes.WINFUNCTYPE(c_int, c_ulong, c_ulong,
                                     POINTER(RECT), ctypes.c_double)


def monitors():
    """[{id, x, y, w, h, primary}] in virtual-desktop coordinates."""
    found = []

    def cb(hmon, hdc, lprect, lparam):
        r = lprect.contents
        found.append({"x": r.left, "y": r.top,
                      "w": r.right - r.left, "h": r.bottom - r.top})
        return 1

    user32.EnumDisplayMonitors(0, 0, MONITORENUMPROC(cb), 0)

    # EnumDisplayMonitors order is arbitrary - on this machine it returns the
    # portrait side monitor first. Sort so the PRIMARY display is always id 0,
    # otherwise the stream defaults to the wrong screen.
    for m in found:
        m["primary"] = (m["x"] == 0 and m["y"] == 0)
    found.sort(key=lambda m: (not m["primary"], m["x"], m["y"]))

    out = []
    for i, m in enumerate(found):
        m["id"] = i
        # Keep labels SHORT - they live in a narrow <select> on a phone and
        # "1920x1080 (main)" truncates to "1920x10".
        m["label"] = "Main" if m["primary"] else "Display %d" % (i + 1)
        m["size"] = "%dx%d" % (m["w"], m["h"])
        out.append(m)
    if not out:
        out = [{"id": 0, "x": 0, "y": 0,
                "w": user32.GetSystemMetrics(0),
                "h": user32.GetSystemMetrics(1),
                "primary": True, "label": "main"}]
    return out


def monitor(mon_id):
    ms = monitors()
    for m in ms:
        if m["id"] == mon_id:
            return m
    return ms[0]


# ------------------------------------------------------------------- capture

QUALITY = {
    "low":    {"width": 800,  "jpeg": 40, "fps": 10},
    "medium": {"width": 1280, "jpeg": 55, "fps": 15},
    "high":   {"width": 1600, "jpeg": 65, "fps": 20},
}


def grab_jpeg(mon_id=0, width=1280, jpeg=55):
    m = monitor(mon_id)
    box = (m["x"], m["y"], m["x"] + m["w"], m["y"] + m["h"])
    img = ImageGrab.grab(bbox=box, all_screens=True)
    img = img.convert("RGB")
    if img.size[0] > width:
        img.thumbnail((width, width * 4))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=jpeg)
    return buf.getvalue()


def mjpeg_frames(mon_id=0, quality="medium", stop_after=None):
    """Yields (boundary-delimited) multipart frames until the client leaves."""
    q = QUALITY.get(quality, QUALITY["medium"])
    delay = 1.0 / q["fps"]
    started = time.time()
    while True:
        t0 = time.time()
        try:
            frame = grab_jpeg(mon_id, q["width"], q["jpeg"])
        except Exception:
            time.sleep(0.5)
            continue
        yield (b"--frame\r\nContent-Type: image/jpeg\r\n"
               b"Content-Length: " + str(len(frame)).encode() +
               b"\r\n\r\n" + frame + b"\r\n")
        if stop_after and time.time() - started > stop_after:
            return
        slept = time.time() - t0
        if slept < delay:
            time.sleep(delay - slept)


# --------------------------------------------------------------------- input

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


class MOUSEINPUT(Structure):
    _fields_ = [("dx", c_long), ("dy", c_long), ("mouseData", c_ulong),
                ("dwFlags", c_ulong), ("time", c_ulong),
                ("dwExtraInfo", POINTER(c_ulong))]


class KEYBDINPUT(Structure):
    _fields_ = [("wVk", c_ushort), ("wScan", c_ushort), ("dwFlags", c_ulong),
                ("time", c_ulong), ("dwExtraInfo", POINTER(c_ulong))]


class _IU(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(Structure):
    _fields_ = [("type", c_ulong), ("u", _IU)]


def _send(*inputs):
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    user32.SendInput(n, arr, ctypes.sizeof(INPUT))


def _mouse(flags, dx=0, dy=0, data=0):
    return INPUT(type=INPUT_MOUSE,
                 u=_IU(mi=MOUSEINPUT(dx, dy, data, flags, 0, None)))


def move_to(mon_id, nx, ny):
    """nx/ny are 0..1 within the given monitor."""
    m = monitor(mon_id)
    x = int(m["x"] + max(0.0, min(1.0, nx)) * m["w"])
    y = int(m["y"] + max(0.0, min(1.0, ny)) * m["h"])
    user32.SetCursorPos(x, y)
    return {"x": x, "y": y}


def click(mon_id, nx, ny, button="left", double=False):
    pos = move_to(mon_id, nx, ny)
    time.sleep(0.012)
    down, up = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }.get(button, (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP))
    _send(_mouse(down), _mouse(up))
    if double:
        time.sleep(0.05)
        _send(_mouse(down), _mouse(up))
    return pos


def drag(mon_id, x1, y1, x2, y2, steps=18):
    move_to(mon_id, x1, y1)
    time.sleep(0.02)
    _send(_mouse(MOUSEEVENTF_LEFTDOWN))
    for i in range(1, steps + 1):
        t = i / steps
        move_to(mon_id, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
        time.sleep(0.012)
    _send(_mouse(MOUSEEVENTF_LEFTUP))
    return {"ok": True}


def move_relative(dx, dy):
    """Trackpad mode - nudge the cursor from wherever it is."""
    pt = wt.POINT()
    user32.GetCursorPos(byref(pt))
    user32.SetCursorPos(int(pt.x + dx), int(pt.y + dy))
    return {"x": int(pt.x + dx), "y": int(pt.y + dy)}


def scroll(amount):
    _send(_mouse(MOUSEEVENTF_WHEEL, data=int(amount)))
    return {"ok": True}


def type_text(text):
    """Unicode-accurate typing - works for any character, no layout guessing."""
    ins = []
    for ch in text[:2000]:
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            ins.append(INPUT(type=INPUT_KEYBOARD,
                             u=_IU(ki=KEYBDINPUT(0, ord(ch), flags, 0, None))))
        if len(ins) >= 80:
            _send(*ins)
            ins = []
            time.sleep(0.004)
    if ins:
        _send(*ins)
    return {"typed": len(text)}


VK = {
    "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "backspace": 0x08,
    "delete": 0x2E, "space": 0x20, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "win": 0x5B, "ctrl": 0x11, "alt": 0x12, "shift": 0x10,
    "f4": 0x73, "f5": 0x74, "f11": 0x7A,
    "a": 0x41, "c": 0x43, "v": 0x56, "x": 0x58, "z": 0x5A,
}


def press(name, mods=None):
    """press('v', ['ctrl']) -> Ctrl+V. Unknown names raise."""
    mods = mods or []
    vks = []
    for mname in mods:
        vk = VK.get(mname.lower())
        if vk is None:
            raise ValueError("unknown modifier: %s" % mname)
        vks.append(vk)
    vk = VK.get(name.lower())
    if vk is None:
        raise ValueError("unknown key: %s" % name)

    ins = []
    for m in vks:
        ins.append(INPUT(type=INPUT_KEYBOARD,
                         u=_IU(ki=KEYBDINPUT(m, 0, 0, 0, None))))
    ins.append(INPUT(type=INPUT_KEYBOARD,
                     u=_IU(ki=KEYBDINPUT(vk, 0, 0, 0, None))))
    ins.append(INPUT(type=INPUT_KEYBOARD,
                     u=_IU(ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, None))))
    for m in reversed(vks):
        ins.append(INPUT(type=INPUT_KEYBOARD,
                         u=_IU(ki=KEYBDINPUT(m, 0, KEYEVENTF_KEYUP, 0, None))))
    _send(*ins)
    return {"key": name, "mods": mods}
