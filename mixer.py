"""Per-app volume (the Windows volume mixer) and the microphone mute.

Each app that makes sound has an audio session on an output device; its own
volume is a separate knob from the device's master volume. This lists them
grouped by program and sets them. Same COM plumbing and the same single
audio worker thread as sysctl (COM objects here are apartment-bound).
"""
import ctypes
import os
from ctypes import POINTER, byref, c_float, c_int, c_ulong, c_void_p, c_wchar_p

import sysctl
from sysctl import GUID, _m, _chk, _release, _enumerator, _active_devices, _dev_id, \
    _dev_name, ole32, CLSCTX_ALL

IID_IAudioSessionManager2 = GUID("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
IID_IAudioSessionControl2 = GUID("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")
# Not ISimpleAudioVolume: on current Windows the session objects answer
# E_NOINTERFACE for it, while the per-channel interface works everywhere.
IID_IChannelAudioVolume = GUID("{1C158861-B533-4B30-B1CF-E853E51C59B8}")
IID_IAudioEndpointVolume = sysctl.IID_IAudioEndpointVolume

eCapture = 1
DEVICE_STATE_ACTIVE = 0x1

kernel32 = ctypes.windll.kernel32
_names = {}


def _qi(ptr, iid):
    out = c_void_p()
    if _m(ptr, 0, c_int, POINTER(GUID), POINTER(c_void_p))(ptr, byref(iid), byref(out)) != 0:
        return None
    return out


def _exe(pid):
    if pid in _names:
        return _names[pid]
    name = ""
    h = kernel32.OpenProcess(0x1000, False, pid)
    if h:
        size = ctypes.c_ulong(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, byref(size)):
            name = buf.value
        kernel32.CloseHandle(h)
    if len(_names) > 500:
        _names.clear()
    _names[pid] = name
    return name


def _sessions():
    """[(exe path, pid, IChannelAudioVolume, device name)] - caller releases."""
    out = []
    enum = _enumerator()
    try:
        for dev in _active_devices(enum):
            try:
                dname = _dev_name(dev)
                mgr = c_void_p()
                if _m(dev, 3, c_int, POINTER(GUID), c_ulong, c_void_p, POINTER(c_void_p))(
                        dev, byref(IID_IAudioSessionManager2), CLSCTX_ALL, None, byref(mgr)) != 0:
                    continue
                se = c_void_p()
                if _m(mgr, 5, c_int, POINTER(c_void_p))(mgr, byref(se)) == 0:
                    n = c_int()
                    _m(se, 3, c_int, POINTER(c_int))(se, byref(n))
                    for i in range(n.value):
                        ctl = c_void_p()
                        if _m(se, 4, c_int, c_int, POINTER(c_void_p))(se, i, byref(ctl)) != 0:
                            continue
                        c2 = _qi(ctl, IID_IAudioSessionControl2)
                        vol = _qi(ctl, IID_IChannelAudioVolume)
                        _release(ctl)
                        if not c2 or not vol:
                            _release(c2); _release(vol)
                            continue
                        state = c_int()
                        _m(c2, 3, c_int, POINTER(c_int))(c2, byref(state))
                        system = _m(c2, 15, c_int)(c2) == 0          # S_OK = system sounds
                        pid = c_ulong()
                        _m(c2, 14, c_int, POINTER(c_ulong))(c2, byref(pid))
                        _release(c2)
                        # 2 = expired (the app closed); system sounds have no app;
                        # a process we can't name is a service, not an app.
                        if state.value == 2 or system or not pid.value or not _exe(pid.value):
                            _release(vol)
                            continue
                        out.append((_exe(pid.value), pid.value, vol, dname))
                    _release(se)
                _release(mgr)
            finally:
                _release(dev)
    finally:
        _release(enum)
    return out


def _label(path):
    try:
        import media
        d = media._file_description(path)
        if d:
            return d
    except Exception:
        pass
    return os.path.splitext(os.path.basename(path))[0].title() or "App"


def _get(vol):
    n = ctypes.c_uint()
    _m(vol, 3, c_int, POINTER(ctypes.c_uint))(vol, byref(n))
    vals = []
    for i in range(n.value):
        f = c_float()
        if _m(vol, 5, c_int, ctypes.c_uint, POINTER(c_float))(vol, i, byref(f)) == 0:
            vals.append(f.value)
    return max(vals) if vals else 1.0


def _set(vol, level):
    n = ctypes.c_uint()
    _m(vol, 3, c_int, POINTER(ctypes.c_uint))(vol, byref(n))
    for i in range(n.value):
        _m(vol, 4, c_int, ctypes.c_uint, c_float, POINTER(GUID))(vol, i, c_float(level), None)


_muted = {}          # app -> the level it had before we muted it


def _op_apps():
    groups = {}
    for path, pid, vol, dname in _sessions():
        key = os.path.basename(path).lower() or str(pid)
        lvl = _get(vol)
        _release(vol)
        g = groups.setdefault(key, {"app": key, "path": path, "volume": 0, "muted": False, "devices": []})
        g["volume"] = max(g["volume"], round(lvl * 100))
        if dname not in g["devices"]:
            g["devices"].append(dname)
    out = []
    for g in groups.values():
        if g["app"] in ("aetherremote.exe",):
            continue
        g["muted"] = g["app"] in _muted and g["volume"] == 0
        g["name"] = _label(g["path"])
        out.append(g)
    out.sort(key=lambda g: g["name"].lower())
    return out


def _op_set(app, pct=None, mute=None):
    app = app.lower()
    n = 0
    for path, pid, vol, dname in _sessions():
        if (os.path.basename(path).lower() or str(pid)) == app:
            if mute is True:
                _muted.setdefault(app, _get(vol))
                _set(vol, 0.0)
            elif mute is False:
                _set(vol, _muted.get(app, 1.0) or 1.0)
            if pct is not None:
                _set(vol, max(0, min(100, pct)) / 100.0)
            n += 1
        _release(vol)
    if mute is False or (pct and pct > 0):
        _muted.pop(app, None)
    return {"app": app, "sessions": n}


# --------------------------------------------------------------- microphone

def _capture_devices(enum):
    col = c_void_p()
    _chk(_m(enum, 3, c_int, c_int, c_ulong, POINTER(c_void_p))(
        enum, eCapture, DEVICE_STATE_ACTIVE, byref(col)), "EnumAudioEndpoints(capture)")
    n = ctypes.c_uint()
    _m(col, 3, c_int, POINTER(ctypes.c_uint))(col, byref(n))
    out = []
    for i in range(n.value):
        d = c_void_p()
        if _m(col, 4, c_int, ctypes.c_uint, POINTER(c_void_p))(col, i, byref(d)) == 0:
            out.append(d)
    _release(col)
    return out


def _op_mic(flag=None):
    """flag None -> read. Muting/unmuting covers EVERY microphone, so "muted"
    means no app can hear you, whichever mic it picked."""
    enum = _enumerator()
    try:
        devs = _capture_devices(enum)
        muted, n = True, 0
        for d in devs:
            try:
                ep = c_void_p()
                if _m(d, 3, c_int, POINTER(GUID), c_ulong, c_void_p, POINTER(c_void_p))(
                        d, byref(IID_IAudioEndpointVolume), CLSCTX_ALL, None, byref(ep)) == 0:
                    if flag is not None:
                        _m(ep, 14, c_int, c_int, POINTER(GUID))(ep, 1 if flag else 0, None)
                    m = c_int()
                    _m(ep, 15, c_int, POINTER(c_int))(ep, byref(m))
                    muted = muted and bool(m.value)
                    n += 1
                    _release(ep)
            finally:
                _release(d)
        return {"muted": muted if n else False, "mics": n}
    finally:
        _release(enum)


# ------------------------------------------------------------------ public

def apps():
    return sysctl.audio_call(_op_apps)


def set_app(app, pct=None, mute=None):
    return sysctl.audio_call(_op_set, str(app), pct, mute)


def mic():
    return sysctl.audio_call(_op_mic, None)


def set_mic(flag):
    return sysctl.audio_call(_op_mic, bool(flag))
