"""The Aether homelab add-on HTTP server.

It serves the SAME phone app as Aether Remote (web/shared/: ui.html, app.js,
login.html - copied from the PC app by sync_ui.py, and checked identical by
the tests), so the tile board, edit mode, drag-to-reorder, themes, PIN and PC
switcher all behave exactly the same. Only the tiles differ: they drive a
Proxmox node instead of a Windows desktop.

Plus /manage - a settings page for a computer's browser.

    python server.py --host 0.0.0.0 --port 8788
"""
import argparse
import json
import os
import re
import socket
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import paths
import auth
import homelab
import hl_layout
import secondfactor
import updater
import version

CFG = homelab.load_config()
CFG_LOCK = threading.Lock()


def save_config():
    p = paths.data("config.json")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(CFG, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


SAMPLER = homelab.Sampler(lambda: CFG)

# The PIN on top of the 30-day login: asked when the app opens, and for every
# VM shut down / reboot / force stop. Signed with the session key, so signing
# everyone out also voids every unlock. (Face ID needs an https address; this
# add-on is served over plain http on the home network, so it's PIN-only.)
SF = secondfactor.SecondFactor(paths.data("sf.json"), lambda: auth.STATE["server_key"],
                               log=lambda m: print("[2fa]", m))
PROTECTED = ("stop", "shutdown", "reboot")

SHARED = paths.asset("web", "shared")
ART_DIR = paths.data("art")
MAX_ART = 512 * 1024
NAME = "Homelab"

MANIFEST = {
    "name": "Aether Homelab", "short_name": "Homelab", "start_url": "/", "scope": "/",
    "display": "standalone", "orientation": "portrait",
    "background_color": "#01020a", "theme_color": "#01020a",
    "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png",
               "purpose": "any maskable"},
              {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png",
               "purpose": "any maskable"}],
}

UNIT_RE = re.compile(r"^[A-Za-z0-9@._:-]{1,80}$")
CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")
URL_RE = re.compile(r"^https?://[^\s<>\"']{3,290}$", re.I)


# ------------------------------------------------------- services + docker
_svc_cache = {"at": 0, "services": {}, "docker": {}}


def svc_states():
    """Status of the configured services and containers. systemctl/docker are
    local and cheap, but the phone polls every few seconds - cache briefly."""
    if time.time() - _svc_cache["at"] < 4:
        return _svc_cache
    show = CFG.get("show", {})
    services = {s["unit"]: homelab.service_status(s["unit"])
                for s in show.get("services", []) if s.get("unit")}
    docker = {}
    if show.get("containers"):
        want = show.get("containers")
        for c in homelab.docker_list():
            if want == "all" or c["name"] in want:
                docker[c["name"]] = c["state"]
    _svc_cache.update(at=time.time(), services=services, docker=docker)
    return _svc_cache


def links():
    return [l for l in CFG.get("links", []) if isinstance(l, dict) and l.get("id")]


def visible_guests(snap_guests):
    return [g for g in snap_guests if homelab.allowed_guest(CFG, g["vmid"])]


def layout_default():
    snap = SAMPLER.get()
    if not snap.get("at"):
        SAMPLER.sample()
        snap = SAMPLER.get()
    services = CFG.get("show", {}).get("services", [])
    return hl_layout.build_default(snap["stats"], visible_guests(snap["guests"]), services)


def load_layout():
    return hl_layout.load(layout_default)


