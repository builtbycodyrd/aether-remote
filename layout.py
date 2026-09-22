# layout.py - the tile layout, the theme, and icon extraction.
#
# The layout is ONE flat ordered list of sections, each holding an ordered
# list of tiles. The three presentation modes (rail / scroll / pages) all
# render that same list, so switching modes never rearranges anything.
#
# Grid: 4 columns. Row height == column width, which is what makes a 2x3
# tile land on exactly 2:3 - the shape of Steam box art.

import json
import os
import re
import subprocess
import time
import uuid

import paths

HERE = paths.ASSET_DIR
# The user's arrangement and their extracted icons - both per install.
LAYOUT_PATH = paths.data("layout.json")
ICON_DIR = paths.data("icons")

COLUMNS = 4

# Anything the phone can ask a tile to do. A tile names a ref; it can never
# hand the server a command to run.
ACTIONS = {
    "media.playpause": {"label": "Play/Pause", "kind": "key"},
    "media.next":      {"label": "Next", "kind": "key"},
    "media.prev":      {"label": "Previous", "kind": "key"},
    "screen.off":      {"label": "Screen off", "kind": "screenoff"},
    "power.lock":      {"label": "Lock PC", "kind": "power"},
    "power.sleep":     {"label": "Sleep", "kind": "power"},
    "power.restart":   {"label": "Restart", "kind": "power", "destructive": True},
    "power.shutdown":  {"label": "Shut down", "kind": "power", "destructive": True},
    "power.signout":   {"label": "Sign out", "kind": "power", "destructive": True},
}

TOGGLES = {
    "mute":   {"label": "Mute"},
    "keeper": {"label": "Lock volume"},
}

# primary  = the "on" colour: filled tiles, slider, active chip
# secondary = the second gradient stop, edit affordances, accents
# Every violet in the stylesheet is derived from these two, so changing them
# actually changes the app rather than just recolouring one button.
DEFAULT_THEME = {
    "preset": "aether",
    "primary": "#7c3aed",
    "secondary": "#22d3ee",
    "bg": "#01020a",
    "radius": 14,
    "glass": True,
    "scanlines": True,
    "gameLabels": True,
    "wallpaper": None,
}

PRESETS = {
    "aether": {"primary": "#7c3aed", "secondary": "#22d3ee", "bg": "#01020a"},
    "ice":    {"primary": "#0ea5e9", "secondary": "#a5f3fc", "bg": "#020617"},
    "ember":  {"primary": "#f97316", "secondary": "#facc15", "bg": "#0a0503"},
    "forest": {"primary": "#10b981", "secondary": "#a3e635", "bg": "#04100b"},
    "rose":   {"primary": "#e11d48", "secondary": "#fb7185", "bg": "#0d0308"},
    "mono":   {"primary": "#94a3b8", "secondary": "#e2e8f0", "bg": "#0a0a0c"},
}


def new_id():
    return uuid.uuid4().hex[:10]


def tile(kind, w=1, h=1, **kw):
    t = {"id": new_id(), "kind": kind, "w": w, "h": h}
    t.update(kw)
    return t


# ------------------------------------------------------------------- icons

def real_exe(path):
    """Squirrel apps (Discord, Slack, Teams) ship a stub Update.exe that has
    a generic Windows icon. The real program - and the real icon - lives in
    the newest app-<version> folder beside it."""
    if not path or os.path.basename(path).lower() != "update.exe":
        return path
    base = os.path.dirname(path)
    try:
        apps = sorted((d for d in os.listdir(base)
                       if d.lower().startswith("app-")), reverse=True)
    except OSError:
        return path
    for d in apps:
        folder = os.path.join(base, d)
        try:
            exes = [f for f in os.listdir(folder)
                    if f.lower().endswith(".exe")
                    and f.lower() not in ("update.exe", "squirrel.exe")]
        except OSError:
            continue
        if exes:
            return os.path.join(folder, exes[0])
    return path


