"""Backend for the Aether homelab add-on.

Talks to a Proxmox VE node over its HTTP API for VM/LXC control and node
stats, and to the local systemd + Docker for service control. Stdlib only,
so the add-on drops onto a server with nothing to pip install.

The Proxmox scheme and the systemctl/docker command names are overridable so
the test suite can point the whole thing at a mock without a real cluster.
"""
import json
import os
import shlex
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import paths

# Command names, so a Linux box uses the real ones and the tests inject fakes.
SYSTEMCTL = os.environ.get("AETHER_HL_SYSTEMCTL", "systemctl")
DOCKER = os.environ.get("AETHER_HL_DOCKER", "docker")


def load_config():
    p = paths.seed("config.json", "config.default.json")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------- Proxmox API

class Proxmox:
    """A thin, read-mostly client for one Proxmox node's HTTP API."""

    def __init__(self, cfg):
        px = cfg.get("proxmox", {})
        scheme = px.get("scheme", "https")
        self.base = "%s://%s:%d/api2/json" % (
            scheme, px.get("host", "127.0.0.1"), int(px.get("port", 8006)))
        self.node = px.get("node", "pve")
        self.token_id = px.get("token_id", "")
        self.secret = px.get("token_secret", "")
        self.ctx = ssl.create_default_context()
        if not px.get("verify_tls", False):
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def _req(self, path, method="GET", data=None):
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(self.base + path, data=body, method=method)
        req.add_header("Authorization",
                       "PVEAPIToken=%s=%s" % (self.token_id, self.secret))
        with urllib.request.urlopen(req, timeout=8, context=self.ctx) as r:
            return json.loads(r.read().decode("utf-8")).get("data")

    def guests(self):
        """Every VM and container on the node, merged and normalized."""
        out = []
        for kind in ("qemu", "lxc"):
            try:
                rows = self._req("/nodes/%s/%s" % (self.node, kind)) or []
            except Exception:
                rows = []
            for g in rows:
                maxmem = g.get("maxmem") or 1
                out.append({
                    "vmid": g.get("vmid"),
                    "name": g.get("name") or ("%s-%s" % (kind, g.get("vmid"))),
                    "type": kind,                      # qemu | lxc
                    "status": g.get("status", "unknown"),   # running | stopped
                    "cpu": round((g.get("cpu") or 0) * 100),
                    "memPct": round((g.get("mem") or 0) / maxmem * 100),
                    "uptime": int(g.get("uptime") or 0),
                })
        out.sort(key=lambda x: (x["type"], x["vmid"] or 0))
        return out

    def guest_action(self, kind, vmid, action):
        """start / stop (pull the plug) / shutdown (ask nicely) / reboot."""
        if kind not in ("qemu", "lxc"):
            raise ValueError("unknown guest type")
        if action not in ("start", "stop", "shutdown", "reboot"):
            raise ValueError("unknown action")
        return self._req(
            "/nodes/%s/%s/%s/status/%s" % (self.node, kind, vmid, action),
            method="POST")

    def node_status(self):
        return self._req("/nodes/%s/status" % self.node) or {}

    # Everything below is read-only and covered by Sys.Audit - the token
    # the installer makes can already do it; nothing new is asked for.
    def zfs_pools(self):
        return self._req("/nodes/%s/disks/zfs" % self.node) or []

    def net_rate(self):
        """Bytes/s in and out, from the node's own 1-minute averages."""
        rows = self._req("/nodes/%s/rrddata?timeframe=hour&cf=AVERAGE" % self.node) or []
        for r in reversed(rows):
            if r.get("netin") is not None and r.get("netout") is not None:
                return float(r["netin"]), float(r["netout"])
        return None

    def last_backup(self):
        rows = self._req("/nodes/%s/tasks?typefilter=vzdump&limit=1" % self.node) or []
        return rows[0] if rows else None


# ------------------------------------------------------------ stats as tiles

def _gb(n):
    return n / (1024.0 ** 3)


