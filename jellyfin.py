"""Now Playing for Jellyfin clients that don't tell Windows what they play.

Moonfin (and most Jellyfin apps) play video through their own engine and
never show up in Windows' media controls. The Jellyfin server knows exactly
what each of its apps is playing, though - so this asks the server.

Signing in uses Jellyfin's Quick Connect: the app shows a six-character code,
you approve it in Jellyfin on your phone, and the server gives THIS app its
own sign-in. Nobody types a password or copies a key, it shows up in
Jellyfin's Devices list, and deleting it there cuts it off. The sign-in is
kept in this install's data folder (which the Files tab refuses to read) and
never goes to the phone.

Only sessions on THIS PC count: matched by Moonfin's device id, or a device
named like this PC. Your TV playing something is not "now playing" here.
"""
import hashlib
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request

import paths

CONF = paths.data("jellyfin.json")
MOONFIN_PREFS = os.path.join(os.environ.get("APPDATA", ""), "org.moonfin", "Moonfin",
                             "shared_preferences.json")
POLL = 2.5
TIMEOUT = 8

_lock = threading.Lock()
_state = {"val": None, "at": 0.0, "err": None, "seen": []}
_qc = {"code": None, "secret": None, "server": None, "state": None, "at": 0.0}
_want = {"at": 0.0}
_started = False
_art_cb = None          # media.py hands us a function that stores artwork


def _load():
    try:
        with open(CONF, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d):
    tmp = CONF + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, CONF)


def _device_id():
    return "aether-remote-" + hashlib.sha1(socket.gethostname().encode()).hexdigest()[:16]


def _auth_header(token=None):
    import version
    parts = ['Client="Aether Remote"', 'Device="%s"' % socket.gethostname().replace('"', ""),
             'DeviceId="%s"' % _device_id(), 'Version="%s"' % version.VERSION]
    if token:
        parts.append('Token="%s"' % token)
    return "MediaBrowser " + ", ".join(parts)


def _req(server, path, token=None, body=None, method=None, raw=False):
    url = server.rstrip("/") + path
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(url, data=data, method=method or ("POST" if body is not None else "GET"),
                               headers={"Authorization": _auth_header(token),
                                        "Content-Type": "application/json",
                                        "Accept": "application/json"})
    with urllib.request.urlopen(r, timeout=TIMEOUT, context=ssl.create_default_context()) as resp:
        out = resp.read(8 * 1024 * 1024)
        if raw:
            return out
        return json.loads(out) if out else {}


def clean_server(url):
    url = (url or "").strip().rstrip("/")
    if not re.match(r"^https?://[A-Za-z0-9.\-\[\]:]+(/[^\s]*)?$", url):
        raise ValueError("That doesn't look like a Jellyfin address (https://...)")
    return url


# ------------------------------------------------------------------ Moonfin

def moonfin():
    """What Moonfin on this PC is signed in to - the address only, never its
    sign-in - and its device id, so its sessions can be told apart."""
    try:
        with open(MOONFIN_PREFS, encoding="utf-8") as f:
            prefs = json.load(f)
    except Exception:
        return None
    out = {"device_id": prefs.get("flutter.device_id") or ""}
    try:
        store = json.loads(prefs.get("flutter.authentication_store_json") or "{}")
        last = prefs.get("flutter.pref_last_server_id")
        servers = store.get("servers") or {}
        s = servers.get(last) or next(iter(servers.values()), {})
        out["server"] = s.get("address") or ""
        out["name"] = s.get("name") or ""
    except Exception:
        pass
    return out


# ------------------------------------------------------------------ sign in

def status():
    c = _load()
    m = moonfin() or {}
    with _lock:
        qc = dict(_qc)
        seen = list(_state["seen"])
        err = _state["err"]
    return {"connected": bool(c.get("token")), "server": c.get("server") or m.get("server") or "",
            "user": c.get("user") or "", "suggested": m.get("server") or "",
            "moonfin": bool(m), "pending": qc["code"] if qc["state"] == "waiting" else None,
            "qcState": qc["state"], "error": err, "sessions": seen}


