# sysctl.py - Windows audio + system control for the phone remote.
# Pure ctypes/COM, no third-party deps required.
#
# All COM work is funnelled through ONE dedicated worker thread (see audio_call)
# because MMDevice/AudioEndpointVolume objects are apartment-bound and the HTTP
# server is multi-threaded. Serialising also means two phone taps can never
# interleave a get/set pair.

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import threading
import subprocess
import time
from ctypes import (
    POINTER, Structure, WINFUNCTYPE, byref, c_float, c_int, c_ubyte,
    c_ulong, c_ushort, c_void_p, c_wchar_p,
)

ole32 = ctypes.windll.ole32
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

CLSCTX_ALL = 23
COINIT_APARTMENTTHREADED = 0x2

eRender = 0
eConsole = 0
eMultimedia = 1
eCommunications = 2
DEVICE_STATE_ACTIVE = 0x1


class GUID(Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", c_ushort),
                ("Data3", c_ushort), ("Data4", c_ubyte * 8)]

    def __init__(self, s=None):
        super().__init__()
        if s:
            ole32.CLSIDFromString(c_wchar_p(s), byref(self))


CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IAudioEndpointVolume = GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")
CLSID_PolicyConfigClient = GUID("{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}")
IID_IPolicyConfig = GUID("{F8679F50-850A-41CF-9C72-430F290290C8}")

PKEY_FriendlyName_fmtid = GUID("{A45C254E-DF1C-4EFD-8020-67D146A850E0}")
PKEY_FriendlyName_pid = 14


class PROPERTYKEY(Structure):
    _fields_ = [("fmtid", GUID), ("pid", c_ulong)]


class PROPVARIANT(Structure):
    # vt + 3 reserved words = 8 bytes, then the union. On x64 the pointer
    # member we care about (pwszVal) sits at offset 8.
    _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort),
                ("r3", c_ushort), ("data", c_void_p * 2)]


def _m(ptr, index, restype, *argtypes):
    """Bind vtable slot `index` on COM interface pointer `ptr`."""
    vtbl = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
    return WINFUNCTYPE(restype, c_void_p, *argtypes)(vtbl[index])


def _chk(hr, what):
    if hr != 0:
        raise OSError("%s failed: 0x%08X" % (what, hr & 0xFFFFFFFF))


def _release(ptr):
    if ptr:
        try:
            _m(ptr, 2, c_ulong)(ptr)
        except Exception:
            pass


# ---------------------------------------------------------------- enumerator

def _enumerator():
    p = c_void_p()
    _chk(ole32.CoCreateInstance(byref(CLSID_MMDeviceEnumerator), None,
                                CLSCTX_ALL, byref(IID_IMMDeviceEnumerator),
                                byref(p)), "CoCreateInstance(MMDeviceEnumerator)")
    return p


def _default_device(enum, role=eConsole):
    dev = c_void_p()
    _chk(_m(enum, 4, c_int, c_int, c_int, POINTER(c_void_p))(
        enum, eRender, role, byref(dev)), "GetDefaultAudioEndpoint")
    return dev


def _active_devices(enum):
    col = c_void_p()
    _chk(_m(enum, 3, c_int, c_int, c_ulong, POINTER(c_void_p))(
        enum, eRender, DEVICE_STATE_ACTIVE, byref(col)), "EnumAudioEndpoints")
    n = ctypes.c_uint()
    _chk(_m(col, 3, c_int, POINTER(ctypes.c_uint))(col, byref(n)), "GetCount")
    out = []
    for i in range(n.value):
        d = c_void_p()
        if _m(col, 4, c_int, ctypes.c_uint, POINTER(c_void_p))(col, i, byref(d)) == 0:
            out.append(d)
    _release(col)
    return out


def _dev_id(dev):
    s = c_wchar_p()
    _chk(_m(dev, 5, c_int, POINTER(c_wchar_p))(dev, byref(s)), "GetId")
    val = s.value
    ole32.CoTaskMemFree(s)
    return val