def icon_for(path):
    """Extract an app's icon to a PNG we can serve. Cached by path.

    Goes through icons.py, which pulls the 256px "jumbo" icon out of the
    Windows shell image list. The obvious ExtractAssociatedIcon route only
    ever returns 32x32, which looked awful on a phone.
    """
    if not path:
        return None
    os.makedirs(ICON_DIR, exist_ok=True)
    key = "".join(c if c.isalnum() else "_" for c in path)[-90:]
    out = os.path.join(ICON_DIR, key + ".png")
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out

    try:
        import icons
        if icons.extract(real_exe(path), out):
            return out
    except Exception:
        pass
    return out if os.path.isfile(out) else None


# ------------------------------------------------------------ default build

def build_default(library_items):
    """First run: a layout built from what is actually installed."""
    games = [i for i in library_items if i["kind"] == "game"]
    games.sort(key=lambda g: (not g.get("art"), g["name"].lower()))

    control = [
        tile("slider", 4, 1, ref="volume", label="Volume"),
        tile("toggle", 1, 1, ref="mute", label="Mute"),
        tile("toggle", 1, 1, ref="keeper", label="Lock"),
        tile("action", 1, 1, ref="media.playpause", label="Play"),
        tile("action", 1, 1, ref="screen.off", label="Screen"),
        tile("stream", 4, 2, ref="0", label="Desktop"),
    ]

    game_tiles = [
        tile("game", 2, 3, ref=g["id"], label=g["name"],
             launch=g["launch"], art=bool(g.get("art")))
        for g in games[:24]
    ]

    apps = [i for i in library_items if i["kind"] == "app"]
    wanted = ("discord", "chrome", "steam", "spotify", "explorer", "moonfin",
              "code", "obs")
    picked, seen = [], set()
    for a in apps:
        low = a["name"].lower()
        for w in wanted:
            if w in low and w not in seen:
                seen.add(w)
                picked.append(a)
                break
    app_tiles = [
        tile("app", 2, 1, ref=a["id"], label=a["name"], launch=a["launch"])
        for a in picked[:8]
    ]

    system = [
        tile("stat", 2, 1, ref="cpu", label="CPU"),
        tile("stat", 2, 1, ref="gpu", label="GPU"),
        tile("stat", 2, 1, ref="ram", label="RAM"),
        tile("stat", 2, 1, ref="disk", label="Disk"),
        tile("action", 2, 1, ref="power.lock", label="Lock PC"),
    ]

    return {
        "v": 1,
        "mode": "rail",
        "theme": dict(DEFAULT_THEME),
        "columns": COLUMNS,
        "sections": [
            {"id": "control", "name": "Control", "tiles": control},
            {"id": "games", "name": "Games", "tiles": game_tiles},
            {"id": "apps", "name": "Apps", "tiles": app_tiles},
            {"id": "system", "name": "System", "tiles": system},
        ],
        "scenes": [],
        "updated": time.time(),
    }


# ----------------------------------------------------------- load and save

def load(library_items=None):
    if os.path.isfile(LAYOUT_PATH):
        try:
            with open(LAYOUT_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            d.setdefault("mode", "rail")
            d.setdefault("scenes", [])
            d.setdefault("columns", COLUMNS)
            theme = dict(DEFAULT_THEME)
            theme.update(d.get("theme") or {})
            d["theme"] = theme
            return d
        except Exception:
            pass
    if library_items is None:
        library_items = []
    d = build_default(library_items)
    save(d)
    return d


def save(d):
    d["updated"] = time.time()
    tmp = LAYOUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, LAYOUT_PATH)
    return d


# ----------------------------------------------------------- validation
# The phone sends back a whole layout after editing. Never trust it: rebuild
# it from scratch, keeping only fields we recognise, so a malformed or
# malicious payload cannot smuggle anything into the file we execute from.

VALID_KINDS = {"slider", "toggle", "action", "app", "game",
               "stream", "stat", "scene", "spacer"}