def node_stats(px):
    """Node CPU / RAM / disk / load as {label,big,unit,pct} tiles, matching
    the phone's stat-tile shape so the same renderer draws them."""
    try:
        s = px.node_status()
    except Exception:
        s = {}
    out = {}

    cpu = s.get("cpu")
    out["cpu"] = ({"label": "CPU", "big": str(round(cpu * 100)), "unit": "%",
                   "pct": round(cpu * 100)} if cpu is not None
                  else {"label": "CPU", "big": "—", "unit": "", "pct": 0,
                        "na": True})

    mem = s.get("memory") or {}
    if mem.get("total"):
        used, total = _gb(mem.get("used", 0)), _gb(mem["total"])
        out["ram"] = {"label": "RAM", "big": "%.1f" % used,
                      "unit": "/ %.0f GB" % total,
                      "pct": round(used / total * 100)}
    else:
        out["ram"] = {"label": "RAM", "big": "—", "unit": "", "pct": 0,
                      "na": True}

    root = s.get("rootfs") or {}
    if root.get("total"):
        free, total = _gb(root.get("avail", root.get("free", 0))), _gb(root["total"])
        out["disk"] = {"label": "Disk", "big": "%.0f" % free,
                       "unit": "GB free",
                       "pct": round((total - free) / total * 100)}
    else:
        out["disk"] = {"label": "Disk", "big": "—", "unit": "", "pct": 0,
                       "na": True}

    load = s.get("loadavg") or []
    if load:
        try:
            one = float(load[0])
            cores = ((s.get("cpuinfo") or {}).get("cpus")) or 4
            out["load"] = {"label": "Load", "big": "%.2f" % one, "unit": "1 min",
                           "pct": min(100, round(one / cores * 100))}
        except (ValueError, IndexError, TypeError):
            pass

    swap = s.get("swap") or {}
    if swap.get("total"):
        out["swap"] = {"label": "Swap", "big": "%.1f" % _gb(swap.get("used", 0)),
                       "unit": "/ %.0f GB" % _gb(swap["total"]),
                       "pct": round(swap.get("used", 0) / swap["total"] * 100)}

    up = s.get("uptime")
    if up:
        out["uptime"] = {"label": "Uptime", "big": human_age(up), "unit": "", "pct": 0}
    return out


def human_age(sec):
    sec = int(sec or 0)
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    if d:
        return "%dd %dh" % (d, h)
    if h:
        return "%dh %dm" % (h, m)
    return "%dm" % m


def _rate(bps):
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1024 or unit == "GB/s":
            return ("%.1f" % bps if unit in ("MB/s", "GB/s") and bps < 10 else "%.0f" % bps), unit
        bps /= 1024.0


def slow_stats(px, guests):
    """The minute-by-minute ones: ZFS pools, network, last backup, guests
    running. Read far less often than the node's live numbers."""
    out = {}
    try:
        for p in px.zfs_pools():
            name = str(p.get("name", "?"))
            size = p.get("size") or 0
            alloc = p.get("alloc") or 0
            health = str(p.get("health", "?"))
            out["zfs:" + name] = {
                "label": "ZFS " + name, "big": health,
                "unit": ("%.0f%% full" % (alloc / size * 100)) if size else "",
                "pct": round(alloc / size * 100) if size else 0,
                "bad": health != "ONLINE"}
    except Exception:
        pass
    try:
        r = px.net_rate()
        if r:
            (i, iu), (o, ou) = _rate(r[0]), _rate(r[1])
            out["net"] = {"label": "Network \u2193 in", "big": "%s %s" % (i, iu),
                          "unit": "\u2191 %s %s" % (o, ou), "pct": 0}
    except Exception:
        pass
    try:
        b = px.last_backup()
        if b:
            ok = str(b.get("status", "")) == "OK"
            out["backup"] = {"label": "Last backup",
                             "big": human_age(time.time() - (b.get("endtime") or b.get("starttime") or 0)) + " ago",
                             "unit": "OK" if ok else str(b.get("status", "failed"))[:40],
                             "pct": 0, "bad": not ok}
        else:
            out["backup"] = {"label": "Last backup", "big": "none", "unit": "no vzdump yet",
                             "pct": 0, "bad": True}
    except Exception:
        pass
    if guests is not None:
        run = sum(1 for g in guests if g.get("status") == "running")
        out["guests"] = {"label": "Guests", "big": "%d/%d" % (run, len(guests)),
                         "unit": "running", "pct": round(run / len(guests) * 100) if guests else 0}
    return out


class Sampler:
    """Reads Proxmox on its own thread so the phone's 2.5s poll never waits
    on the network: live numbers every few seconds, the slow ones each
    minute. /api/state just hands over the latest reading."""

    FAST, SLOW = 3.0, 60.0

    def __init__(self, cfg_fn):
        self.cfg_fn = cfg_fn
        self.lock = threading.Lock()
        self.data = {"stats": {}, "guests": [], "at": 0, "error": None}
        self._slow = {}
        self._slow_at = 0
        self._started = False

    def start(self):
        if not self._started:
            self._started = True
            threading.Thread(target=self._loop, daemon=True).start()

    def poke(self):
        """Read again now (after a start/stop, so the dot turns quickly)."""
        threading.Thread(target=self.sample, daemon=True).start()

    def sample(self):
        cfg = self.cfg_fn()
        px = Proxmox(cfg)
        err = None
        try:
            guests = px.guests()
        except Exception as e:
            guests, err = [], str(e)
        stats = node_stats(px)
        if time.time() - self._slow_at > self.SLOW:
            self._slow = slow_stats(px, guests)
            self._slow_at = time.time()
        elif "guests" in self._slow:
            self._slow.update(slow_stats_guests(guests))
        stats.update(self._slow)
        with self.lock:
            self.data = {"stats": stats, "guests": guests, "at": time.time(), "error": err}

    def _loop(self):
        while True:
            try:
                self.sample()
            except Exception:
                pass
            time.sleep(self.FAST)

    def get(self):
        with self.lock:
            return dict(self.data)


