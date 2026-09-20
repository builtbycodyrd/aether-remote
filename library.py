# library.py - find the games and apps installed on this PC, with artwork.
#
# NVIDIA-style auto-scan. Sources, best art first:
#   Steam   - libraryfolders.vdf -> every library -> appmanifest_*.acf.
#             Steam already caches real box art under appcache/librarycache,
#             so we get proper vertical capsules for free.
#   Epic    - ProgramData\Epic\EpicGamesLauncher\Data\Manifests\*.item (JSON)
#   Xbox    - Get-AppxPackage, filtered to things with a real launchable app
#   EA      - registry uninstall entries
#   Ubisoft - registry under Ubisoft\Launcher\Installs
#   Start Menu - .lnk shortcuts, for everything that is not a game
#
# Nothing here launches anything. It only reports what exists; the launcher
# allowlist in config.json is still what the phone is allowed to start.

import json
import os
import re
import subprocess
import glob

USERPROFILE = os.environ.get("USERPROFILE", r"C:\Users\Default")
PROGRAMDATA = os.environ.get("PROGRAMDATA", r"C:\ProgramData")

# Where Steam itself might live. Found dynamically first, these are fallbacks.
STEAM_GUESSES = [
    r"C:\Program Files (x86)\Steam",
    r"C:\Program Files\Steam",
    r"D:\Steam",
    r"E:\Steam",
]


# --------------------------------------------------------------------- steam

def steam_root():
    try:
        import winreg
        for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for name in ("SteamPath", "InstallPath"):
                        try:
                            p = winreg.QueryValueEx(k, name)[0]
                            if p and os.path.isdir(p):
                                return p.replace("/", "\\")
                        except FileNotFoundError:
                            pass
            except FileNotFoundError:
                pass
    except Exception:
        pass
    for g in STEAM_GUESSES:
        if os.path.isdir(g):
            return g
    return None


def steam_libraries(root):
    """Every steamapps folder, not just the default one - his games are
    spread across C:, D: and E:."""
    libs = []
    if not root:
        return libs
    default = os.path.join(root, "steamapps")
    if os.path.isdir(default):
        libs.append(default)

    vdf = os.path.join(default, "libraryfolders.vdf")
    if os.path.isfile(vdf):
        try:
            with open(vdf, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            for m in re.finditer(r'"path"\s+"([^"]+)"', text):
                p = os.path.join(m.group(1).replace("\\\\", "\\"), "steamapps")
                if os.path.isdir(p) and p not in libs:
                    libs.append(p)
        except Exception:
            pass
    return libs


def _acf(text, key):
    m = re.search(r'"%s"\s+"([^"]*)"' % key, text)
    return m.group(1) if m else None


# Resolving art is the fiddly part. Older Steam wrote friendly names
# (library_600x900.jpg); current Steam writes SHA-named files with no
# extension, so the name tells you nothing. Rather than reverse-engineer
# assetcache.vdf, identify the vertical capsule by its SHAPE: it is portrait,
# about 2:3. That is version-proof. Results are cached because it means
# opening several files per game.

_ART_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "artcache.json")
_art_cache = None


def _load_art_cache():
    global _art_cache
    if _art_cache is None:
        try:
            with open(_ART_CACHE_PATH, "r", encoding="utf-8") as f:
                _art_cache = json.load(f)
        except Exception:
            _art_cache = {}
    return _art_cache


def _save_art_cache():
    try:
        with open(_ART_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_art_cache, f)
    except Exception:
        pass


