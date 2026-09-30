"""Now playing - read live from Windows' media controls.

Every app that shows up in the Windows volume flyout's media box (Spotify, a
browser tab, Windows Media Player, most games' launchers) publishes itself
through the System Media Transport Controls. This reads that straight from
WinRT with the `winrt` packages: title, artist, album, artwork, where the
track is and how long it is, which buttons the app allows - and drives it
(play/pause, next, previous, seek).

Design:
  * One background thread owns an asyncio loop and every WinRT call. The
    HTTP threads only ever read a snapshot or queue a command, so a slow app
    can never stall /api/state.
  * It only polls while someone is looking (a phone asked in the last 30s),
    once a second. Idle, it costs nothing.
  * The phone gets the position as of "now" plus the rate, and animates the
    bar itself between polls - so it moves smoothly without hammering us.
  * The artwork is re-encoded by Pillow (never passed through as-is) and
    served from memory under a content hash. Its main colour is picked here
    so the phone can tint the tile to match, Spotify-style.
  * No artwork (lots of browser tabs) -> the app's own icon stands in, found
    by matching the session's app id to a running process.

If winrt isn't installed (running from source without it) this falls back to
the old PowerShell read in sysctl: title/artist/playing only, and the media
keys for control.
"""
import asyncio
import ctypes
import ctypes.wintypes as wt
import datetime
import hashlib
import io
import os
import threading
import time

try:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as _Manager)
    from winrt.windows.storage.streams import Buffer, InputStreamOptions
    HAVE_WINRT = True
except Exception:                                   # pragma: no cover
    HAVE_WINRT = False

PLAYING, PAUSED = 4, 5
WANT_FOR = 30.0          # keep polling this long after the last request
POLL = 1.0
ART_MAX = 600            # px, longest side, for the phone
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)

_lock = threading.Lock()
_snap = {"val": None, "at": 0.0}
_art = {}                # hash -> (bytes, content type); last two only
_icon = {"key": None, "path": None}
_want = {"at": 0.0}
_seek = {"v": None}      # the last seek, until the app confirms it
_loop = None
_started = False


# --------------------------------------------------------------- processes

kernel32 = ctypes.windll.kernel32
version = ctypes.windll.version


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


def _processes():
    """[(pid, exe name)] for everything running."""
    out = []
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)
    if snap in (0, -1, ctypes.c_void_p(-1).value):
        return out
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out.append((e.th32ProcessID, e.szExeFile))
            ok = kernel32.Process32NextW(snap, ctypes.byref(e))
    finally:
        kernel32.CloseHandle(snap)
    return out