def _dev_name(dev):
    store = c_void_p()
    if _m(dev, 4, c_int, c_ulong, POINTER(c_void_p))(dev, 0, byref(store)) != 0:
        return "(unknown)"
    key = PROPERTYKEY()
    key.fmtid = PKEY_FriendlyName_fmtid
    key.pid = PKEY_FriendlyName_pid
    pv = PROPVARIANT()
    name = "(unknown)"
    if _m(store, 5, c_int, POINTER(PROPERTYKEY), POINTER(PROPVARIANT))(
            store, byref(key), byref(pv)) == 0:
        if pv.vt == 31 and pv.data[0]:
            name = ctypes.cast(pv.data[0], c_wchar_p).value
        ole32.PropVariantClear(byref(pv))
    _release(store)
    return name


def _endpoint_volume(dev):
    ep = c_void_p()
    _chk(_m(dev, 3, c_int, POINTER(GUID), c_ulong, c_void_p, POINTER(c_void_p))(
        dev, byref(IID_IAudioEndpointVolume), CLSCTX_ALL, None, byref(ep)),
        "Activate(IAudioEndpointVolume)")
    return ep


# ---------------------------------------------------------------- operations
# Each of these runs ON the audio worker thread.

def _op_status():
    enum = _enumerator()
    try:
        dev = _default_device(enum)
        ep = _endpoint_volume(dev)
        lvl = c_float()
        _chk(_m(ep, 9, c_int, POINTER(c_float))(ep, byref(lvl)),
             "GetMasterVolumeLevelScalar")
        mute = c_int()
        _chk(_m(ep, 15, c_int, POINTER(c_int))(ep, byref(mute)), "GetMute")
        name = _dev_name(dev)
        did = _dev_id(dev)
        _release(ep)
        _release(dev)
        return {"volume": round(lvl.value * 100), "muted": bool(mute.value),
                "device": name, "deviceId": did}
    finally:
        _release(enum)


def _op_set_volume(pct):
    pct = max(0, min(100, int(pct)))
    enum = _enumerator()
    try:
        dev = _default_device(enum)
        ep = _endpoint_volume(dev)
        _chk(_m(ep, 7, c_int, c_float, POINTER(GUID))(ep, c_float(pct / 100.0), None),
             "SetMasterVolumeLevelScalar")
        _release(ep)
        _release(dev)
        return {"volume": pct}
    finally:
        _release(enum)


def _op_set_mute(flag, every=True):
    """Mute is applied to EVERY active render endpoint, not just the default,
    because 'gf is asleep' has to mean silence from all six of them."""
    enum = _enumerator()
    try:
        devs = _active_devices(enum) if every else [_default_device(enum)]
        n = 0
        for d in devs:
            try:
                ep = _endpoint_volume(d)
                if _m(ep, 14, c_int, c_int, POINTER(GUID))(ep, 1 if flag else 0, None) == 0:
                    n += 1
                _release(ep)
            except Exception:
                pass
            _release(d)
        return {"muted": bool(flag), "endpoints": n}
    finally:
        _release(enum)


def _op_devices():
    enum = _enumerator()
    try:
        cur = None
        try:
            d = _default_device(enum)
            cur = _dev_id(d)
            _release(d)
        except Exception:
            pass
        out = []
        for d in _active_devices(enum):
            try:
                out.append({"id": _dev_id(d), "name": _dev_name(d)})
            except Exception:
                pass
            _release(d)
        for o in out:
            o["default"] = (o["id"] == cur)
        return {"devices": out}
    finally:
        _release(enum)


def _op_set_default(device_id):
    pc = c_void_p()
    _chk(ole32.CoCreateInstance(byref(CLSID_PolicyConfigClient), None,
                                CLSCTX_ALL, byref(IID_IPolicyConfig), byref(pc)),
         "CoCreateInstance(PolicyConfigClient)")
    try:
        fn = _m(pc, 13, c_int, c_wchar_p, c_int)
        for role in (eConsole, eMultimedia, eCommunications):
            _chk(fn(pc, c_wchar_p(device_id), role), "SetDefaultEndpoint")
        return {"ok": True}
    finally:
        _release(pc)