def sanitize_layout(incoming):
    return hl_layout.sanitize(
        incoming,
        ok_guest=lambda vmid: homelab.allowed_guest(CFG, vmid),
        ok_service=lambda u: homelab.allowed_service(CFG, u),
        ok_docker=lambda n: CONTAINER_RE.match(n or "") is not None and (
            CFG.get("show", {}).get("containers") == "all"
            or n in (CFG.get("show", {}).get("containers") or [])),
        link_ids={l["id"] for l in links()})


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _sniff(b):
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "AetherHomelab"

    def log_message(self, *a):
        pass

    # -- helpers ----------------------------------------------------------
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _cookie_named(self, name):
        for part in self.headers.get("Cookie", "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == name:
                    return v
        return ""

    def _cookie(self):
        return self._cookie_named("rs")

    def _authed(self):
        return auth.valid_session(self._cookie())

    def _unlocked(self):
        return (not SF.enabled()) or SF.valid_unlock(self._cookie_named("ru"),
                                                     self._cookie())

    def _unlock_cookie(self, clear=False):
        if clear:
            return "ru=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"
        return ("ru=%s; Max-Age=%d; Path=/; HttpOnly; SameSite=Strict"
                % (SF.make_unlock(self._cookie()), secondfactor.UNLOCK_TTL))

    def _stepup(self, scope, b):
        """None = go ahead. Otherwise the refusal has been sent (returns True).
        With no PIN set up yet, a protected power action asks the phone to set
        one first - it never runs unprotected."""
        if SF.enabled() and SF.use_stepup(b.get("stepup"), scope, self._cookie()):
            return None
        return self._send(403, {"error": "stepup", "scope": scope,
                                "setup": not SF.enabled()}) or True

    def _settings_ok(self, b):
        """Changing what the phone can control: the current PIN if there is
        one, else the signed-in session is enough (like first-time setup)."""
        return (not SF.enabled()) or SF.use_stepup(b.get("stepup"), "manage", self._cookie())

    def _body(self, limit=64 * 1024):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > limit:
            return None
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _client_ip(self):
        return self.client_address[0] if self.client_address else "?"

    def _page(self, name, ctype="text/html; charset=utf-8", subst=None):
        body = _read(os.path.join(SHARED if name in ("ui.html", "app.js", "login.html")
                                  else paths.asset("web"), name))
        if subst:
            txt = body.decode("utf-8")
            for k, v in subst.items():
                txt = txt.replace(k, v)
            body = txt.encode("utf-8")
        return self._send(200, body, ctype)

    # -- GET --------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        qs = parse_qs(u.query)
        try:
            if path == "/ping":
                return self._send(200, {"ok": True, "app": "aether-homelab"})

            if path == "/manifest.webmanifest":
                return self._send(200, MANIFEST, "application/manifest+json")

            if path.startswith("/static/"):
                name = os.path.basename(path)
                p = os.path.join(SHARED, "static", name)
                if not re.fullmatch(r"icon-\d+\.png", name) or not os.path.isfile(p):
                    return self._send(404, {"error": "no such file"})
                return self._send(200, _read(p), "image/png",
                                  {"Cache-Control": "public, max-age=604800"})

            if path == "/login":
                return self._page("login.html", subst={
                    "{{PC}}": NAME,
                    "This device stays unlocked for 30 days.":
                        "This device stays unlocked for 30 days.<br>First time? Scan the QR "
                        "code the installer printed, or run <code>aether-homelab code</code> "
                        "in the container."})

            if path == "/api/enroll":
                # The secret is shown by the installer in the Proxmox shell,
                # never over the network. This route only answers the box
                # itself, and only before enrollment is done.
                if self._client_ip() not in ("127.0.0.1", "::1"):
                    return self._send(403, {"error": "enroll from the server"})
                if auth.STATE.get("enrolled"):
                    return self._send(403, {"error": "already enrolled"})
                return self._send(200, {"enrolled": False,
                                        "secret": auth.STATE["secret"],
                                        "uri": auth.provisioning_uri()})

            if not self._authed():
                if path in ("/", "/manage"):
                    return self._send(302, b"", "text/html",
                                      {"Location": "/login" + ("?next=/manage" if path == "/manage" else "")})
                if path.startswith("/api/"):
                    return self._send(401, {"error": "sign in"})
                return self._send(404, {"error": "no such path"})

            # The pages load (so they can show their own lock screen); their
            # data doesn't until the PIN is given.
            if path == "/":
                return self._page("ui.html", subst={"<title>Aether Remote</title>":
                                                    "<title>Aether Homelab</title>"})
            if path == "/app.js":
                return self._page("app.js", "application/javascript; charset=utf-8")
            if path == "/manage":
                return self._page("manage.html")
            if path == "/manage.js":
                return self._page("manage.js", "application/javascript; charset=utf-8")

            if path == "/api/sf/status":
                return self._send(200, {**SF.status(), "unlocked": self._unlocked(),
                                        "local": False, "passkeys_possible": False})
            if path.startswith("/api/") and not self._unlocked():
                return self._send(403, {"error": "locked"})

            if path == "/api/platform":
                return self._send(200, {"kind": "homelab", "name": NAME,
                                        "version": version.VERSION})

            if path == "/api/state":
                return self._send(200, self._state())

            if path == "/api/layout":
                return self._send(200, load_layout())

            if path == "/api/library":
                return self._send(200, self._library())

            if path == "/api/pairinfo":
                host = self.headers.get("Host", "")
                ok = re.fullmatch(r"[A-Za-z0-9.\-:\[\]]{1,120}", host or "")
                return self._send(200, {"pc": NAME, "url": ("http://%s/" % host) if ok else None,
                                        "app": "aether-homelab"})

            if path == "/api/art":
                ident = (qs.get("id") or [""])[0]
                m = re.fullmatch(r"link:([a-f0-9]{8,32})", ident)
                p = os.path.join(ART_DIR, "link-" + m.group(1)) if m else None
                if not p or not os.path.isfile(p):
                    return self._send(404, {"error": "no art"})
                b = _read(p)
                return self._send(200, b, _sniff(b) or "application/octet-stream",
                                  {"Cache-Control": "private, max-age=300"})

            if path == "/api/manage":
                return self._send(200, self._manage_state())

            if path == "/api/update":
                return self._send(200, updater.state())

            if path == "/api/update/notes":
                tag = (qs.get("tag") or [""])[0]
                try:
                    return self._send(200, updater.notes(tag), "text/plain; charset=utf-8")
                except Exception as e:
                    return self._send(404, {"error": str(e)})

            return self._send(404, {"error": "no such path"})
        except Exception as e:
            print("GET %s failed: %s\n%s" % (path, e, traceback.format_exc()))
            return self._send(500, {"error": str(e)})

    # -- the live state the phone polls ------------------------------------
    def _state(self):
        snap = SAMPLER.get()
        guests = {}
        for g in visible_guests(snap["guests"]):
            guests["%s:%s" % (g["type"], g["vmid"])] = {
                "name": g["name"], "status": g["status"], "cpu": g["cpu"],
                "mem": g["memPct"],
                "uptime": homelab.human_age(g["uptime"]) if g.get("uptime") else ""}
        sv = svc_states()
        return {
            "time": time.strftime("%H:%M"),
            "device": CFG.get("proxmox", {}).get("node") or NAME,
            "stats": snap["stats"], "guests": guests,
            "services": sv["services"], "docker": sv["docker"],
            "links": {l["id"]: {"name": l["name"], "url": l["url"]} for l in links()},
            "error": snap.get("error"), "version": version.VERSION,
        }

    def _library(self):
        snap = SAMPLER.get()
        gl = visible_guests(snap["guests"])
        tabs = [
            {"key": "guest", "name": "VMs & CTs", "items": [
                {"kind": "guest", "ref": "%s:%s" % (g["type"], g["vmid"]), "label": g["name"],
                 "sub": "%s %s · %s" % ("CT" if g["type"] == "lxc" else "VM", g["vmid"], g["status"]),
                 "on": g["status"] == "running", "w": 2, "h": 1} for g in gl]},
            {"key": "stat", "name": "Server", "items": [
                {"kind": "stat", "ref": k, "label": v.get("label", k),
                 "sub": ("%s %s" % (v.get("big", ""), v.get("unit", ""))).strip(),
                 "w": 2, "h": 1} for k, v in sorted(snap["stats"].items())]},
            {"key": "service", "name": "Services",
             "note": "Restarts services running inside this add-on's own container. "
                     "To restart something in one of your other containers, reboot "
                     "that container from its tile. Add services on the manage page.",
             "items": [{"kind": "service", "ref": s["unit"], "label": s.get("label") or s["unit"],
                        "sub": "systemd", "on": svc_states()["services"].get(s["unit"]) == "active",
                        "w": 2, "h": 1} for s in CFG.get("show", {}).get("services", [])]
                      + [{"kind": "docker", "ref": n, "label": n, "sub": "docker · " + st,
                          "on": st == "running", "w": 2, "h": 1}
                         for n, st in sorted(svc_states()["docker"].items())]},
            {"key": "link", "name": "Links", "items": [
                {"kind": "link", "ref": l["id"], "label": l["name"], "sub": l["url"],
                 "w": 2, "h": 1} for l in links()]},
        ]
        return {"tabs": tabs, "total": sum(len(t["items"]) for t in tabs)}

    def _manage_state(self):
        snap = SAMPLER.get()
        show = CFG.get("show", {})
        want = show.get("guests", "all")
        px = CFG.get("proxmox", {})
        return {
            "version": version.VERSION, "node": px.get("node"),
            "proxmox": {"host": px.get("host"), "ok": bool(snap.get("at")) and not snap.get("error"),
                        "error": snap.get("error"), "token": px.get("token_id")},
            "guests_mode": "all" if want == "all" else "list",
            "guests": [{"ref": "%s:%s" % (g["type"], g["vmid"]), "vmid": g["vmid"],
                        "name": g["name"], "type": g["type"], "status": g["status"],
                        "shown": want == "all" or str(g["vmid"]) in {str(x) for x in want}}
                       for g in snap["guests"]],
            "services": show.get("services", []),
            "containers": show.get("containers", []),
            "links": [{**l, "art": os.path.isfile(os.path.join(ART_DIR, "link-" + l["id"]))}
                      for l in links()],
            "auto_update": bool(CFG.get("auto_update")),
            "update": updater.state(),
            "sf": SF.status(),
        }

    # -- POST -------------------------------------------------------------
    def do_POST(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        qs = parse_qs(u.query)
        try:
            if path == "/api/login":
                b = self._body() or {}
                ok, msg = auth.verify_code(str(b.get("code", "")), self._client_ip())
                if not ok:
                    return self._send(401, {"ok": False, "error": msg})
                cookie, age = auth.new_session()
                return self._send(200, {"ok": True}, extra={
                    "Set-Cookie": "rs=%s; Max-Age=%d; Path=/; HttpOnly; SameSite=Lax"
                                  % (cookie, age)})

            if not self._authed():
                return self._send(401, {"error": "sign in"})

            if path == "/api/manage/art":
                return self._art_upload(qs)

            b = self._body()
            if b is None:
                return self._send(413, {"error": "too big"})
            if not isinstance(b, dict):
                b = {}

            if path == "/api/logout":
                return self._send(200, {"ok": True}, extra={
                    "Set-Cookie": "rs=; Max-Age=0; Path=/; SameSite=Lax"})

            if path.startswith("/api/sf/"):
                return self._sf_post(path, b)
            if not self._unlocked():
                return self._send(403, {"error": "locked"})

            if path == "/api/layout":
                return self._send(200, hl_layout.save(sanitize_layout(b)))

            if path == "/api/tile":
                return self._tile(b)

            if path == "/api/links":
                return self._link_edit({"op": "add", **b})

            if path.startswith("/api/manage/"):
                return self._manage_post(path, b)

            # ---- updates ----
            if path == "/api/update/check":
                updater.check(force=True)
                return self._send(200, updater.state())
            if path == "/api/update/skip":
                updater.skip(str(b.get("version", "")), clear=bool(b.get("clear")))
                return self._send(200, updater.state())
            if path == "/api/update/install":
                if not updater.state().get("newer"):
                    return self._send(400, {"error": "already up to date"})
                try:
                    updater.install()
                    return self._send(200, {"ok": True})
                except Exception as e:
                    return self._send(500, {"ok": False, "error": str(e)})

            return self._send(404, {"error": "no such path"})
        except Exception as e:
            print("POST %s failed: %s\n%s" % (path, e, traceback.format_exc()))
            return self._send(500, {"error": str(e)})

    def _tile(self, b):
        kind, ref = str(b.get("kind", "")), str(b.get("ref", ""))[:120]
        if kind == "guest":
            action = str(b.get("action", ""))
            m = hl_layout.GUEST_REF.match(ref)
            if not m or action not in ("start",) + PROTECTED:
                return self._send(400, {"error": "bad guest action"})
            gtype, vmid = ref.split(":")
            if not homelab.allowed_guest(CFG, vmid):
                return self._send(403, {"error": "that guest isn't shown here"})
            if action in PROTECTED and self._stepup("guest." + action, b):
                return
            try:
                homelab.Proxmox(CFG).guest_action(gtype, vmid, action)
            except Exception as e:
                return self._send(502, {"ok": False, "error": "Proxmox: %s" % e})
            print("guest %s %s from %s" % (ref, action, self._client_ip()))
            SAMPLER.poke()
            return self._send(200, {"ok": True})
        if kind == "service":
            if not homelab.allowed_service(CFG, ref):
                return self._send(403, {"error": "that service isn't on the list"})
            ok = homelab.service_restart(ref)
            _svc_cache["at"] = 0
            return self._send(200 if ok else 500, {"ok": ok})
        if kind == "docker":
            want = CFG.get("show", {}).get("containers") or []
            if not (want == "all" or ref in want) or not CONTAINER_RE.match(ref):
                return self._send(403, {"error": "that container isn't on the list"})
            ok = homelab.docker_restart(ref)
            _svc_cache["at"] = 0
            return self._send(200 if ok else 500, {"ok": ok})
        return self._send(400, {"error": "that tile has no action here"})

    # -- links (phone "Add link" and the manage page) ----------------------
    def _link_edit(self, b):
        op = str(b.get("op", ""))
        with CFG_LOCK:
            ls = CFG.setdefault("links", [])
            if op in ("add", "edit"):
                name = str(b.get("name", "")).strip()[:40]
                url = str(b.get("url", "")).strip()
                if not name:
                    return self._send(400, {"error": "Give it a name."})
                if not URL_RE.match(url):
                    return self._send(400, {"error": "The address must start with http:// or https://"})
                if op == "add":
                    if len(ls) >= 60:
                        return self._send(400, {"error": "That's a lot of links - remove some first."})
                    link = {"id": uuid.uuid4().hex[:12], "name": name, "url": url}
                    ls.append(link)
                else:
                    link = next((l for l in ls if l.get("id") == b.get("id")), None)
                    if not link:
                        return self._send(404, {"error": "no such link"})
                    link.update(name=name, url=url)
                save_config()
                return self._send(200, link)
            if op == "delete":
                lid = str(b.get("id", ""))
                CFG["links"] = [l for l in ls if l.get("id") != lid]
                save_config()
                if re.fullmatch(r"[a-f0-9]{8,32}", lid):
                    try:
                        os.remove(os.path.join(ART_DIR, "link-" + lid))
                    except OSError:
                        pass
                return self._send(200, {"ok": True})
        return self._send(400, {"error": "unknown op"})

    def _art_upload(self, qs):
        if not self._unlocked():
            return self._send(403, {"error": "locked"})
        ident = (qs.get("id") or [""])[0]
        m = re.fullmatch(r"link:([a-f0-9]{8,32})", ident)
        if not m or m.group(1) not in {l["id"] for l in links()}:
            return self._send(404, {"error": "no such link"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n <= 0 or n > MAX_ART:
            return self._send(413, {"error": "Pictures up to 512 KB."})
        data = self.rfile.read(n)
        if not _sniff(data):
            return self._send(400, {"error": "PNG, JPEG, WebP or GIF only."})
        os.makedirs(ART_DIR, exist_ok=True)
        p = os.path.join(ART_DIR, "link-" + m.group(1))
        with open(p + ".tmp", "wb") as f:
            f.write(data)
        os.replace(p + ".tmp", p)
        return self._send(200, {"ok": True})

    # -- the manage page's changes -------------------------------------------
    def _manage_post(self, path, b):
        if path == "/api/manage/links":
            return self._link_edit(b)

        if path == "/api/manage/auto":
            with CFG_LOCK:
                CFG["auto_update"] = bool(b.get("on"))
                save_config()
            return self._send(200, {"ok": True, "auto_update": CFG["auto_update"]})

        if path == "/api/manage/show":
            # What the phone may act on. Widening it needs the PIN, if one is set.
            if not self._settings_ok(b):
                return self._send(403, {"error": "stepup", "scope": "manage", "setup": False})
            g = b.get("guests")
            if g == "all":
                guests = "all"
            elif isinstance(g, list):
                guests = [int(x) for x in g if str(x).isdigit()][:500]
            else:
                return self._send(400, {"error": "guests: 'all' or a list of IDs"})
            services = []
            for s in (b.get("services") or [])[:40]:
                unit = str((s or {}).get("unit", "")).strip()
                if not UNIT_RE.match(unit):
                    return self._send(400, {"error": "'%s' isn't a service name" % unit[:40]})
                services.append({"unit": unit, "label": str(s.get("label") or unit)[:40]})
            c = b.get("containers")
            if c == "all":
                containers = "all"
            else:
                containers = [str(x) for x in (c or [])][:60]
                bad = [x for x in containers if not CONTAINER_RE.match(x)]
                if bad:
                    return self._send(400, {"error": "'%s' isn't a container name" % bad[0][:40]})
            with CFG_LOCK:
                CFG.setdefault("show", {}).update(guests=guests, services=services,
                                                  containers=containers)
                save_config()
            _svc_cache["at"] = 0
            print("manage: what the phone controls was changed from %s" % self._client_ip())
            return self._send(200, {"ok": True})

        if path == "/api/manage/signout-all":
            if SF.enabled() and self._stepup("security", b):
                return
            auth.revoke_all()
            return self._send(200, {"ok": True})

        return self._send(404, {"error": "no such path"})

    # -- the PIN ----------------------------------------------------------
    def _sf_post(self, path, b):
        purpose = str(b.get("purpose", ""))
        scope = str(b.get("scope", ""))[:80]
        rs = self._cookie()

        if path == "/api/sf/verify":
            if purpose not in ("unlock", "stepup"):
                return self._send(400, {"error": "unknown purpose"})
            if not SF.enabled():
                return self._send(400, {"error": "No PIN is set up."})
            good, msg = SF.check_pin(b.get("pin"))
            if not good:
                print("[2fa] wrong PIN from %s" % self._client_ip())
                # 400, not 401: the app reads 401 as "signed out".
                return self._send(400, {"error": msg, "sf_failed": True, **SF.status()})
            out = {"ok": True}
            if purpose == "stepup":
                out["stepup"] = SF.make_stepup(scope, rs)
            return self._send(200, out, extra={"Set-Cookie": self._unlock_cookie()})

        if path == "/api/sf/pin":
            # First time: the signed-in session is enough. Changing it needs
            # the current PIN.
            if SF.enabled() and not SF.use_stepup(b.get("stepup"), "settings", rs):
                return self._send(403, {"error": "stepup", "scope": "settings",
                                        "setup": False})
            try:
                SF.set_pin(str(b.get("pin", "")))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"ok": True, **SF.status()},
                              extra={"Set-Cookie": self._unlock_cookie()})

        if path == "/api/sf/lock":
            return self._send(200, {"ok": True},
                              extra={"Set-Cookie": self._unlock_cookie(clear=True)})

        if path == "/api/sf/disable":
            if SF.enabled() and not SF.use_stepup(b.get("stepup"), "settings", rs):
                return self._send(403, {"error": "stepup", "scope": "settings",
                                        "setup": False})
            SF.reset()
            return self._send(200, {"ok": True, **SF.status()},
                              extra={"Set-Cookie": self._unlock_cookie(clear=True)})

        # Passkeys (Face ID) need https - not offered here. And no reset over
        # the network: a forgotten PIN is reset from the container shell with
        #   aether-homelab reset-pin
        return self._send(404, {"error": "not available on the homelab"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=CFG.get("host", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=CFG.get("port", 8788))
    args = ap.parse_args()
    updater.clear_busy()               # a fresh process = any update has settled
    updater.start_auto(lambda: CFG)    # only acts while auto_update is on
    SAMPLER.start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("Aether Homelab on http://%s:%d  (node: %s)"
          % (args.host, args.port, CFG.get("proxmox", {}).get("node")))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