def sanitize(incoming, known_launches):
    out = {
        "v": 1,
        "mode": incoming.get("mode") if incoming.get("mode") in
                ("rail", "scroll", "pages") else "rail",
        "columns": COLUMNS,
        "sections": [],
        "scenes": [],
        "theme": dict(DEFAULT_THEME),
    }

    th = incoming.get("theme") or {}
    for k, v in DEFAULT_THEME.items():
        if k not in th:
            continue
        if isinstance(v, bool):
            out["theme"][k] = bool(th[k])
        elif isinstance(v, int) and not isinstance(v, bool):
            try:
                out["theme"][k] = max(0, min(28, int(th[k])))
            except Exception:
                pass
        elif k in ("primary", "secondary", "bg"):
            # These end up inside CSS custom properties, so only ever accept a
            # literal hex colour - never arbitrary text.
            s = str(th[k] or "")
            if re.fullmatch(r"#[0-9a-fA-F]{6}", s):
                out["theme"][k] = s.lower()
        elif isinstance(th[k], str) or th[k] is None:
            out["theme"][k] = str(th[k])[:60] if th[k] else None

    for sec in (incoming.get("sections") or [])[:12]:
        tiles = []
        for t in (sec.get("tiles") or [])[:120]:
            kind = t.get("kind")
            if kind not in VALID_KINDS:
                continue
            ref = str(t.get("ref", ""))[:120]
            try:
                w = max(1, min(COLUMNS, int(t.get("w", 1))))
                h = max(1, min(6, int(t.get("h", 1))))
            except Exception:
                w, h = 1, 1

            clean = {
                "id": str(t.get("id") or new_id())[:32],
                "kind": kind, "w": w, "h": h, "ref": ref,
                "label": str(t.get("label", ""))[:60],
            }

            # A launch command is only ever taken from OUR scan, never from
            # whatever the phone sent - that is the whole allowlist idea.
            if kind in ("app", "game"):
                launch = known_launches.get(ref)
                if not launch:
                    continue
                clean["launch"] = launch
                clean["art"] = bool(t.get("art"))
                # Optional pre-launch actions: "set volume to 40, then launch".
                acts = clean_steps(t.get("actions"), known_launches, limit=12)
                if acts:
                    clean["actions"] = acts
            if kind == "action" and ref not in ACTIONS:
                continue
            if kind == "toggle" and ref not in TOGGLES:
                continue
            tiles.append(clean)

        out["sections"].append({
            "id": str(sec.get("id") or new_id())[:32],
            "name": str(sec.get("name", "Section"))[:40],
            "tiles": tiles,
        })

    for sc in (incoming.get("scenes") or [])[:40]:
        steps = clean_steps(sc.get("steps"), known_launches)
        if not steps:
            continue
        out["scenes"].append({
            "id": str(sc.get("id") or new_id())[:32],
            "name": str(sc.get("name", "Scene"))[:40],
            "icon": str(sc.get("icon", "star"))[:24],
            "steps": steps,
        })

    return out


STEP_OPS = {"volume", "mute", "unmute", "keeper", "device", "open", "close",
            "key", "type", "wait", "screenoff", "power"}


def clean_steps(raw, known_launches, limit=30):
    """Rebuild a list of scene/action steps, keeping only recognised ops and
    never trusting a launch string from the phone - open/close resolve their
    command from our own scan, exactly like a tile does. Shared by scenes and
    by a tile's pre-launch actions."""
    steps = []
    for st in (raw or [])[:limit]:
        op = str(st.get("op", ""))[:30]
        if op not in STEP_OPS:
            continue
        step = {"op": op}
        if "value" in st:
            v = st["value"]
            step["value"] = v if isinstance(v, (int, float, bool)) \
                else str(v)[:200]
        if op in ("open", "close"):
            launch = known_launches.get(str(st.get("ref", "")))
            if not launch:
                continue
            step["ref"] = str(st.get("ref"))[:120]
            step["launch"] = launch
        steps.append(step)
    return steps
