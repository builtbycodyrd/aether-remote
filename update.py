"""Is there a newer release, and can we fetch it?

Written for someone who does not think about software updates: the app
notices, says so once, and offers one button. Nothing downloads or runs
without the user asking for it.

Rules this sticks to:
  * The release feed is pinned to one repo over HTTPS. A redirect may take
    the actual file to githubusercontent, which is where GitHub serves
    assets from, but the conversation only ever STARTS at github.com.
  * Checks are cached for a day. A phone remote has no business hitting an
    API every time someone opens a menu.
  * A version the user dismissed stays dismissed until a newer one appears.
  * Nothing is executed automatically. `fetch_installer` downloads and hands
    back a path; running it is a separate, deliberate call.
"""
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

import paths
import version

REPO = "builtbycodyrd/aether-remote"
API = "https://api.github.com/repos/%s/releases/latest" % REPO
STATE_PATH = paths.data("update.json")
CACHE_SECONDS = 24 * 60 * 60
UA = {"User-Agent": "AetherRemote/%s" % version.VERSION,
      "Accept": "application/vnd.github+json"}

# Where an update is allowed to come from. Anything else is refused, so a
# tampered cache file cannot turn this into a download-and-run primitive.
ALLOWED_HOSTS = {"github.com", "api.github.com", "objects.githubusercontent.com",
                 "release-assets.githubusercontent.com"}


def parse(v):
    """'v1.2.3' -> (1, 2, 3). Unparseable becomes (0,), which loses to
    everything, so a malformed tag can never look like an upgrade."""
    nums = re.findall(r"\d+", str(v or ""))
    return tuple(int(n) for n in nums[:4]) or (0,)


def newer(a, b):
    """Is a newer than b?"""
    pa, pb = parse(a), parse(b)
    n = max(len(pa), len(pb))
    pa += (0,) * (n - len(pa))
    pb += (0,) * (n - len(pb))
    return pa > pb


def api_url():
    """The release feed. AETHER_UPDATE_API repoints it, which is how the
    tests serve a pretend release without publishing a real one."""
    return os.environ.get("AETHER_UPDATE_API") or API


def _allowed(url):
    """May we download this? Normally: only from GitHub. If the feed itself
    has been repointed (tests), that host is trusted too - but only that one,
    so a tampered update.json still cannot send us anywhere it likes."""
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        scheme = urllib.parse.urlparse(url).scheme.lower()
    except Exception:
        return False
    if host in ALLOWED_HOSTS:
        return scheme == "https"
    override = os.environ.get("AETHER_UPDATE_API")
    if override:
        try:
            return host == (urllib.parse.urlparse(override).hostname or "").lower()
        except Exception:
            return False
    return False


def _load():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            got = json.load(f)
        return got if isinstance(got, dict) else {}
    except Exception:
        return {}


def _save(st):
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass
    return st


def _get(url, timeout=12):
    req = urllib.request.Request(url, headers=dict(UA))
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


def _pick_asset(assets):
    """The installer, if there is one. A .zip is the fallback because the
    portable build is a legitimate way to ship, but the .exe is what a
    non-technical user wants."""
    exes = [a for a in assets if str(a.get("name", "")).lower().endswith(".exe")]
    for a in exes:
        if "setup" in str(a.get("name", "")).lower():
            return a
    if exes:
        return exes[0]
    for a in assets:
        if str(a.get("name", "")).lower().endswith(".zip"):
            return a
    return None


