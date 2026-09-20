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

from PIL import Image, ImageGrab  # noqa: E402


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
    "low":    {"width": 720,  "jpeg": 42, "fps": 12},
    "medium": {"width": 1000, "jpeg": 55, "fps": 18},
    "high":   {"width": 1440, "jpeg": 66, "fps": 24},
}

# ---- GDI capture ------------------------------------------------------------
# ImageGrab.grab was measured at ~50ms for one 1920x1080 frame on this machine,
# and scaling it down afterwards cost another 14-21ms. Both are avoidable:
# StretchBlt copies the screen into an already-scaled bitmap in one step, on
# a DC and a bitmap that are reused between frames.
#
# Falls back to ImageGrab if any of it fails, because a slow preview beats no
# preview.

gdi32 = ctypes.windll.gdi32

SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000        # include layered windows
COLORONCOLOR = 3               # StretchBlt: drop pixels
HALFTONE = 4                   # StretchBlt: averages pixels; the good one
DIB_RGB_COLORS = 0

# How the full-resolution screen becomes a small frame. Measured on the same
# screen content by diag/capbench3.py, 1920x1080 -> 1000 wide, quality 55:
#
#   StretchBlt HALFTONE      21 ms   26 kB
#   StretchBlt COLORONCOLOR  14 ms   27 kB   <- faster, but aliased and no
#                                               smaller, so nothing is gained
#   BitBlt + Pillow BILINEAR 33 ms   23 kB   <- 12% fewer bytes for 12ms more
#
# HALFTONE wins: GDI does the scaling in one step, and it compresses as well
# as a proper resample. Flip SCALE_IN_PIL if a future machine disagrees -
# the benchmark measures all three.
SCALE_IN_PIL = False