def steam_art(cache, appid):
    c = _load_art_cache()
    hit = c.get(appid)
    if hit is not None:
        return hit if (hit and os.path.isfile(hit)) else (hit or None)

    d = os.path.join(cache, appid)
    chosen = None

    # Fast path: the friendly name, when this game was cached by older Steam.
    for named in ("library_600x900.jpg", "library_capsule.jpg"):
        p = os.path.join(d, named)
        if os.path.isfile(p):
            chosen = p
            break

    if not chosen and os.path.isdir(d):
        try:
            from PIL import Image
        except ImportError:
            Image = None
        best_portrait, best_any = None, None
        if Image:
            # Current Steam nests one level deeper: <appid>/<sha>/<files>.
            # Walking handles both that and the old flat layout.
            candidates = []
            for root_dir, _sub, files in os.walk(d):
                for fn in files:
                    candidates.append(os.path.join(root_dir, fn))
            for p in candidates:
                if not os.path.isfile(p) or os.path.getsize(p) < 4096:
                    continue
                try:
                    with Image.open(p) as im:
                        w, h = im.size
                except Exception:
                    continue
                if w < 100 or h < 100:
                    continue
                ratio = w / float(h)
                # 600x900 => 0.667. Allow a little slack for 300x450 etc.
                if 0.60 <= ratio <= 0.75:
                    if not best_portrait or w > best_portrait[1]:
                        best_portrait = (p, w)
                elif not best_any or w > best_any[1]:
                    best_any = (p, w)
        chosen = (best_portrait or best_any or (None, 0))[0]

    if not chosen:
        for fallback in (os.path.join(d, "header.jpg"),
                         os.path.join(cache, "%s_header.jpg" % appid)):
            if os.path.isfile(fallback):
                chosen = fallback
                break

    c[appid] = chosen or ""
    _save_art_cache()
    return chosen


# Steam ships these alongside real games; they are not playable.
STEAM_SKIP = {
    "228980",   # Steamworks Common Redistributables
    "1070560",  # Steam Linux Runtime
    "1391110",
    "1628350",
}


def scan_steam():
    out = []
    root = steam_root()
    if not root:
        return out
    cache = os.path.join(root, "appcache", "librarycache")

    for lib in steam_libraries(root):
        for acf in glob.glob(os.path.join(lib, "appmanifest_*.acf")):
            try:
                with open(acf, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except Exception:
                continue
            appid = _acf(text, "appid")
            name = _acf(text, "name")
            if not appid or not name or appid in STEAM_SKIP:
                continue

            art = steam_art(cache, appid)

            out.append({
                "id": "steam-%s" % appid,
                "name": name,
                "kind": "game",
                "source": "Steam",
                "launch": "steam://rungameid/%s" % appid,
                "art": art,
                "installdir": os.path.join(lib, "common", _acf(text, "installdir") or ""),
            })
    return out


# ---------------------------------------------------------------------- epic

def scan_epic():
    out = []
    d = os.path.join(PROGRAMDATA, "Epic", "EpicGamesLauncher", "Data", "Manifests")
    if not os.path.isdir(d):
        return out
    for item in glob.glob(os.path.join(d, "*.item")):
        try:
            with open(item, "r", encoding="utf-8", errors="replace") as f:
                j = json.load(f)
        except Exception:
            continue
        name = j.get("DisplayName")
        appname = j.get("AppName")
        if not name or not appname:
            continue
        if j.get("bIsIncompleteInstall"):
            continue

        # Epic leaves manifests behind for games that were uninstalled or
        # moved, so the folder is the only reliable proof it is really here.
        # Listing a phantom game means a tile that silently does nothing.
        loc = j.get("InstallLocation")
        if not loc or not os.path.isdir(loc):
            continue

        out.append({
            "id": "epic-%s" % appname,
            "name": name,
            "kind": "game",
            "source": "Epic",
            "launch": ("com.epicgames.launcher://apps/%s?action=launch&silent=true"
                       % appname),
            "art": None,
            "installdir": j.get("InstallLocation"),
        })
    return out


# ------------------------------------------------------------------- ubisoft

def scan_ubisoft():
    out = []
    try:
        import winreg
        key = r"SOFTWARE\WOW6432Node\Ubisoft\Launcher\Installs"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
            i = 0
            while True:
                try:
                    gid = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(k, gid) as sub:
                        path = winreg.QueryValueEx(sub, "InstallDir")[0]
                    name = os.path.basename(path.rstrip("\\/")) or ("Ubisoft %s" % gid)
                    out.append({
                        "id": "ubi-%s" % gid,
                        "name": name,
                        "kind": "game",
                        "source": "Ubisoft",
                        "launch": "uplay://launch/%s/0" % gid,
                        "art": None,
                        "installdir": path,
                    })
                except Exception:
                    pass
    except Exception:
        pass
    return out


# --------------------------------------------------------------- xbox / uwp

def scan_xbox():
    """Game Pass titles are UWP packages. PowerShell is the only sane way in."""
    ps = (
        "Get-AppxPackage | Where-Object { $_.IsFramework -eq $false -and "
        "$_.SignatureKind -ne 'System' } | ForEach-Object { "
        "$m = $_.InstallLocation + '\\AppxManifest.xml'; "
        "if (Test-Path $m) { "
        "  $x = [xml](Get-Content $m -ErrorAction SilentlyContinue); "
        "  $app = $x.Package.Applications.Application; "
        "  if ($app) { "
        "    $aid = if ($app -is [array]) { $app[0].Id } else { $app.Id }; "
        "    [pscustomobject]@{ n=$_.Name; f=$_.PackageFamilyName; a=$aid } } } } | "
        "ConvertTo-Json -Compress"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=90,
                           creationflags=0x08000000)
        data = json.loads(r.stdout or "[]")
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]

    # Almost everything here is a Windows component; only keep plausible apps.
    skip = re.compile(
        r"Microsoft\.(Windows|UI|VCLibs|NET|Services|Aad|Account|Async|BioEnroll|"
        r"Cred|Desktop|ECApp|Lock|MicrosoftEdge|OneDrive|Paint|People|Print|"
        r"Search|SecHealth|StartExperience|Sticky|Store|Todos|Xbox(Game(Callable|"
        r"Overlay|Speech))|Yourphone|ZuneMusic|ZuneVideo)", re.I)

    out = []
    for e in data:
        name = e.get("n") or ""
        fam = e.get("f") or ""
        aid = e.get("a") or ""
        if not name or not fam or not aid or skip.search(name):
            continue
        out.append({
            "id": "uwp-%s" % fam,
            "name": name.split(".")[-1] if "." in name else name,
            "kind": "app",
            "source": "Microsoft Store",
            "launch": "shell:appsFolder\\%s!%s" % (fam, aid),
            "art": None,
            "installdir": None,
        })
    return out