# ------------------------------------------------------- audio worker thread

_q = queue.Queue()
_worker = None
_worker_lock = threading.Lock()


def _pump():
    ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    while True:
        fn, args, box = _q.get()
        try:
            box["result"] = fn(*args)
        except Exception as e:
            box["error"] = str(e)
        box["event"].set()


def audio_call(fn, *args, timeout=6.0):
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_pump, daemon=True, name="audio")
            _worker.start()
    box = {"event": threading.Event()}
    _q.put((fn, args, box))
    if not box["event"].wait(timeout):
        raise TimeoutError("audio worker timed out")
    if "error" in box:
        raise OSError(box["error"])
    return box["result"]


# Public audio API -----------------------------------------------------------

def status():
    return audio_call(_op_status)


def set_volume(pct):
    return audio_call(_op_set_volume, pct)


def set_mute(flag):
    return audio_call(_op_set_mute, flag)


def devices():
    return audio_call(_op_devices)


def set_default_device(device_id):
    return audio_call(_op_set_default, device_id)


# ----------------------------------------------------------------- input/sys

VK = {
    "playpause": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2,
    "volup": 0xAF, "voldown": 0xAE, "volmute": 0xAD,
    "space": 0x20, "enter": 0x0D, "esc": 0x1B,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
}
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001


def tap_key(name):
    vk = VK.get(name)
    if vk is None:
        raise ValueError("unknown key: %s" % name)
    user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY, 0)
    time.sleep(0.02)
    user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
    return {"key": name}


def foreground_app():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return {"title": "", "process": ""}
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, byref(pid))
    name = ""
    h = kernel32.OpenProcess(0x1000, False, pid.value)
    if h:
        size = wt.DWORD(260)
        pbuf = ctypes.create_unicode_buffer(260)
        if kernel32.QueryFullProcessImageNameW(h, 0, pbuf, byref(size)):
            name = pbuf.value.rsplit("\\", 1)[-1]
        kernel32.CloseHandle(h)
    return {"title": buf.value, "process": name, "pid": pid.value}


