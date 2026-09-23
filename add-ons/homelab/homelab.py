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
                })
        out.sort(key=lambda x: (x["type"], x["vmid"] or 0))
        return out

    def guest_action(self, kind, vmid, action):
        """start / stop (pull the plug) / shutdown (ask nicely)."""
        if kind not in ("qemu", "lxc"):
            raise ValueError("unknown guest type")
        if action not in ("start", "stop", "shutdown"):
            raise ValueError("unknown action")
        return self._req(
            "/nodes/%s/%s/%s/status/%s" % (self.node, kind, vmid, action),
            method="POST")

    def node_status(self):
        return self._req("/nodes/%s/status" % self.node) or {}


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
            out["load"] = {"label": "Load", "big": "%.2f" % one, "unit": "1 min",
                           "pct": min(100, round(one * 25))}
        except (ValueError, IndexError):
            pass
    return out


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