# -------------------------------------------------------------- start menu

# Windows' own tool drawers. Almost nothing in them is something you would
# want on a phone remote - Character Map, ODBC Data Sources, Application
# Verifier - so they are dropped wholesale, and the handful people do reach
# for are named individually below.
SYSTEM_FOLDERS = {
    "accessories", "accessibility", "administrative tools",
    "windows administrative tools", "windows accessories", "windows system",
    "windows powershell", "windows kits", "system tools", "maintenance",
    "startup", "microsoft office tools", "windows ease of access",
}

# The exceptions - the ones people actually ask a remote for. Character Map
# and friends are deliberately NOT here.
SYSTEM_KEEP = {
    "task manager", "control panel", "run", "command prompt",
    "file explorer", "this pc", "remote desktop connection",
    "snipping tool", "calculator", "notepad", "paint",
    "windows terminal", "windows security", "registry editor",
    "disk cleanup", "event viewer",
}

# Documentation, samples and the little settings utilities that ship beside
# a real app. "Batch Standards Checker" and "MSI Afterburner localization
# reference" are not apps anyone launches from their phone.
NOISE = re.compile(
    r"uninstall|readme|help|documentation|website|user guide|manual|"
    r"repair|licen[cs]e|changelog|report a|feedback|"
    r"^visual studio installer$|"
    r"\b(reference|references|samples?|faq|release notes|revision history)$|"
    r"example scripts|module docs|localization|skin format|"
    r"settings wizard|migrate from|reset settings|"
    r"\b(export|import)\b.*\bsettings\b|"
    r"(native|cross) tools command prompt|developer (command prompt|powershell)|"
    r"command prompt for vs|install additional tools|"
    r"\bverifier\b|\btelemetry\b|language preferences|spreadsheet compare|"
    r"recording manager|check for updates|background downloader|"
    r"standards checker|performance test|property tab builder|"
    r"costing template|add-ins manager|sample (desktop|uwp) apps",
    re.I)


