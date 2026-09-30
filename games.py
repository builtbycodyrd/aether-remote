"""Now Playing for games: which game from your library is running right now.

Steam says so itself (HKCU\\Software\\Valve\\Steam\\RunningAppID). Anything
else in the library - Epic, Ubisoft, Xbox, games you added - counts as
running when a process lives inside its install folder (or, for Xbox games,
its package). How long you've been playing comes from when that process
started.
"""
import ctypes
import ctypes.wintypes as wt
import os
import time
import winreg

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _PE(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


_k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
_k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PE)]
_k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PE)]
_k32.CloseHandle.argtypes = [ctypes.c_void_p]
_k32.OpenProcess.restype = ctypes.c_void_p
_k32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, wt.DWORD, ctypes.c_wchar_p,
                                            ctypes.POINTER(wt.DWORD)]
_k32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(wt.FILETIME)] * 4

# Helpers every launcher/engine ships that are not the game itself.
_NOISE = ("crashhandler", "crashreport", "unitycrash", "easyanticheat", "battleye",
          "vcredist", "dxsetup", "uninstall", "setup", "launcherpatcher")

# Steam "games" that are really always-on tools. Steam's own RunningAppID
# is the only thing trusted for Steam, so these just never count.
STEAM_NOT_GAMES = {"431960", "993090", "250820", "1070560", "228980", "629520", "1245040"}

_paths = {}          # pid -> (exe path, start time); pids get reused, so keyed by both


def _pids():
    out = []
    snap = _k32.CreateToolhelp32Snapshot(0x2, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        return out
    try:
        e = _PE()
        e.dwSize = ctypes.sizeof(e)
        ok = _k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out.append((e.th32ProcessID, e.szExeFile))
            ok = _k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        _k32.CloseHandle(snap)
    return out


def _info(pid):
    """(exe path, started epoch) for a pid, or None."""
    h = _k32.OpenProcess(0x1000, False, pid)
    if not h:
        return None
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if not _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return None
        c, x, k, u = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
        started = time.time()
        if _k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(x), ctypes.byref(k), ctypes.byref(u)):
            ft = (c.dwHighDateTime << 32) | c.dwLowDateTime
            started = ft / 1e7 - 11644473600
        return buf.value, started
    finally:
        _k32.CloseHandle(h)


def processes():
    """[(pid, exe path, started)] - paths looked up once per process."""
    out, live = [], set()
    for pid, name in _pids():
        if pid in (0, 4):
            continue
        key = (pid, name)
        live.add(key)
        if key not in _paths:
            _paths[key] = _info(pid)
        if _paths[key]:
            out.append((pid,) + _paths[key])
    for k in [k for k in _paths if k not in live]:
        _paths.pop(k, None)
    return out


def steam_running_appid():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            v, _ = winreg.QueryValueEx(k, "RunningAppID")
            return str(int(v)) if int(v) else None
    except OSError:
        return None


def _norm(p):
    return os.path.normcase(os.path.abspath(p)).rstrip("\\") + "\\"


def current(items):
    """The running game from the library as {item, exe, started}, or None.
    `items` is the library (dicts with id, kind, installdir)."""
    games = [i for i in items if i.get("kind") == "game"]
    procs = processes()

    def under(folder):
        best = None
        f = _norm(folder)
        for pid, exe, started in procs:
            low = exe.lower()
            if _norm(exe).startswith(f) and not any(n in os.path.basename(low) for n in _NOISE):
                # The earliest process in the folder is the game's own start.
                if best is None or started < best[2]:
                    best = (pid, exe, started)
        return best

    appid = steam_running_appid()
    if appid and appid not in STEAM_NOT_GAMES:
        it = next((g for g in games if g["id"] == "steam-" + appid), None)
        if it:
            p = under(it["installdir"]) if it.get("installdir") and os.path.isdir(it["installdir"]) else None
            return {"item": it, "exe": p[1] if p else "", "started": p[2] if p else None}

    for it in games:
        if it["id"].startswith("steam-"):
            continue       # Steam says itself what's running (above); a folder
                           # match would count Wallpaper Engine as "playing"
        d = it.get("installdir")
        if d and os.path.isdir(d) and len(_norm(d)) > 12:     # never a drive root
            p = under(d)
            if p:
                return {"item": it, "exe": p[1], "started": p[2]}
        elif it.get("id", "").startswith("uwp-"):
            fam = it["id"][4:].lower()
            name, _, pub = fam.rpartition("_")
            for pid, exe, started in procs:
                low = exe.lower()
                if "\\windowsapps\\" in low and ("\\" + name + "_") in low and ("__" + pub) in low:
                    return {"item": it, "exe": exe, "started": started}
    return None