class BITMAPINFOHEADER(Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", c_long), ("biHeight", c_long),
                ("biPlanes", c_ushort), ("biBitCount", c_ushort),
                ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", c_long), ("biYPelsPerMeter", c_long),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class BITMAPINFO(Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


# Handles are pointer-sized. Left to ctypes' default c_int these truncate on
# 64-bit Windows and the calls fail in ways that look like a black screen.
for _fn, _args, _res in (
    (gdi32.CreateCompatibleDC, [wt.HDC], wt.HDC),
    (gdi32.CreateCompatibleBitmap, [wt.HDC, c_int, c_int], wt.HBITMAP),
    (gdi32.SelectObject, [wt.HDC, wt.HGDIOBJ], wt.HGDIOBJ),
    (gdi32.DeleteObject, [wt.HGDIOBJ], wt.BOOL),
    (gdi32.DeleteDC, [wt.HDC], wt.BOOL),
    (gdi32.SetStretchBltMode, [wt.HDC, c_int], c_int),
    (gdi32.StretchBlt, [wt.HDC, c_int, c_int, c_int, c_int,
                        wt.HDC, c_int, c_int, c_int, c_int, wt.DWORD], wt.BOOL),
    (gdi32.GetDIBits, [wt.HDC, wt.HBITMAP, c_uint, c_uint, ctypes.c_void_p,
                       POINTER(BITMAPINFO), c_uint], c_int),
    (gdi32.CreateDCW, [wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_void_p],
     wt.HDC),
):
    _fn.argtypes, _fn.restype = _args, _res


class _Grabber:
    """Keeps the screen DC and the scratch bitmap alive between frames."""

    def __init__(self):
        self.lock = threading.Lock()
        self.screen = None
        self.size = None
        self.mem = self.bmp = self.old = None
        self.buf = None
        self.info = None
        self.broken = False
        # Overridable so diag/capbench3.py can measure the alternatives
        # rather than take the comment above on trust.
        self.pil_scale = SCALE_IN_PIL
        self.stretch = HALFTONE

    def _screen_dc(self):
        if self.screen is None:
            # "DISPLAY" spans the whole virtual desktop, so source coordinates
            # are virtual-screen coordinates and a second monitor works.
            self.screen = gdi32.CreateDCW("DISPLAY", None, None, None)
        return self.screen

    def _target(self, w, h):
        if self.size == (w, h):
            return
        self._drop_target()
        src = self._screen_dc()
        self.mem = gdi32.CreateCompatibleDC(src)
        self.bmp = gdi32.CreateCompatibleBitmap(src, w, h)
        self.old = gdi32.SelectObject(self.mem, self.bmp)
        gdi32.SetStretchBltMode(self.mem, self.stretch)
        self.buf = ctypes.create_string_buffer(w * h * 4)
        self.info = BITMAPINFO()
        hdr = self.info.bmiHeader
        hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        hdr.biWidth = w
        hdr.biHeight = -h                # negative = top-down rows
        hdr.biPlanes = 1
        hdr.biBitCount = 32
        hdr.biCompression = 0            # BI_RGB
        self.size = (w, h)

    def _drop_target(self):
        try:
            if self.mem and self.old:
                gdi32.SelectObject(self.mem, self.old)
            if self.bmp:
                gdi32.DeleteObject(self.bmp)
            if self.mem:
                gdi32.DeleteDC(self.mem)
        except Exception:
            pass
        self.mem = self.bmp = self.old = self.buf = self.info = None
        self.size = None

    def frame(self, m, w, h):
        """One RGB image of monitor `m`, already scaled to w x h."""
        if self.broken:
            return self._fallback(m, w, h)
        try:
            with self.lock:
                src = self._screen_dc()
                if not src:
                    raise RuntimeError("no screen DC")
                # Blit at full resolution and let Pillow do the scaling, so
                # the result is a clean resample rather than GDI's dither.
                bw, bh = (m["w"], m["h"]) if self.pil_scale else (w, h)
                self._target(bw, bh)
                if not gdi32.StretchBlt(self.mem, 0, 0, bw, bh,
                                        src, m["x"], m["y"], m["w"], m["h"],
                                        SRCCOPY | CAPTUREBLT):
                    raise RuntimeError("StretchBlt failed")
                if not gdi32.GetDIBits(self.mem, self.bmp, 0, bh, self.buf,
                                       byref(self.info), DIB_RGB_COLORS):
                    raise RuntimeError("GetDIBits failed")
                # BGRX straight out of the DIB - no conversion pass.
                img = Image.frombuffer("RGB", (bw, bh), self.buf,
                                       "raw", "BGRX", 0, 1)
                if (bw, bh) != (w, h):
                    # An exact halving is much cheaper than a general resample,
                    # so get most of the way there with reduce() first.
                    f = min(bw // w, bh // h) if w and h else 1
                    if f >= 2:
                        img = img.reduce(f)
                    img = img.resize((w, h), Image.BILINEAR)
                return img
        except Exception:
            self.broken = True
            self._drop_target()
            try:
                if self.screen:
                    gdi32.DeleteDC(self.screen)
            except Exception:
                pass
            self.screen = None
            return self._fallback(m, w, h)

    @staticmethod
    def _fallback(m, w, h):
        box = (m["x"], m["y"], m["x"] + m["w"], m["y"] + m["h"])
        img = ImageGrab.grab(bbox=box, all_screens=True)
        if img.mode != "RGB":
            img = img.convert("RGB")
        if img.size != (w, h):
            img = img.resize((w, h), Image.BILINEAR)
        return img


_grabber = _Grabber()


def frame_size(m, width):
    """The size a frame of monitor `m` gets scaled to.

    `width` bounds the LONG edge, not the horizontal one. A portrait monitor
    is 1080x1920, and capping its width at 1000 would send a 1000x1778 frame -
    nearly twice the pixels of the landscape screen it was meant to match.

    Even numbers, because JPEG chroma subsampling halves them and an odd
    width costs a padding column.
    """
    w, h = m["w"], m["h"]
    longest = max(w, h)
    if longest > width:
        scale = width / float(longest)
        w = max(2, round(w * scale))
        h = max(2, round(h * scale))
    return (w - (w % 2), h - (h % 2))


def grab_jpeg(mon_id=0, width=1280, jpeg=55, mon=None):
    m = mon or monitor(mon_id)
    w, h = frame_size(m, width)
    img = _grabber.frame(m, w, h)
    buf = io.BytesIO()
    # subsampling=2 is 4:2:0. optimize=False skips a second Huffman pass that
    # costs more time than the bytes it saves at these sizes.
    img.save(buf, "JPEG", quality=jpeg, subsampling=2, optimize=False)
    return buf.getvalue()


def mjpeg_frames(mon_id=0, quality="medium", stop_after=None):
    """Yields (boundary-delimited) multipart frames until the client leaves."""
    q = QUALITY.get(quality, QUALITY["medium"])
    delay = 1.0 / q["fps"]
    started = time.time()
    mon = monitor(mon_id)
    while True:
        t0 = time.time()
        try:
            frame = grab_jpeg(mon_id, q["width"], q["jpeg"], mon=mon)
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


# One table, used by both click() and click_here(), so a button added here
# cannot work in one mode and silently not the other.
_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def click(mon_id, nx, ny, button="left", double=False):
    pos = move_to(mon_id, nx, ny)
    time.sleep(0.012)
    down, up = _BUTTONS.get(button, _BUTTONS["left"])
    _send(_mouse(down), _mouse(up))
    if double:
        time.sleep(0.05)
        _send(_mouse(down), _mouse(up))
    return pos


def click_here(button="left", double=False):
    """Click wherever the cursor already is.

    Trackpad mode drives the cursor with move_relative and then taps; using
    click() for that tap would first SetCursorPos back to a screen coordinate
    the phone guessed, throwing away the positioning the user just did.
    """
    down, up = _BUTTONS.get(button, _BUTTONS["left"])
    _send(_mouse(down), _mouse(up))
    if double:
        time.sleep(0.05)
        _send(_mouse(down), _mouse(up))
    pt = wt.POINT()
    user32.GetCursorPos(byref(pt))
    return {"x": int(pt.x), "y": int(pt.y)}


def cursor():
    """Where the cursor is now, and which monitor it is on."""
    pt = wt.POINT()
    user32.GetCursorPos(byref(pt))
    for m in monitors():
        if (m["x"] <= pt.x < m["x"] + m["w"]
                and m["y"] <= pt.y < m["y"] + m["h"]):
            return {"x": int(pt.x), "y": int(pt.y), "mon": m["id"],
                    "nx": (pt.x - m["x"]) / max(1, m["w"]),
                    "ny": (pt.y - m["y"]) / max(1, m["h"])}
    return {"x": int(pt.x), "y": int(pt.y), "mon": 0, "nx": 0.0, "ny": 0.0}


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