def scan_start_menu(include_system=False):
    """The .lnk files Windows already curates, minus the parts it curates
    badly. `include_system` puts the Windows tool drawers back."""
    dirs = [
        os.path.join(USERPROFILE, r"AppData\Roaming\Microsoft\Windows\Start Menu\Programs"),
        os.path.join(PROGRAMDATA, r"Microsoft\Windows\Start Menu\Programs"),
    ]
    noise = NOISE

    out = []
    seen = set()
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            # Which folder under Programs\ is this? That is what separates
            # "software you installed" from "Windows' own tool drawer".
            rel = os.path.relpath(root, d)
            top = "" if rel == "." else rel.split(os.sep)[0].lower()
            sysfolder = top in SYSTEM_FOLDERS

            for fn in files:
                if not fn.lower().endswith(".lnk"):
                    continue
                base = os.path.splitext(fn)[0]
                low = base.lower()

                if sysfolder and not include_system and low not in SYSTEM_KEEP:
                    continue
                if noise.search(base) or low in seen:
                    continue
                seen.add(low)
                out.append({
                    "id": "lnk-%s" % re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-"),
                    "name": base,
                    "kind": "app",
                    "source": "Start Menu",
                    "launch": os.path.join(root, fn),
                    "art": None,
                    "installdir": None,
                })
    return out


# ------------------------------------------------------------------ combined

def custom_apps():
    """Apps the user added deliberately - from the PC app's file picker or
    the phone's browser. These rank above anything auto-detected."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "custom_apps.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            apps = json.load(f)
    except Exception:
        return []
    out = []
    for a in apps:
        if not isinstance(a, dict) or not a.get("launch"):
            continue
        if not os.path.exists(a["launch"]):
            continue          # uninstalled since - do not offer a dead tile
        a.setdefault("kind", "app")
        a.setdefault("source", "Added by you")
        a.setdefault("art", None)
        out.append(a)
    return out


def scan_all(include_start_menu=True, include_system=False):
    """include_system=True keeps Windows' own tool drawers (Character Map,
    ODBC Data Sources and the rest). Off by default because they bury the
    apps you actually want under a few hundred entries you do not."""
    items = list(custom_apps())
    for fn in (scan_steam, scan_epic, scan_ubisoft, scan_xbox):
        try:
            items.extend(fn())
        except Exception:
            pass
    if include_start_menu:
        try:
            items.extend(scan_start_menu(include_system=include_system))
        except Exception:
            pass

    # Same title from two sources: keep the one with artwork.
    best = {}
    for it in items:
        k = it["name"].strip().lower()
        cur = best.get(k)
        if cur is None or (it.get("art") and not cur.get("art")):
            best[k] = it
    out = sorted(best.values(), key=lambda x: (x["kind"] != "game", x["name"].lower()))

    # Ids must be unique - the UI keys tiles by them. Two different Start Menu
    # names can slugify to the same id ("VS Code" / "VS-Code"), so disambiguate
    # rather than letting one silently shadow the other.
    seen = {}
    for it in out:
        base = it["id"]
        n = seen.get(base, 0)
        if n:
            it["id"] = "%s-%d" % (base, n + 1)
        seen[base] = n + 1
    return out


if __name__ == "__main__":
    import sys
    games = [i for i in scan_all() if i["kind"] == "game"]
    apps = [i for i in scan_all() if i["kind"] == "app"]
    print("games: %d   apps: %d" % (len(games), len(apps)))
    for g in games:
        print("  [%-8s] %-42s art=%s" % (g["source"], g["name"][:42],
                                         "yes" if g["art"] else "no"))
    if "-a" in sys.argv:
        for a in apps[:40]:
            print("  [%-12s] %s" % (a["source"], a["name"][:50]))
