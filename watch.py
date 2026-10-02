"""What's worth a notification - the things the PC notices on its own.

  downloads  a new file in Downloads stops growing (and isn't a temp file)
  steam      a game's install goes from "downloading/updating" to "ready"
  health     the GPU stays above 85 °C, or the system drive drops under 5% free
  session    you close a game you played for 10+ minutes
  updates    a newer Aether Remote is out (once per version)

Security and the sleep timer notify from where they happen (remote.py,
timer.py). Everything goes through push.notify, which applies each phone's
own choices.
"""
import glob
import os
import re
import threading
import time

import push

TEMP_EXT = (".crdownload", ".part", ".partial", ".tmp", ".download", ".aether-part", ".opdownload")
_started = False
_hooks = {"game": lambda: None, "stats": lambda: {}, "update": lambda: {}}


def configure(game=None, stats=None, update=None):
    if game:
        _hooks["game"] = game
    if stats:
        _hooks["stats"] = stats
    if update:
        _hooks["update"] = update
    _start()


def _size(n):
    for u in ("bytes", "KB", "MB", "GB"):
        if n < 1000 or u == "GB":
            return ("%d %s" % (n, u)) if u == "bytes" else ("%.1f %s" % (n, u))
        n /= 1000.0


def human_time(sec):
    m = int(sec // 60)
    h, m = divmod(m, 60)
    return ("%dh %dm" % (h, m)) if h else ("%d min" % m)


class Downloads:
    def __init__(self):
        import files
        self.dir = files.known_folder("{374DE290-123F-4565-9164-39C4925E467B}") or \
            os.path.join(os.path.expanduser("~"), "Downloads")
        self.known = self._snap()
        self.growing = {}

    def _snap(self):
        out = {}
        try:
            for de in os.scandir(self.dir):
                if de.is_file():
                    st = de.stat()
                    out[de.name] = (st.st_size, st.st_mtime)
        except OSError:
            pass
        return out

    def tick(self):
        now = self._snap()
        for name, (size, mtime) in now.items():
            if name in self.known or name.lower().endswith(TEMP_EXT) or name.startswith("~"):
                continue
            prev = self.growing.get(name)
            if prev is not None and prev == size and size > 0:
                self.known[name] = (size, mtime)
                self.growing.pop(name, None)
                push.notify("downloads", "Download finished",
                            "%s · %s" % (name, _size(size)), url="/#files", tag="dl:" + name)
            else:
                self.growing[name] = size
        for name in list(self.known):
            if name not in now:
                self.known.pop(name, None)


class Steam:
    def __init__(self):
        self.state = self._read()

    def _read(self):
        import library
        out = {}
        try:
            for lib in library.steam_libraries(library.steam_root()):
                for p in glob.glob(os.path.join(lib, "appmanifest_*.acf")):
                    try:
                        with open(p, encoding="utf-8", errors="replace") as f:
                            t = f.read(20000)
                    except OSError:
                        continue
                    aid = re.search(r'"appid"\s+"(\d+)"', t)
                    flags = re.search(r'"StateFlags"\s+"(\d+)"', t)
                    name = re.search(r'"name"\s+"([^"]+)"', t)
                    if aid and flags:
                        out[aid.group(1)] = (int(flags.group(1)), name.group(1) if name else "A game")
        except Exception:
            pass
        return out

    def tick(self):
        now = self._read()
        for aid, (flags, name) in now.items():
            old = self.state.get(aid)
            # 4 = fully installed. Anything else with the "update running" /
            # "downloading" bits means work in progress.
            if old and old[0] != 4 and flags == 4:
                push.notify("steam", "Ready to play", "%s finished %s." % (
                    name, "updating" if old[0] & 2 or old[0] & 1024 else "downloading"),
                    tag="steam:" + aid)
        self.state = now


class Health:
    def __init__(self):
        self.hot = 0
        self.told_hot = 0
        self.told_disk = 0

    def tick(self):
        st = _hooks["stats"]() or {}
        t = st.get("temp") or {}
        try:
            c = int(t.get("big"))
        except Exception:
            c = None
        self.hot = self.hot + 1 if c is not None and c >= 85 else 0
        if self.hot >= 3 and time.time() - self.told_hot > 3600:
            self.told_hot = time.time()
            push.notify("health", "GPU running hot", "It's been at %d °C for a while." % c, tag="gpu-hot")
        d = st.get("disk") or {}
        if d.get("pct", 0) >= 95 and time.time() - self.told_disk > 86400:
            self.told_disk = time.time()
            push.notify("health", "Running out of space",
                        "%s has only %s GB free." % (d.get("label", "The system drive").replace("Disk ", ""),
                                                     d.get("big", "?")), tag="disk-full")


class Session:
    def __init__(self):
        self.cur = None            # (game id, name, started)

    def tick(self):
        g = _hooks["game"]()
        gid = g and g.get("gameId")
        if self.cur and gid != self.cur[0]:
            played = time.time() - self.cur[2]
            if played >= 600:
                push.notify("session", "Nice session", "You played %s for %s." % (
                    self.cur[1], human_time(played)), tag="session:" + self.cur[0])
            self.cur = None
        if gid and not self.cur:
            self.cur = (gid, g.get("title", "a game"), time.time() - float(g.get("pos") or 0))


class Updates:
    def __init__(self):
        self.told = set()

    def tick(self):
        st = _hooks["update"]() or {}
        v = st.get("latest")
        if st.get("newer") and v and v not in self.told:
            self.told.add(v)
            push.notify("updates", "Update available",
                        "Aether Remote %s is out - Settings to install it." % v, tag="update:" + v)


def _loop():
    # Each watcher only runs while some phone wants its kind of notification;
    # when one is switched back on it starts from a fresh look, so nothing
    # that happened while it was off comes out as a burst.
    jobs = [[cls, kind, every, None, 0.0] for cls, kind, every in (
        (Downloads, "downloads", 4), (Steam, "steam", 30), (Health, "health", 5),
        (Session, "session", 5), (Updates, "updates", 3600))]
    while True:
        now = time.time()
        for j in jobs:
            cls, kind, every, obj, last = j
            if not push.wants(kind):
                j[3] = None
                continue
            if now - last < every:
                continue
            j[4] = now
            try:
                if obj is None:
                    j[3] = cls()             # first look: just remember how things are
                else:
                    obj.tick()
            except Exception:
                pass
        time.sleep(1)


def _start():
    global _started
    if not _started:
        _started = True
        threading.Thread(target=_loop, name="notify-watch", daemon=True).start()