def start_quick_connect(server):
    server = clean_server(server)
    try:
        if not _req(server, "/QuickConnect/Enabled"):
            raise ValueError("Quick Connect is turned off on that server (Dashboard > General).")
    except ValueError:
        raise
    except Exception as e:
        raise ValueError("Couldn't reach Jellyfin at %s (%s)" % (server, e))
    r = _req(server, "/QuickConnect/Initiate", body={}, method="POST")
    with _lock:
        _qc.update(code=r.get("Code"), secret=r.get("Secret"), server=server,
                   state="waiting", at=time.time())
    threading.Thread(target=_wait_quick_connect, args=(r.get("Secret"),), daemon=True).start()
    return r.get("Code")


def _wait_quick_connect(secret):
    """Poll until it's approved in Jellyfin (or ten minutes pass)."""
    t0 = time.time()
    while time.time() - t0 < 600:
        with _lock:
            if _qc["secret"] != secret:
                return                     # a newer attempt replaced this one
            server = _qc["server"]
        try:
            r = _req(server, "/QuickConnect/Connect?secret=" + urllib.parse.quote(secret))
            if r.get("Authenticated"):
                a = _req(server, "/Users/AuthenticateWithQuickConnect", body={"Secret": secret})
                _save({"server": server, "token": a["AccessToken"],
                       "user": (a.get("User") or {}).get("Name", ""),
                       "user_id": (a.get("User") or {}).get("Id", "")})
                with _lock:
                    _qc.update(state="done", code=None, secret=None)
                    _state["err"] = None
                return
        except Exception:
            pass
        time.sleep(2)
    with _lock:
        if _qc["secret"] == secret:
            _qc.update(state="expired", code=None, secret=None)


def disconnect():
    c = _load()
    if c.get("token"):
        try:
            _req(c["server"], "/Sessions/Logout", token=c["token"], body={}, method="POST")
        except Exception:
            pass
    try:
        os.remove(CONF)
    except OSError:
        pass
    with _lock:
        _state.update(val=None, err=None, seen=[])


# ------------------------------------------------------------------ reading

_cur = {"key": None, "art": None, "color": None, "aspect": None}
_hold = {"v": None}      # a seek we sent, until the app's own reports catch up


def _is_this_pc(s, moon):
    if s.get("DeviceId") == _device_id():
        return False
    if moon and moon.get("device_id") and s.get("DeviceId") == moon["device_id"]:
        return True
    name = (s.get("DeviceName") or "").lower()
    return name == socket.gethostname().lower()