def check(force=False):
    """Ask GitHub what the latest release is, at most once a day.

    Never raises. A failed check leaves the previous answer in place and
    records why, because an app that cannot reach the internet should still
    open its menus.
    """
    st = _load()
    age = time.time() - float(st.get("checked") or 0)
    # Keyed off "checked", not "latest": a machine with no internet must not
    # retry on every poll just because it has never had an answer.
    if not force and st.get("checked") and age < CACHE_SECONDS:
        return st

    try:
        rel = json.loads(_get(api_url()).decode("utf-8", "replace"))
    except Exception as e:
        st["error"] = "%s: %s" % (type(e).__name__, e)
        st["checked_failed"] = time.time()
        # A failure earns a 15-minute cooldown rather than a full day, but
        # it is still a cooldown - no retry storm on a laptop that is offline.
        st["checked"] = time.time() - CACHE_SECONDS + 900
        return _save(st)

    tag = rel.get("tag_name") or ""
    asset = _pick_asset(rel.get("assets") or [])
    st.update({
        "checked": time.time(),
        "error": None,
        "tag": tag,
        "latest": re.sub(r"^v", "", tag),
        "name": rel.get("name") or tag,
        "notes": rel.get("body") or "",
        "page": rel.get("html_url") or "",
        "published": rel.get("published_at") or "",
        "asset": (asset or {}).get("browser_download_url") or "",
        "asset_name": (asset or {}).get("name") or "",
        "asset_size": (asset or {}).get("size") or 0,
    })
    return _save(st)


def state(force=False):
    """Everything the UI needs, in one shape, whatever happened.

    `available` is the single flag the banner keys off: a newer version
    exists, it has something to download, and the user has not waved it away.
    """
    st = check(force=force)
    latest = st.get("latest") or ""
    skipped = st.get("skipped") or ""
    is_new = bool(latest) and newer(latest, version.VERSION)
    return {
        "current": version.VERSION,
        "latest": latest,
        "newer": is_new,
        "skipped": skipped,
        "dismissed": bool(skipped) and not newer(latest, skipped),
        "available": is_new and bool(st.get("asset")) and
                     (not skipped or newer(latest, skipped)),
        "name": st.get("name") or "",
        "notes": st.get("notes") or "",
        "page": st.get("page") or "",
        "published": st.get("published") or "",
        "asset_name": st.get("asset_name") or "",
        "asset_size": st.get("asset_size") or 0,
        "checked": st.get("checked") or 0,
        "error": st.get("error") or "",
        "seen": st.get("seen") or "",
    }


def skip(v=None):
    """Wave a version away. Stays waved away until a newer one turns up."""
    st = _load()
    st["skipped"] = v or st.get("latest") or ""
    return _save(st)


def unskip():
    st = _load()
    st.pop("skipped", None)
    return _save(st)


def mark_seen(v=None):
    """Remember that we have already told the user about this one, so the
    tray balloon appears once and not every time the app restarts."""
    st = _load()
    st["seen"] = v or st.get("latest") or ""
    return _save(st)


def fetch_installer(progress=None):
    """Download the release asset and return its path. Does not run it.

    Refuses anything that is not an .exe starting with MZ - a redirect that
    lands somewhere unexpected produces a file we throw away rather than a
    file we hand to the user.
    """
    st = check()
    url = st.get("asset") or ""
    if not url:
        raise RuntimeError("no download for the latest release")
    if not _allowed(url):
        raise RuntimeError("refusing to download from %s" % url)

    name = st.get("asset_name") or "AetherRemoteSetup.exe"
    name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(name))
    folder = paths.data("updates")
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception:
        pass
    out = os.path.join(folder, name)

    req = urllib.request.Request(url, headers=dict(UA))
    ctx = ssl.create_default_context()
    total = int(st.get("asset_size") or 0)
    done = 0
    with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
        if not _allowed(r.geturl()):
            raise RuntimeError("redirected somewhere unexpected")
        with open(out + ".part", "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress:
                    try:
                        progress(done, total)
                    except Exception:
                        pass

    if name.lower().endswith(".exe"):
        with open(out + ".part", "rb") as f:
            if f.read(2) != b"MZ":
                os.remove(out + ".part")
                raise RuntimeError("that download is not a Windows program")

    os.replace(out + ".part", out)
    return out


if __name__ == "__main__":
    import pprint
    import sys
    pprint.pprint(state(force="--force" in sys.argv))