class MEMORYSTATUSEX(Structure):
    _fields_ = [("dwLength", c_ulong), ("dwMemoryLoad", c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def memory_info():
    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(m)
    kernel32.GlobalMemoryStatusEx(byref(m))
    gb = 1024.0 ** 3
    return {"percent": m.dwMemoryLoad,
            "usedGb": round((m.ullTotalPhys - m.ullAvailPhys) / gb, 1),
            "totalGb": round(m.ullTotalPhys / gb, 1)}


# ------------------------------------------------------- live component stats
# Everything a stat tile can show: CPU, RAM, disk, GPU, temp, battery. Read
# with plain syscalls except the GPU, which needs nvidia-smi - so CPU and GPU
# are sampled on a background thread and read from a cache, keeping /api/state
# instant no matter how often the phone polls it.

class _FILETIME(Structure):
    _fields_ = [("lo", wt.DWORD), ("hi", wt.DWORD)]


def _ft(ft):
    return (ft.hi << 32) | ft.lo


def _cpu_times():
    idle, kern, usr = _FILETIME(), _FILETIME(), _FILETIME()
    kernel32.GetSystemTimes(byref(idle), byref(kern), byref(usr))
    # kernel time already includes idle, so total busy = (kernel+user)-idle.
    return _ft(idle), _ft(kern) + _ft(usr)


def _read_gpu():
    """utilization / memory / temp / name for an NVIDIA card, or None.

    nvidia-smi is the only dependency-free way to get this, and it is the card
    Cody actually has. No NVIDIA -> None, and the GPU/temp tiles show a dash.
    """
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,"
             "memory.total,temperature.gpu,name",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
            creationflags=0x08000000).stdout.strip()
        if not out:
            return None
        p = [x.strip() for x in out.splitlines()[0].split(",")]
        mu, mt = float(p[1]), float(p[2])
        # "NVIDIA GeForce RTX 3060" -> "RTX 3060" so it fits a small tile.
        name = p[4].replace("NVIDIA GeForce ", "").replace("NVIDIA ", "")
        return {"pct": int(float(p[0])),
                "memPct": round(mu / mt * 100) if mt else 0,
                "tempC": int(float(p[3])), "name": name}
    except Exception:
        return None


_stat_lock = threading.Lock()
_stat_cache = {"cpu": None, "gpu": None}
_stat_thread = None


def _stat_loop():
    prev = _cpu_times()
    i = 0
    while True:
        time.sleep(1.5)
        try:
            idle, total = _cpu_times()
            dt = total - prev[1]
            cpu = 0 if dt <= 0 else max(0, min(100,
                  round((1 - (idle - prev[0]) / dt) * 100)))
            prev = (idle, total)
        except Exception:
            cpu = None
        # GPU only every other tick - nvidia-smi is a process spawn.
        gpu = _read_gpu() if (i % 2 == 0) else _stat_cache.get("gpu")
        i += 1
        with _stat_lock:
            _stat_cache["cpu"] = cpu
            _stat_cache["gpu"] = gpu


def _ensure_sampler():
    global _stat_thread
    if _stat_thread is None or not _stat_thread.is_alive():
        _stat_thread = threading.Thread(target=_stat_loop, daemon=True,
                                        name="stats")
        _stat_thread.start()


def disk_info(drive=None):
    if drive is None:
        drive = (os.environ.get("SystemDrive", "C:") + "\\")
    free = ctypes.c_ulonglong()
    total = ctypes.c_ulonglong()
    tfree = ctypes.c_ulonglong()
    kernel32.GetDiskFreeSpaceExW(c_wchar_p(drive), byref(free),
                                 byref(total), byref(tfree))
    gb = 1024.0 ** 3
    t = total.value / gb
    f = tfree.value / gb
    return {"drive": drive.rstrip("\\"), "freeGb": round(f),
            "totalGb": round(t), "pct": round((t - f) / t * 100) if t else 0}


class SYSTEM_POWER_STATUS(Structure):
    _fields_ = [("ACLineStatus", c_ubyte), ("BatteryFlag", c_ubyte),
                ("BatteryLifePercent", c_ubyte), ("SystemStatusFlag", c_ubyte),
                ("BatteryLifeTime", wt.DWORD), ("BatteryFullLifeTime", wt.DWORD)]


def battery_info():
    """None on a desktop (no battery), else percent + charging."""
    s = SYSTEM_POWER_STATUS()
    if not kernel32.GetSystemPowerStatus(byref(s)):
        return None
    if s.BatteryFlag == 128 or s.BatteryLifePercent == 255:
        return None
    return {"pct": int(s.BatteryLifePercent),
            "charging": s.ACLineStatus == 1}


def _na(label):
    return {"label": label, "big": "—", "unit": "", "pct": 0, "na": True}


def system_stats():
    """One dict keyed by what a stat tile can name (cpu/ram/disk/gpu/temp/
    battery). Each entry is {label, big, unit, pct} so the phone renders any
    of them the same way - a number, a small unit, and a bar driven by pct."""
    _ensure_sampler()
    with _stat_lock:
        cpu = _stat_cache.get("cpu")
        gpu = _stat_cache.get("gpu")
    mem = memory_info()
    disk = disk_info()
    bat = battery_info()

    out = {
        "cpu": {"label": "CPU", "big": "—" if cpu is None else str(cpu),
                "unit": "%", "pct": cpu or 0},
        "ram": {"label": "RAM", "big": str(mem["usedGb"]),
                "unit": "/ %s GB" % mem["totalGb"], "pct": mem["percent"]},
        "disk": {"label": "Disk " + disk["drive"], "big": str(disk["freeGb"]),
                 "unit": "GB free", "pct": disk["pct"]},
    }
    if gpu:
        out["gpu"] = {"label": gpu["name"] or "GPU", "big": str(gpu["pct"]),
                      "unit": "%", "pct": gpu["pct"]}
        out["temp"] = {"label": "GPU temp", "big": str(gpu["tempC"]),
                       "unit": "°C", "pct": min(100, gpu["tempC"])}
    else:
        out["gpu"] = _na("GPU")
        out["temp"] = _na("Temp")
    if bat:
        out["battery"] = {"label": "Battery", "big": str(bat["pct"]),
                          "unit": "% ⚡" if bat["charging"] else "%",
                          "pct": bat["pct"], "charging": bat["charging"]}
    else:
        out["battery"] = dict(_na("Battery"), unit="no battery")
    return out


# ---------------------------------------------------------------- clipboard
# Text only, both directions. Pointer-sized handles need explicit restypes or
# ctypes truncates them to 32 bits on x64 and the calls quietly fail.

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

kernel32.GlobalAlloc.restype = c_void_p
kernel32.GlobalAlloc.argtypes = [c_ulong, ctypes.c_size_t]
kernel32.GlobalLock.restype = c_void_p
kernel32.GlobalLock.argtypes = [c_void_p]
kernel32.GlobalUnlock.argtypes = [c_void_p]
user32.GetClipboardData.restype = c_void_p
user32.GetClipboardData.argtypes = [c_ulong]
user32.SetClipboardData.restype = c_void_p
user32.SetClipboardData.argtypes = [c_ulong, c_void_p]


def _open_clipboard(tries=5):
    for _ in range(tries):
        if user32.OpenClipboard(0):
            return True
        time.sleep(0.03)     # another app may hold it for an instant
    return False


def get_clipboard_text():
    if not _open_clipboard():
        return ""
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = kernel32.GlobalLock(h)
        if not p:
            return ""
        try:
            return ctypes.c_wchar_p(p).value or ""
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text):
    text = str(text)
    if not _open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        buf = ctypes.create_unicode_buffer(text)     # includes the NUL
        size = ctypes.sizeof(buf)
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not h:
            return False
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, buf, size)
        kernel32.GlobalUnlock(h)
        # SetClipboardData takes ownership of h - do not free it.
        user32.SetClipboardData(CF_UNICODETEXT, h)
        return True
    finally:
        user32.CloseClipboard()