def _read():
    c = _load()
    if not c.get("token"):
        return None
    moon = moonfin()
    sessions = _req(c["server"], "/Sessions?ActiveWithinSeconds=900", token=c["token"])
    seen, mine = [], []
    for s in sessions or []:
        if s.get("DeviceId") == _device_id():
            continue
        seen.append({"client": s.get("Client", ""), "device": s.get("DeviceName", ""),
                     "playing": bool(s.get("NowPlayingItem")), "here": _is_this_pc(s, moon)})
        if s.get("NowPlayingItem") and _is_this_pc(s, moon):
            mine.append(s)
    with _lock:
        _state["seen"] = seen[:12]
    if not mine:
        return None
    mine.sort(key=lambda s: (s.get("PlayState") or {}).get("IsPaused", False))
    s = mine[0]
    it, ps = s["NowPlayingItem"], s.get("PlayState") or {}
    kind = it.get("Type", "")
    if kind == "Episode":
        title = it.get("Name", "")
        bits = [it.get("SeriesName", "")]
        if it.get("ParentIndexNumber") is not None and it.get("IndexNumber") is not None:
            bits.append("S%d E%d" % (it["ParentIndexNumber"], it["IndexNumber"]))
        artist = " · ".join(b for b in bits if b)
        album = it.get("SeasonName", "")
        img_id = it.get("SeriesId") if it.get("SeriesPrimaryImageTag") else it.get("Id")
    elif kind == "Audio":
        title = it.get("Name", "")
        artist = ", ".join(it.get("Artists") or []) or it.get("AlbumArtist", "")
        album = it.get("Album", "")
        img_id = it.get("AlbumId") if it.get("AlbumPrimaryImageTag") else it.get("Id")
    else:
        title = it.get("Name", "")
        artist = str(it.get("ProductionYear") or "") or kind
        album = ""
        img_id = it.get("Id")
    key = (it.get("Id"), img_id)
    if key != _cur["key"]:
        _cur.update(key=key, art=None, color=None, aspect=None)
        if img_id and _art_cb:
            try:
                raw = _req(c["server"], "/Items/%s/Images/Primary?maxHeight=720&quality=90"
                           % urllib.parse.quote(img_id), token=c["token"], raw=True)
                _cur["art"], _cur["color"], _cur["aspect"] = _art_cb(raw)
            except Exception:
                pass
    dur = (it.get("RunTimeTicks") or 0) / 1e7
    pos = (ps.get("PositionTicks") or 0) / 1e7
    playing = not ps.get("IsPaused", False)
    now = time.time()
    # Clients report their position every ~10 s; carry it forward from then.
    try:
        last = s.get("LastPlaybackCheckIn") or ""
        m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?", last)
        if m and playing:
            import calendar
            t = calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")) + float(m.group(2) or 0)
            if 0 < now - t < 60:
                pos += now - t
    except Exception:
        pass
    h = _hold["v"]
    if h and h["session"] == s.get("Id"):
        reported = (ps.get("PositionTicks") or 0) / 1e7
        if abs(reported - h["pos"]) < 15 or now - h["at"] > 15:
            _hold["v"] = None
        else:
            pos = h["pos"] + (now - h["at"] if playing else 0)
    if dur:
        pos = min(pos, dur)
    remote = bool(s.get("SupportsRemoteControl"))
    return {"src": "jellyfin", "session": s.get("Id"), "kind": "video" if kind != "Audio" else "media",
            "title": title[:200], "artist": artist[:200], "album": album[:200],
            "app": s.get("Client", "Jellyfin"), "appName": s.get("Client") or "Jellyfin",
            "status": 4 if playing else 5, "playing": playing,
            "pos": round(pos, 2), "dur": round(dur, 2), "rate": 1.0, "at": now,
            "art": _cur["art"], "color": _cur["color"], "aspect": _cur["aspect"],
            "can": {"toggle": remote, "next": remote and kind in ("Episode", "Audio"),
                    "prev": remote and kind in ("Episode", "Audio"), "seek": remote and dur > 0}}


def _loop():
    while True:
        if time.time() - _want["at"] < 30 and os.path.isfile(CONF):
            try:
                v = _read()
                with _lock:
                    _state.update(val=v, at=time.time(), err=None)
            except Exception as e:
                msg = str(e)
                if "401" in msg:
                    msg = "Jellyfin signed this app out - connect it again in Settings."
                with _lock:
                    _state.update(val=None, at=time.time(), err=msg[:200])
            time.sleep(POLL)
        else:
            time.sleep(0.5)


def current(art_cb=None):
    """The last reading, straight away (never waits on the network)."""
    global _started, _art_cb
    if art_cb:
        _art_cb = art_cb
    _want["at"] = time.time()
    if not _started:
        _started = True
        threading.Thread(target=_loop, name="jellyfin", daemon=True).start()
    with _lock:
        v = _state["val"]
    if not v:
        return None
    v = dict(v)
    now = time.time()
    if v["playing"] and v["dur"]:
        v["pos"] = round(min(v["dur"], v["pos"] + now - v["at"]), 2)
    v["at"] = now
    return v


def command(session, op, pos=None):
    c = _load()
    if not c.get("token") or not session:
        return False, "not connected"
    path = {"toggle": "PlayPause", "next": "NextTrack", "prev": "PreviousTrack"}.get(op)
    if op == "seek":
        path = "Seek?SeekPositionTicks=%d" % int(max(0.0, float(pos)) * 1e7)
    if not path:
        return False, "unknown command"
    try:
        _req(c["server"], "/Sessions/%s/Playing/%s" % (urllib.parse.quote(session), path),
             token=c["token"], body={}, method="POST")
    except Exception as e:
        return False, str(e)
    with _lock:
        v = _state["val"]
        if v and op == "toggle":
            v["playing"] = not v["playing"]
            v["pos"] = v["pos"] + (time.time() - v["at"] if not v["playing"] else 0)
            v["at"] = time.time()
        if v and op == "seek":
            v["pos"], v["at"] = float(pos), time.time()
            _hold["v"] = {"session": session, "pos": float(pos), "at": time.time()}
    return True, None