def slow_stats_guests(guests):
    run = sum(1 for g in guests if g.get("status") == "running")
    return {"guests": {"label": "Guests", "big": "%d/%d" % (run, len(guests)),
                       "unit": "running", "pct": round(run / len(guests) * 100) if guests else 0}}


# ---------------------------------------------------------- systemd + docker

def _run(cmd, timeout=6):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except Exception as e:
        class _R:
            returncode = 1
            stdout = ""
            stderr = str(e)
        return _R()


def service_status(unit):
    """active / inactive / failed / unknown - never raises."""
    r = _run(shlex.split(SYSTEMCTL) + ["is-active", unit])
    out = (r.stdout or r.stderr or "").strip()
    return out or "unknown"


def service_restart(unit):
    r = _run(shlex.split(SYSTEMCTL) + ["restart", unit], timeout=30)
    return r.returncode == 0


def docker_list():
    """Every container as {name, state}. Empty if docker isn't here."""
    r = _run(shlex.split(DOCKER) + ["ps", "-a", "--format",
                                    "{{.Names}}\t{{.State}}"])
    if r.returncode != 0:
        return []
    out = []
    for line in (r.stdout or "").splitlines():
        if "\t" in line:
            name, state = line.split("\t", 1)
            out.append({"name": name.strip(), "state": state.strip()})
    return out


def docker_restart(name):
    r = _run(shlex.split(DOCKER) + ["restart", name], timeout=30)
    return r.returncode == 0


# ------------------------------------------------- the whole view, assembled

def snapshot(cfg):
    """Everything the phone shows in one read: stats, guests, services,
    containers. Each part fails soft so one dead source can't blank the page."""
    px = Proxmox(cfg)
    show = cfg.get("show", {})

    guests = px.guests()
    want = show.get("guests", "all")
    if isinstance(want, list):
        keep = set(str(x) for x in want)
        guests = [g for g in guests if str(g["vmid"]) in keep]

    services = []
    for svc in show.get("services", []):
        unit = svc.get("unit")
        if unit:
            services.append({"unit": unit,
                             "label": svc.get("label", unit),
                             "status": service_status(unit)})

    containers = docker_list()
    cwant = show.get("containers", "all")
    if isinstance(cwant, list):
        keep = set(cwant)
        containers = [c for c in containers if c["name"] in keep]

    return {"stats": node_stats(px), "guests": guests,
            "services": services, "containers": containers}


def sections(snap):
    """Build the phone's tile sections from a snapshot. Rebuilt each load, so
    a VM that appears or disappears just shows up or drops off on its own."""
    secs = []

    stat_order = [("cpu", "CPU"), ("ram", "RAM"), ("disk", "Disk"),
                  ("load", "Load")]
    stat_tiles = [{"kind": "stat", "ref": k, "label": lbl}
                  for k, lbl in stat_order if k in snap["stats"]]
    if stat_tiles:
        secs.append({"id": "server", "name": "Server", "tiles": stat_tiles})

    if snap["guests"]:
        secs.append({"id": "guests", "name": "VMs & Containers",
                     "tiles": [{"kind": "vm",
                                "ref": "%s:%s" % (g["type"], g["vmid"]),
                                "label": g["name"], "status": g["status"],
                                "gtype": g["type"]}
                               for g in snap["guests"]]})

    svc_tiles = [{"kind": "service", "ref": s["unit"], "label": s["label"],
                  "status": s["status"]} for s in snap["services"]]
    svc_tiles += [{"kind": "docker", "ref": c["name"], "label": c["name"],
                   "status": c["state"]} for c in snap["containers"]]
    if svc_tiles:
        secs.append({"id": "services", "name": "Services", "tiles": svc_tiles})

    return secs


# ------------------------------------------------------- allowlist guards
# An authenticated phone must only be able to act on what the config exposes -
# not restart an arbitrary systemd unit or container by sending its name.

def allowed_service(cfg, unit):
    return any(s.get("unit") == unit
               for s in cfg.get("show", {}).get("services", []))


def allowed_container(cfg, name):
    want = cfg.get("show", {}).get("containers", "all")
    if isinstance(want, list):
        return name in want
    return name in {c["name"] for c in docker_list()}


def allowed_guest(cfg, vmid):
    want = cfg.get("show", {}).get("guests", "all")
    if isinstance(want, list):
        return str(vmid) in {str(x) for x in want}
    return True     # Proxmox's own token scope is the backstop here.
