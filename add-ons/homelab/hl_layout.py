"""The homelab's tile layout - the same shape as Aether Remote's, so the
shared phone app edits it exactly the same way: sections of tiles on a 4-wide
grid, a theme, and one of three presentation modes.

Only the tile kinds differ. The phone sends back the whole layout after an
edit; never trust it - rebuild it keeping only fields we know, and only
tiles that point at something this install actually exposes.
"""
import json
import os
import re
import time
import uuid

import paths

LAYOUT_PATH = paths.data("layout.json")
COLUMNS = 4

VALID_KINDS = {"stat", "guest", "service", "docker", "link", "spacer"}
STAT_REF = re.compile(r"^[a-z]{2,12}(:[A-Za-z0-9_.-]{1,40})?$")
GUEST_REF = re.compile(r"^(qemu|lxc):\d{1,9}$")

# The same defaults and presets as the PC app, so a theme looks the same on
# both. (The PC's gameLabels switch is harmless here.)
DEFAULT_THEME = {
    "preset": "aether", "primary": "#7c3aed", "secondary": "#22d3ee",
    "bg": "#01020a", "radius": 14, "glass": True, "scanlines": True,
    "gameLabels": True, "wallpaper": None,
}

STAT_LABELS = [("cpu", "CPU"), ("ram", "RAM"), ("disk", "Disk"), ("load", "Load"),
               ("swap", "Swap"), ("uptime", "Uptime"), ("net", "Network"),
               ("guests", "Guests"), ("backup", "Last backup")]


def new_id():
    return uuid.uuid4().hex[:10]


def tile(kind, w, h, ref, label):
    return {"id": new_id(), "kind": kind, "w": w, "h": h, "ref": ref, "label": label}


def build_default(stats, guests, services):
    server = [tile("stat", 2, 1, k, lbl) for k, lbl in STAT_LABELS[:4] if k in stats]
    server += [tile("stat", 2, 1, k, stats[k]["label"]) for k in sorted(stats)
               if k.startswith("zfs:")]
    server += [tile("stat", 2, 1, k, lbl) for k, lbl in STAT_LABELS[4:] if k in stats]
    gtiles = [tile("guest", 2, 1, "%s:%s" % (g["type"], g["vmid"]), g["name"])
              for g in guests]
    secs = [{"id": "server", "name": "Server", "tiles": server},
            {"id": "guests", "name": "VMs & Containers", "tiles": gtiles}]
    if services:
        secs.append({"id": "services", "name": "Services",
                     "tiles": [tile("service", 2, 1, s["unit"], s.get("label") or s["unit"])
                               for s in services]})
    secs.append({"id": "links", "name": "Links", "tiles": []})
    return {"v": 1, "mode": "rail", "theme": dict(DEFAULT_THEME), "columns": COLUMNS,
            "sections": secs, "scenes": [], "updated": time.time()}


def load(default_fn):
    if os.path.isfile(LAYOUT_PATH):
        try:
            with open(LAYOUT_PATH, encoding="utf-8") as f:
                d = json.load(f)
            d.setdefault("mode", "rail")
            d.setdefault("scenes", [])
            d.setdefault("columns", COLUMNS)
            th = dict(DEFAULT_THEME)
            th.update(d.get("theme") or {})
            d["theme"] = th
            return d
        except Exception:
            pass
    d = default_fn()
    save(d)
    return d


def save(d):
    d["updated"] = time.time()
    tmp = LAYOUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, LAYOUT_PATH)
    return d


def sanitize(incoming, ok_guest, ok_service, ok_docker, link_ids):
    """ok_* are callables from the server's allowlist; link_ids the known
    links. A tile for something not exposed is dropped, not kept dormant."""
    incoming = incoming if isinstance(incoming, dict) else {}
    out = {"v": 1,
           "mode": incoming.get("mode") if incoming.get("mode") in ("rail", "scroll", "pages") else "rail",
           "columns": COLUMNS, "sections": [], "scenes": [], "theme": dict(DEFAULT_THEME)}
    th = incoming.get("theme") if isinstance(incoming.get("theme"), dict) else {}
    for k, v in DEFAULT_THEME.items():
        if k not in th:
            continue
        if isinstance(v, bool):
            out["theme"][k] = bool(th[k])
        elif k == "radius":
            try:
                out["theme"][k] = max(0, min(28, int(th[k])))
            except Exception:
                pass
        elif k in ("primary", "secondary", "bg"):
            # These end up inside CSS custom properties: a literal hex colour only.
            if re.fullmatch(r"#[0-9a-fA-F]{6}", str(th[k] or "")):
                out["theme"][k] = str(th[k]).lower()
        elif k == "preset":
            if re.fullmatch(r"[a-z]{1,20}", str(th[k] or "")):
                out["theme"][k] = str(th[k])
    for sec in (incoming.get("sections") or [])[:12]:
        if not isinstance(sec, dict):
            continue
        tiles = []
        for t in (sec.get("tiles") or [])[:150]:
            if not isinstance(t, dict):
                continue
            kind = t.get("kind")
            if kind not in VALID_KINDS:
                continue
            ref = str(t.get("ref", ""))[:120]
            if kind == "stat" and not STAT_REF.match(ref):
                continue
            if kind == "guest" and not (GUEST_REF.match(ref) and ok_guest(ref.split(":")[1])):
                continue
            if kind == "service" and not ok_service(ref):
                continue
            if kind == "docker" and not ok_docker(ref):
                continue
            if kind == "link" and ref not in link_ids:
                continue
            try:
                w = max(1, min(COLUMNS, int(t.get("w", 1))))
                h = max(1, min(6, int(t.get("h", 1))))
            except Exception:
                w, h = 1, 1
            tiles.append({"id": re.sub(r"[^A-Za-z0-9_-]", "", str(t.get("id") or new_id()))[:32] or new_id(),
                          "kind": kind, "w": w, "h": h, "ref": ref,
                          "label": str(t.get("label", ""))[:60]})
        out["sections"].append({
            "id": re.sub(r"[^A-Za-z0-9_-]", "", str(sec.get("id") or new_id()))[:32] or new_id(),
            "name": str(sec.get("name", "Section"))[:40],
            "tiles": tiles})
    return out