# --------------------------------------------------------------- now playing
# Windows exposes the current media (Spotify, a browser, a game) through the
# System Media Transport Controls. There is no ctypes route to it - it is a
# WinRT async API - so we borrow Windows PowerShell's WinRT projection through
# a tiny script. pwsh (7+) dropped WinRT, so this must be "powershell". Read
# is cached for a couple of seconds so polling /api/state never spawns a
# storm of shells.

PS_NOWPLAYING = r"""
$ErrorActionPreference='Stop'
try {
 Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
 $m = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
   $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
   $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
 function Await($o,$t){ $x=$m.MakeGenericMethod($t).Invoke($null,@($o));
   $x.Wait(-1)|Out-Null; $x.Result }
 [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager,Windows.Media.Control,ContentType=WindowsRuntime]|Out-Null
 $mgr = Await ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
 $s = $mgr.GetCurrentSession()
 if($s){
  $i=$s.GetPlaybackInfo()
  $p = Await ($s.GetMediaPropertiesAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties])
  [pscustomobject]@{title=$p.Title;artist=$p.Artist;album=$p.AlbumTitle;app=$s.SourceAppUserModelId;status=[int]$i.PlaybackStatus} | ConvertTo-Json -Compress
 } else { '{}' }
} catch { '{}' }
"""

_np_lock = threading.Lock()
_np = {"at": 0.0, "val": None}


def _read_now_playing():
    b64 = base64.b64encode(PS_NOWPLAYING.encode("utf-16-le")).decode()
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", b64],
            capture_output=True, text=True, timeout=8,
            creationflags=0x08000000).stdout.strip()
        d = json.loads(out or "{}")
    except Exception:
        return None
    if not d or not d.get("title"):
        return None
    status = d.get("status")           # 4 = Playing, 5 = Paused
    return {"title": d.get("title") or "", "artist": d.get("artist") or "",
            "album": d.get("album") or "", "app": d.get("app") or "",
            "playing": status == 4, "status": status}