def _exe_path(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
    finally:
        kernel32.CloseHandle(h)
    return ""


def _file_description(path):
    """The name Windows shows for an exe ("Google Chrome", "Spotify")."""
    try:
        n = version.GetFileVersionInfoSizeW(path, None)
        if not n:
            return ""
        data = ctypes.create_string_buffer(n)
        if not version.GetFileVersionInfoW(path, 0, n, data):
            return ""
        ptr, ln = ctypes.c_void_p(), wt.UINT()
        if not version.VerQueryValueW(data, "\\VarFileInfo\\Translation",
                                      ctypes.byref(ptr), ctypes.byref(ln)) or not ln.value:
            langs = [(0x0409, 0x04B0)]
        else:
            arr = ctypes.cast(ptr, ctypes.POINTER(wt.WORD * 2)).contents
            langs = [(arr[0], arr[1]), (0x0409, 0x04B0)]
        for lang, cp in langs:
            key = "\\StringFileInfo\\%04x%04x\\FileDescription" % (lang, cp)
            if version.VerQueryValueW(data, key, ctypes.byref(ptr), ctypes.byref(ln)) and ln.value:
                s = ctypes.wstring_at(ptr, ln.value).rstrip("\x00").strip()
                if s:
                    return s
    except Exception:
        pass
    return ""


# App ids that aren't simply "<exe>" or "<exe>.<profile>".
_AUMID_EXE = {
    "308046b0af4a39cb": "firefox.exe",      # Firefox's AUMID is a path hash
    "e7a4f1b7e0a5c3f2": "firefox.exe",
    "msedge": "msedge.exe", "chrome": "chrome.exe", "brave": "brave.exe",
    "opera": "opera.exe", "vivaldi": "vivaldi.exe", "discord": "discord.exe",
}
_NAMES = {"chrome.exe": "Chrome", "msedge.exe": "Edge", "firefox.exe": "Firefox",
          "brave.exe": "Brave", "spotify.exe": "Spotify", "vlc.exe": "VLC",
          "discord.exe": "Discord", "opera.exe": "Opera"}

_app_cache = {}


def resolve_app(aumid):
    """App id -> {"name", "exe"}. exe may be "" when the app isn't a process
    we can see (it still gets a readable name). Found ones are cached for
    good; misses are retried every 30s in case the app was still starting."""
    hit = _app_cache.get(aumid)
    if hit and (hit[0]["exe"] or time.time() < hit[1]):
        return hit[0]
    a = (aumid or "").strip()
    low = a.lower()
    procs = _processes()
    exe = ""

    if "!" in a:
        # A Store app: "Publisher.App_hash!AppId". Its exe lives under
        # WindowsApps\Publisher.App_<version>_<arch>__hash\.
        fam = a.split("!", 1)[0].lower()
        name, _, pub = fam.rpartition("_")
        for pid, nm in procs:
            p = _exe_path(pid)
            pl = p.lower()
            if "\\windowsapps\\" in pl and ("\\" + name + "_") in pl and pl.split("\\windowsapps\\", 1)[1].split("\\", 1)[0].endswith("__" + pub):
                exe = p
                break
    else:
        cand = []
        if low.endswith(".exe"):
            cand.append(os.path.basename(low))
        first = low.split(".", 1)[0]
        if low in _AUMID_EXE:
            cand.append(_AUMID_EXE[low])
        if first in _AUMID_EXE:
            cand.append(_AUMID_EXE[first])
        cand.append(first + ".exe")
        by_name = {}
        for pid, nm in procs:
            by_name.setdefault(nm.lower(), pid)
        for c in cand:
            if c in by_name:
                exe = _exe_path(by_name[c])
                if exe:
                    break

    name = ""
    if exe:
        name = _NAMES.get(os.path.basename(exe).lower()) or _file_description(exe)
    if not name:
        base = a.split("!")[-1] if "!" in a else a.split(".")[0]
        if base.lower().endswith(".exe"):
            base = base[:-4]
        name = _NAMES.get(base.lower() + ".exe") or base or "Media"
        if "_" in name and "!" in a:
            name = name.split("_")[0].split(".")[-1]
    val = {"name": name[:40], "exe": exe}
    if len(_app_cache) > 64:
        _app_cache.clear()
    _app_cache[aumid] = (val, time.time() + 30)
    return val


def _store_logo(exe):
    """For a Store app, the package's own logo beats the exe's icon."""
    try:
        root = exe
        while root and not os.path.isfile(os.path.join(root, "AppxManifest.xml")):
            parent = os.path.dirname(root)
            if parent == root or parent.lower().endswith("windowsapps"):
                return None
            root = parent
        import re
        with open(os.path.join(root, "AppxManifest.xml"), encoding="utf-8", errors="ignore") as f:
            man = f.read()
        m = (re.search(r'Square44x44Logo="([^"]+)"', man) or
             re.search(r'Square150x150Logo="([^"]+)"', man) or
             re.search(r"<Logo>([^<]+)</Logo>", man))
        if not m:
            return None
        rel = m.group(1).replace("/", "\\")
        d = os.path.join(root, os.path.dirname(rel))
        stem, ext = os.path.splitext(os.path.basename(rel))
        best, size = None, 0
        for fn in os.listdir(d):
            fl = fn.lower()
            if not (fl.startswith(stem.lower()) and fl.endswith(ext.lower())) \
                    or "contrast" in fl:
                continue
            # "unplated" is the bare icon, without a coloured square behind it.
            s = os.path.getsize(os.path.join(d, fn)) + (1 << 30 if "unplated" in fl else 0)
            if s > size:
                best, size = os.path.join(d, fn), s
        return best
    except Exception:
        return None


def icon_path(exe):
    if not exe:
        return None
    if "\\windowsapps\\" in exe.lower():
        p = _store_logo(exe)
        if p:
            return p
    try:
        import layout
        return layout.icon_for(exe)
    except Exception:
        return None


# ---------------------------------------------------------------- artwork

def _main_colour(im):
    """The colour a person would call this picture's colour: frequent, and
    not black, white or grey when anything livelier is there."""
    import colorsys
    from PIL import Image
    im = im.copy()
    im.thumbnail((64, 64))
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        px = [p[:3] for p in im.getdata() if p[3] > 160]
        if not px:
            return None
        im = Image.new("RGB", (len(px), 1))
        im.putdata(px)
    else:
        im = im.convert("RGB")
    q = im.quantize(colors=6, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette()
    best, best_score = None, -1.0
    total = float(im.width * im.height)
    for count, idx in q.getcolors() or []:
        r, g, b = pal[idx * 3: idx * 3 + 3]
        h, l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
        score = (count / total) * (0.25 + s) * (0.3 if l < 0.1 or l > 0.93 else 1.0)
        if score > best_score:
            best, best_score = (r, g, b), score
    return list(best) if best else None


def _process_art(raw):
    """Artwork bytes from the app -> (jpeg bytes, colour). Re-encoding
    means we only ever hand the phone an image we produced."""
    from PIL import Image
    im = Image.open(io.BytesIO(raw))
    im.load()
    colour = _main_colour(im)
    im = im.convert("RGB")
    im.thumbnail((ART_MAX, ART_MAX))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=88)
    return out.getvalue(), colour


def _icon_colour(path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.load()
            return _main_colour(im)
    except Exception:
        return None


# ---------------------------------------------------------------- reading

def _secs(td):
    try:
        return max(0.0, td.total_seconds())
    except Exception:
        return 0.0


async def _read_thumb(ref):
    st = await ref.open_read_async()
    try:
        n = int(st.size)
        if not n or n > 8 * 1024 * 1024:
            return None
        r = await st.read_async(Buffer(n), n, InputStreamOptions.READ_AHEAD)
        # Release the view before the buffer goes away - an outstanding view
        # on a WinRT buffer takes the whole process down with it.
        with memoryview(r) as mv:
            return bytes(mv)
    finally:
        st.close()


class _Reader:
    def __init__(self):
        self.mgr = None
        self.session = None
        self.track_key = None
        self.art_hash = None
        self.art_colour = None

    async def manager(self):
        if self.mgr is None:
            self.mgr = await _Manager.request_async()
        return self.mgr

    async def pick(self):
        """Windows' own "current" session, unless it's paused and something
        else is actually playing - then that."""
        mgr = await self.manager()
        cur = mgr.get_current_session()
        sessions = list(mgr.get_sessions())
        def st(s):
            try:
                return int(s.get_playback_info().playback_status)
            except Exception:
                return 0
        if cur is not None and st(cur) == PLAYING:
            return cur
        for s in sessions:
            if st(s) == PLAYING:
                return s
        return cur or (sessions[0] if sessions else None)

    async def read(self):
        s = await self.pick()
        self.session = s
        if s is None:
            return None
        props = await s.try_get_media_properties_async()
        title = (props.title or "").strip() if props else ""
        if not title:
            return None
        info = s.get_playback_info()
        c = info.controls
        status = int(info.playback_status)
        tl = s.get_timeline_properties()
        start, end = _secs(tl.start_time), _secs(tl.end_time)
        dur = max(0.0, end - start)
        pos = max(0.0, _secs(tl.position) - start)
        rate = 1.0
        try:
            rate = float(info.playback_rate or 1.0)
        except Exception:
            pass
        now = time.time()
        # The app says where it was at last_updated; it doesn't tick every
        # second (Spotify barely updates at all). Carry it forward to now.
        try:
            upd = (tl.last_updated_time - _EPOCH).total_seconds()
        except Exception:
            upd = 0.0
        if status == PLAYING and dur and 0 < upd <= now + 5:
            pos += max(0.0, now - upd) * rate
        # Right after a seek, apps take a few seconds to report the new spot;
        # until they do, trust the seek rather than snap back to the old one.
        sk = _seek.get("v")
        if sk and sk["app"] == (s.source_app_user_model_id or ""):
            if upd >= sk["at"] or now - sk["at"] > 6:
                _seek["v"] = None
            else:
                pos = sk["pos"] + ((now - sk["at"]) * rate if status == PLAYING else 0.0)
        if dur:
            pos = min(pos, dur)

        aumid = s.source_app_user_model_id or ""
        app = resolve_app(aumid)
        artist = (props.artist or props.album_artist or "").strip()
        album = (props.album_title or "").strip()
        key = (aumid, title, artist, album)
        if key != self.track_key:
            self.track_key = key
            self.art_hash, self.art_colour = None, None
            if props.thumbnail is not None:
                try:
                    raw = await _read_thumb(props.thumbnail)
                    if raw:
                        jpg, colour = _process_art(raw)
                        h = hashlib.sha1(jpg).hexdigest()[:16]
                        with _lock:
                            _art[h] = (jpg, "image/jpeg")
                            for old in list(_art)[:-2]:
                                _art.pop(old, None)
                        self.art_hash, self.art_colour = h, colour
                except Exception:
                    pass

        ipath = icon_path(app["exe"])
        icon_v = None
        colour = self.art_colour
        if ipath:
            icon_v = hashlib.sha1(ipath.encode("utf-8", "ignore")).hexdigest()[:12]
            with _lock:
                _icon["key"], _icon["path"] = icon_v, ipath
            if colour is None:
                colour = _icon_colour_cached(ipath)

        return {
            "title": title[:200], "artist": artist[:200], "album": album[:200],
            "app": aumid[:200], "appName": app["name"],
            "status": status, "playing": status == PLAYING,
            "pos": round(pos, 2), "dur": round(dur, 2), "rate": rate,
            "at": now,
            "art": self.art_hash, "icon": icon_v, "color": colour,
            "can": {"toggle": bool(c.is_play_pause_toggle_enabled or c.is_play_enabled or c.is_pause_enabled),
                    "next": bool(c.is_next_enabled), "prev": bool(c.is_previous_enabled),
                    "seek": bool(c.is_playback_position_enabled) and dur > 0},
        }


_icol = {}


def _icon_colour_cached(path):
    if path not in _icol:
        _icol[path] = _icon_colour(path)
    return _icol[path]


_reader = _Reader()


async def _pump():
    while True:
        if time.time() - _want["at"] < WANT_FOR:
            try:
                val = await _reader.read()
            except Exception:
                val = None
                _reader.mgr = None          # re-acquire next time
            with _lock:
                _snap["val"], _snap["at"] = val, time.time()
            await asyncio.sleep(POLL)
        else:
            await asyncio.sleep(0.4)


def _thread():
    global _loop
    try:
        from winrt.runtime import init_apartment, MTA
        init_apartment(MTA)
    except Exception:
        pass
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_until_complete(_pump())


def _start():
    global _started
    if _started or not HAVE_WINRT:
        return
    _started = True
    threading.Thread(target=_thread, name="now-playing", daemon=True).start()


# ------------------------------------------------------------------ public

def now_playing():
    """The latest snapshot, immediately, with the position carried forward
    to this instant. Asking also keeps the reader awake."""
    _want["at"] = time.time()
    if not HAVE_WINRT:
        import sysctl
        return sysctl.now_playing()
    _start()
    with _lock:
        v = _snap["val"]
    if not v:
        return None
    v = dict(v)
    now = time.time()
    if v["playing"] and v["dur"]:
        v["pos"] = round(min(v["dur"], v["pos"] + (now - v["at"]) * v["rate"]), 2)
    v["at"] = now
    return v


def art(h):
    with _lock:
        return _art.get(h)


def icon(v):
    with _lock:
        if v and v == _icon["key"]:
            return _icon["path"]
    return None


def command(op, pos=None):
    """play/pause, next, previous or seek on the session shown. Returns
    (ok, error). Without winrt it falls back to the media keys."""
    if op not in ("toggle", "next", "prev", "seek"):
        return False, "unknown command"
    if not HAVE_WINRT or _loop is None:
        if op == "seek":
            return False, "seeking needs the winrt packages"
        import sysctl
        sysctl.tap_key({"toggle": "playpause", "next": "next", "prev": "prev"}[op])
        return True, None

    async def go():
        s = _reader.session or await _reader.pick()
        if s is None:
            return False
        if op == "toggle":
            return await s.try_toggle_play_pause_async()
        if op == "next":
            return await s.try_skip_next_async()
        if op == "prev":
            return await s.try_skip_previous_async()
        tl = s.get_timeline_properties()
        start = _secs(tl.start_time)
        end = _secs(tl.end_time)
        p = max(0.0, min(float(pos), max(0.0, end - start)))
        done = await s.try_change_playback_position_async(int((start + p) * 10_000_000))
        if done:
            _seek["v"] = {"pos": p, "at": time.time(),
                          "app": s.source_app_user_model_id or ""}
        return done

    try:
        ok = asyncio.run_coroutine_threadsafe(go(), _loop).result(timeout=6)
    except Exception as e:
        return False, str(e) or "the app didn't answer"
    # Read again straight away so the phone's next poll sees the change.
    _want["at"] = time.time()
    return bool(ok), (None if ok else "the app refused")


def refresh_now(timeout=3.0):
    """Block until a fresh read lands (tests; and right after a command)."""
    if not HAVE_WINRT:
        return now_playing()
    _start()
    _want["at"] = time.time()
    t0 = time.time()
    with _lock:
        seen = _snap["at"]
    while time.time() - t0 < timeout:
        time.sleep(0.1)
        with _lock:
            if _snap["at"] > seen:
                break
    return now_playing()