def now_playing(ttl=3.0):
    now = time.time()
    with _np_lock:
        if now - _np["at"] < ttl:
            return _np["val"]
    val = _read_now_playing()
    with _np_lock:
        _np["at"] = time.time()
        _np["val"] = val
    return val


# ------------------------------------------------------- running apps / tasks

WNDENUMPROC = WINFUNCTYPE(c_int, wt.HWND, wt.LPARAM)
_SKIP_PROC = {"applicationframehost.exe", "textinputhost.exe",
              "systemsettings.exe", "pythonw.exe", "python.exe",
              "shellexperiencehost.exe", "searchhost.exe"}


def running_windows():
    """Visible, titled top-level windows - i.e. the apps a person would think
    of as 'open' - as {pid, process, title}, one row per process."""
    out, seen = [], set()

    def cb(hwnd, lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return 1
            n = user32.GetWindowTextLengthW(hwnd)
            if not n:
                return 1
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value
            if not title or title == "Program Manager":
                return 1
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, byref(pid))
            if pid.value in seen:
                return 1
            name = ""
            h = kernel32.OpenProcess(0x1000, False, pid.value)
            if h:
                size = wt.DWORD(260)
                pbuf = ctypes.create_unicode_buffer(260)
                if kernel32.QueryFullProcessImageNameW(h, 0, pbuf, byref(size)):
                    name = pbuf.value.rsplit("\\", 1)[-1]
                kernel32.CloseHandle(h)
            if name.lower() in _SKIP_PROC:
                return 1
            seen.add(pid.value)
            out.append({"pid": pid.value, "process": name,
                        "title": title[:90]})
        except Exception:
            pass
        return 1

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    out.sort(key=lambda w: (w["process"].lower(), w["title"].lower()))
    return out[:60]


def end_task(pid):
    """Close an app by pid. Refuses this very process, so the phone can never
    make the remote end itself."""
    try:
        pid = int(pid)
    except Exception:
        return {"ok": False, "error": "bad pid"}
    if pid == os.getpid():
        return {"ok": False, "error": "cannot end the remote itself"}
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=10,
                       creationflags=0x08000000)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def lock_workstation():
    user32.LockWorkStation()
    return {"ok": True}


def screen_off():
    HWND_BROADCAST = 0xFFFF
    WM_SYSCOMMAND = 0x0112
    SC_MONITORPOWER = 0xF170
    user32.SendMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, 2)
    return {"ok": True}


def run_detached(cmd):
    """Fire and forget. CREATE_NO_WINDOW so nothing flashes on his screen."""
    subprocess.Popen(cmd, shell=isinstance(cmd, str),
                     creationflags=0x08000000 | 0x00000008,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True)
    return {"ok": True}


# ------------------------------------------------------------- volume keeper
# Cody's standing complaint: volume jumps to 100% when apps open / on focus
# change. This pins it. When armed, any change made by anything other than
# this remote gets stomped back to the target within ~200ms.

class VolumeKeeper:
    def __init__(self):
        self.enabled = False
        self.target = None
        self.corrections = 0
        self.last_correction = None
        self._stop = threading.Event()
        self._thread = None

    def arm(self, pct):
        self.target = max(0, min(100, int(pct)))
        self.enabled = True
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="volkeeper")
            self._thread.start()
        return self.info()

    def disarm(self):
        self.enabled = False
        return self.info()

    def _loop(self):
        while not self._stop.is_set():
            if self.enabled and self.target is not None:
                try:
                    st = status()
                    if abs(st["volume"] - self.target) >= 2:
                        set_volume(self.target)
                        self.corrections += 1
                        self.last_correction = {
                            "at": time.strftime("%H:%M:%S"),
                            "was": st["volume"], "to": self.target,
                            "app": foreground_app().get("process"),
                        }
                except Exception:
                    pass
            self._stop.wait(0.2)

    def info(self):
        return {"enabled": self.enabled, "target": self.target,
                "corrections": self.corrections,
                "last": self.last_correction}


keeper = VolumeKeeper()
