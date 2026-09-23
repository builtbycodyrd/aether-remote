# remote.py - phone remote for Cody's PC.
#
# Stdlib only (PIL used opportunistically for screenshots). Serves a
# mobile web UI plus a small JSON API. Every request must carry the token
# from config.json, either as ?k=<token>, an X-Token header, or the cookie
# the UI sets on first load.
#
# Launch commands are an ID -> command allowlist in config.json. The phone
# can only name an ID; it can never post a command string to be run.
#
# Usage:  python remote.py [--host H] [--port P]

import argparse
import io
import json
import mimetypes
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import paths    # noqa: E402

# HERE is where the PROGRAM is; anything the user owns goes to paths.data().
# The two are the same folder when running from source, and diverge once
# this is installed - see paths.py.
HERE = paths.ASSET_DIR

import sysctl   # noqa: E402
import auth     # noqa: E402
import stream   # noqa: E402
import library  # noqa: E402
import layout   # noqa: E402
import update   # noqa: E402
import wol      # noqa: E402
import tls      # noqa: E402
import ssl      # noqa: E402


# The scan takes ~1.3s, so cache it and refresh on demand rather than on
# every state poll.
_lib = {"items": None, "at": 0}
_lib_lock = threading.Lock()


def library_items(force=False):
    with _lib_lock:
        if force or _lib["items"] is None or (time.time() - _lib["at"]) > 900:
            try:
                _lib["items"] = library.scan_all()
                _lib["at"] = time.time()
            except Exception as e:
                log("library scan failed: %s" % e)
                if _lib["items"] is None:
                    _lib["items"] = []
        return _lib["items"]


def library_index():
    return {i["id"]: i for i in library_items()}


def known_launches():
    return {i["id"]: i["launch"] for i in library_items()}

# seed(), not data(): a fresh install copies config.default.json in on first
# run, and an update never overwrites one the user has edited.
CONFIG_PATH = paths.seed("config.json")
UI_PATH = paths.asset("ui.html")
LOGIN_PATH = paths.asset("login.html")
LOG_PATH = paths.data("remote.log")

def autostart_command():
    """The VBScript string literal that launches the tray at logon.

    Installed, that is just the exe. From source it is pythonw.exe plus the
    script - named explicitly, because relying on the .py file association
    works on a developer's machine and nowhere else.
    """
    # A VBScript string literal is delimited by " and escapes a quote by
    # doubling it. So a quoted path is three quotes, the path, three quotes:
    #   sh.Run """C:\\Program Files\\x.exe""", 0, False
    def lit(*parts):
        return '"""' + '"" ""'.join(parts) + '"""'

    if paths.FROZEN:
        return lit(sys.executable)

    vbs = paths.asset("tray.vbs")
    if os.path.isfile(vbs):
        return lit(vbs)

    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.isfile(pyw):
        pyw = sys.executable
    return lit(pyw, paths.asset("tray.py"))


DEFAULT_CONFIG = {
    "port": 8787,
    "host": "0.0.0.0",
    "token": "",
    "launchers": [],
}


def log(msg):
    line = "%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    # Under pythonw.exe there is no stdout at all - sys.stdout is None.
    # Writing to it blindly was crashing the server at startup.
    try:
        if sys.stdout is not None:
            sys.stdout.write(line)
            sys.stdout.flush()
    except Exception:
        pass


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    # No URL token any more - TOTP + a signed session cookie replaced it.
    # config.json keeps the key only so old bookmarks fail loudly rather
    # than silently appearing to work.
    cfg["token"] = ""
    return cfg


CFG = load_config()
LAUNCHERS = {l["id"]: l for l in CFG.get("launchers", [])}

SETUP_PATH = paths.data("setup.json")

MANIFEST = {
    "name": "Aether Remote",
    "short_name": "Aether",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#01020a",
    "theme_color": "#01020a",
    "icons": [
        {"src": "/static/icon-192.png", "sizes": "192x192",
         "type": "image/png", "purpose": "any maskable"},
        {"src": "/static/icon-512.png", "sizes": "512x512",
         "type": "image/png", "purpose": "any maskable"},
    ],
}


def setup_state():
    try:
        with open(SETUP_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def setup_done():
    return bool(setup_state().get("done"))


def mark_setup_done():
    s = setup_state()
    s["done"] = True
    s["at"] = time.time()
    with open(SETUP_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=1)


# ------------------------------------------------------------------ handler

class Handler(BaseHTTPRequestHandler):
    server_version = "CodyRemote/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # too chatty; we log the interesting things ourselves

    # -- helpers ----------------------------------------------------------
    def _cookie(self, name):
        for part in (self.headers.get("Cookie") or "").split(";"):
            part = part.strip()
            if part.startswith(name + "="):
                return part[len(name) + 1:]
        return None

    def _authed(self):
        """A valid signed session cookie is the only way in. The old ?k=
        token is gone - TOTP replaced it."""
        return auth.valid_session(self._cookie("rs"))

    def _client_ip(self):
        return self.client_address[0] if self.client_address else "?"

    def _is_local(self):
        """Is this request from the PC itself, rather than over the network?"""
        ip = self._client_ip()
        if ip in ("127.0.0.1", "::1", "localhost"):
            return True
        # When bound to one address, a browser on this PC connects to that
        # address rather than to loopback, so it still counts as local.
        try:
            with open(paths.data("bound.json"), encoding="utf-8") as f:
                return ip == json.load(f).get("host")
        except Exception:
            return False

    def _static(self, path):
        name = os.path.basename(path)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        types = {"png": "image/png", "ico": "image/x-icon",
                 "svg": "image/svg+xml", "webp": "image/webp"}
        p = paths.asset("static", name)
        if ext not in types or not os.path.isfile(p) or os.sep in name:
            return self._send(404, {"error": "no such file"})
        with open(p, "rb") as f:
            return self._send(200, f.read(), types[ext],
                              {"Cache-Control": "public, max-age=604800"})

    # -- first-run wizard --------------------------------------------------
    def _setup_get(self, path, qs):
        if path == "/api/setup/state":
            return self._send(200, {
                "done": setup_done(),
                "enrolled": bool(auth.STATE.get("enrolled")),
                "pc": auth.ACCOUNT,
                "host": CFG.get("host", "auto"),
                "port": CFG.get("port", 8787),
                "tailscale": bool(tailscale_ip()),
                "url": self._phone_url(),
                "autostart": os.path.isfile(self.STARTUP_LNK),
            })
        if path == "/api/setup/totp":
            return self._send(200, {"uri": auth.provisioning_uri(),
                                    "secret": auth.STATE["secret"]})
        if path == "/api/setup/qr":
            kind = qs.get("of", ["url"])[0]
            data = (auth.provisioning_uri() if kind == "totp"
                    else self._phone_url())
            return self._qr_png(data)
        return self._send(404, {"error": "no such path"})

    def _setup_post(self, path, b):
        if path == "/api/setup/network":
            h = str(b.get("host", "auto"))
            if h not in ("auto", "tailscale", "0.0.0.0"):
                return self._send(400, {"error": "bad mode"})
            try:
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    disk = json.load(f)
            except Exception:
                disk = {}
            disk["host"] = h
            CFG["host"] = h
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(disk, f, indent=2)
            log("setup: network mode -> %s" % h)
            return self._send(200, {"ok": True, "host": h})

        if path == "/api/setup/verify":
            # Prove the authenticator actually works BEFORE finishing setup,
            # so nobody ends up locked out of their own PC.
            ok, msg = auth.verify_code(b.get("code", ""), self._client_ip())
            if not ok:
                return self._send(400, {"error": msg})
            return self._send(200, {"ok": True},
                              extra={"Set-Cookie": self._session_cookie()})

        if path == "/api/setup/newsecret":
            auth.reset_enrollment()
            return self._send(200, {"ok": True,
                                    "uri": auth.provisioning_uri()})

        if path == "/api/setup/autostart":
            self._set_autostart(bool(b.get("on", True)))
            return self._send(200, {"ok": True,
                                    "autostart": os.path.isfile(self.STARTUP_LNK)})

        if path == "/api/setup/finish":
            if not auth.STATE.get("enrolled"):
                return self._send(400, {"error":
                                        "verify a code from your authenticator first"})
            mark_setup_done()
            log("first-run setup complete")
            return self._send(200, {"ok": True})

        return self._send(404, {"error": "no such path"})

    def _phone_url(self):
        if getattr(self.server, "https_base", None):
            return self.server.https_base + "/"
        ip = tailscale_ip()
        if ip:
            return "http://%s:%d/" % (ip, CFG.get("port", 8787))
        good = [i for i in lan_ips()
                if not i.startswith(("169.254.", "192.168.56."))]
        return "http://%s:%d/" % (good[0] if good else "127.0.0.1",
                                  CFG.get("port", 8787))

    def _qr_png(self, data):
        try:
            import qrcode
        except ImportError:
            return self._send(501, {"error": "qrcode not installed"})
        q = qrcode.QRCode(box_size=8, border=2)
        q.add_data(data)
        q.make(fit=True)
        img = q.make_image(fill_color="#f1f0f7", back_color="#0b0a18").convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return self._send(200, buf.getvalue(), "image/png")

    def _secure(self):
        """Did this request arrive over HTTPS?"""
        return isinstance(self.connection, ssl.SSLSocket)

    def _session_cookie(self):
        value, max_age = auth.new_session()
        # Over HTTPS the cookie is marked Secure, so a browser never sends it
        # over a plain connection.
        return ("rs=%s; Path=/; Max-Age=%d; SameSite=Lax; HttpOnly%s"
                % (value, max_age, "; Secure" if self._secure() else ""))

    def _send(self, code, body, ctype="application/json; charset=utf-8",
              extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # This is a private LAN/Tailscale tool; don't let a web page
        # anywhere else poke at it from the phone's browser.
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # -- updates -----------------------------------------------------------
    def _update_install(self):
        """Download the new installer and hand off to it.

        The installer's first act is to stop this process, so the reply has
        to be on the wire before it starts. Hence the short delay: answer the
        phone, THEN pull the rug.

        It runs --quiet --no-admin on purpose. The firewall rule and the
        startup task already exist and point at the same folder, so there is
        nothing left that needs admin - which means no UAC prompt nobody is
        there to click.
        """
        try:
            exe = update.fetch_installer()
        except Exception as e:
            log("update: download failed: %s" % e)
            return self._send(502, {"error": str(e)})

        def go():
            time.sleep(1.5)
            try:
                subprocess.Popen([exe, "--quiet", "--no-admin"],
                                 cwd=os.path.dirname(exe),
                                 creationflags=0x08000000,
                                 stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except Exception as e:
                log("update: could not start the installer: %s" % e)

        log("update: installing %s" % os.path.basename(exe))
        threading.Thread(target=go, daemon=True).start()
        return self._send(200, {"ok": True, "installer": os.path.basename(exe),
                                "restarting": True})

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        path = u.path.rstrip("/") or "/"

        if path == "/ping":
            # The only route that answers cross-origin, and deliberately so:
            # the phone's "add a PC" screen has to check an address belongs to
            # an Aether Remote before saving it, and that check is made from a
            # page served by a DIFFERENT PC. Nothing here is authenticated and
            # nothing here is private - it is a liveness probe. Every other
            # route stays same-origin.
            return self._send(200, {"ok": True, "app": "remote",
                                    "pc": auth.ACCOUNT},
                              extra={"Access-Control-Allow-Origin": "*"})

        # Icons and the manifest are needed by the login page and by
        # "Add to Home Screen", both of which happen before any session.
        if path == "/manifest.webmanifest":
            return self._send(200, MANIFEST, "application/manifest+json",
                              {"Cache-Control": "public, max-age=3600"})

        if path.startswith("/static/"):
            return self._static(path)

        # The first-run wizard runs BEFORE anyone can log in, so it cannot sit
        # behind the session gate. Two things keep that from being a hole:
        # it is refused once setup is finished, and it only answers the PC
        # itself - never a phone or anything else on the network.
        if path in ("/setup", "/setup.js") or path.startswith("/api/setup/"):
            if setup_done():
                # Re-running it deliberately is allowed, but only for someone
                # already logged in AND sitting at the PC. The page is no use
                # without its own API, so the whole group opens together or
                # not at all.
                if not (self._authed() and self._is_local()):
                    return self._send(403, {"error": "setup is already done"})
            elif not self._is_local():
                return self._send(403, {"error":
                                        "setup can only be done at the PC"})
            if path == "/setup":
                with open(paths.asset("setup.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/setup.js":
                with open(paths.asset("setup.js"), "rb") as f:
                    return self._send(200, f.read(),
                                      "application/javascript; charset=utf-8")
            return self._setup_get(path, qs)

        # The login page itself must be reachable without a session.
        if path == "/login":
            with open(LOGIN_PATH, "r", encoding="utf-8") as f:
                html = f.read()
            # Whatever PC this is - the name is not baked into the page.
            html = html.replace("{{PC}}", auth.ACCOUNT)
            return self._send(200, html, "text/html; charset=utf-8")

        if not self._authed():
            if path == "/":
                return self._send(302, b"", "text/html",
                                  {"Location": "/login"})
            return self._send(401, {"error": "not logged in"})

        try:
            if path == "/":
                with open(UI_PATH, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")

            if path in ("/app.js", "/pc.js"):
                with open(paths.asset(path.lstrip("/")), "rb") as f:
                    return self._send(200, f.read(),
                                      "application/javascript; charset=utf-8")

            if path == "/pc":
                with open(paths.asset("pc.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")

            if path == "/api/pairinfo":
                ip = tailscale_ip()
                if getattr(self.server, "https_base", None):
                    # The secure address - the only one Face ID works on.
                    url, ts = self.server.https_base + "/", True
                elif ip:
                    url, ts = "http://%s:%d/" % (ip, CFG.get("port", 8787)), True
                else:
                    good = [i for i in lan_ips()
                            if not i.startswith(("169.254.", "192.168.56."))]
                    host = good[0] if good else "127.0.0.1"
                    url, ts = "http://%s:%d/" % (host, CFG.get("port", 8787)), False
                return self._send(200, {"url": url, "tailscale": ts,
                                        "pc": auth.ACCOUNT})

            if path == "/api/qr":
                return self._qr()

            if path == "/api/settings":
                return self._send(200, self._settings())

            if path == "/api/state":
                return self._send(200, self._state())

            if path == "/api/shot":
                return self._shot()

            if path == "/api/stream":
                mon = int(qs.get("mon", ["0"])[0])
                quality = qs.get("q", ["medium"])[0]
                return self._stream(mon, quality)

            if path == "/api/frame":
                # The full-screen viewer's feed: one frame per request, asked
                # for again only once the last one is on screen. See the note
                # in app.js - it is why the picture cannot fall behind.
                mon = int(qs.get("mon", ["0"])[0])
                q = stream.QUALITY.get(qs.get("q", ["medium"])[0],
                                       stream.QUALITY["medium"])
                data = stream.grab_jpeg(mon, q["width"], q["jpeg"])
                return self._send(200, data, "image/jpeg",
                                  {"Cache-Control": "no-store"})

            if path == "/api/layout":
                return self._send(200, layout.load(library_items()))

            if path == "/api/library":
                force = qs.get("rescan", ["0"])[0] == "1"
                items = library_items(force=force)
                q = (qs.get("q", [""])[0] or "").strip().lower()
                if q:
                    items = [i for i in items if q in i["name"].lower()]
                return self._send(200, {
                    "items": [{"id": i["id"], "name": i["name"],
                               "kind": i["kind"], "source": i["source"],
                               "art": bool(i.get("art"))}
                              for i in items[:400]],
                    "total": len(items),
                    "scannedAt": _lib["at"],
                })

            if path == "/api/art":
                return self._art(qs.get("id", [""])[0])

            if path == "/api/browse":
                return self._send(200, self._browse(qs.get("p", [""])[0]))

            if path == "/api/files":
                return self._send(200, self._files(qs.get("p", [""])[0]))

            if path == "/api/download":
                return self._download(qs.get("p", [""])[0])

            if path == "/api/update":
                # Cached: the page polls this, GitHub does not need to hear
                # about it more than once a day.
                return self._send(200, update.state())

            if path == "/api/clipboard":
                # Text only. Capped, and deliberately never logged.
                txt = sysctl.get_clipboard_text() or ""
                return self._send(200, {"text": txt[:100000],
                                        "truncated": len(txt) > 100000})

            if path == "/api/apps":
                return self._send(200, {"apps": sysctl.running_windows()})

            if path == "/api/wol/info":
                return self._send(200, {"primary": wol.primary(),
                                        "adapters": wol.adapters()})

            if path == "/api/wol/piscript":
                return self._send(200, wol.PI_SCRIPT,
                                  "text/plain; charset=utf-8")

            return self._send(404, {"error": "no such path"})
        except Exception as e:
            log("GET %s failed: %s\n%s" % (path, e, traceback.format_exc()))
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        path = u.path.rstrip("/") or "/"

        # The wizard posts before any session exists - same two guards as the
        # GET side: only while setup is unfinished, and only from the PC.
        if path.startswith("/api/setup/"):
            # Always the PC. A logged-in phone must never be able to reach
            # newsecret or the network mode from across the network - being
            # logged in is not the same as being sat in front of the machine.
            if not self._is_local():
                return self._send(403, {"error":
                                        "setup can only be done at the PC"})
            if setup_done() and not self._authed():
                return self._send(403, {"error": "setup is already done"})
            return self._setup_post(path, self._body())

        # Login is the one POST that works without a session.
        if path == "/api/login":
            b = self._body()
            ok, msg = auth.verify_code(b.get("code", ""), self._client_ip())
            if ok:
                log("login ok from %s" % self._client_ip())
                return self._send(200, {"ok": True},
                                  extra={"Set-Cookie": self._session_cookie()})
            log("login failed from %s: %s" % (self._client_ip(), msg))
            return self._send(401, {"error": msg})

        if not self._authed():
            return self._send(401, {"error": "not logged in"})

        try:
            b = self._body()

            if path == "/api/logout":
                return self._send(200, {"ok": True}, extra={
                    "Set-Cookie": "rs=; Path=/; Max-Age=0; SameSite=Lax"})

            # ---- updates ----
            if path == "/api/update/check":
                return self._send(200, update.state(force=True))

            if path == "/api/update/skip":
                if b.get("clear"):
                    update.unskip()
                    log("update: dismissal cleared")
                else:
                    update.skip(b.get("version") or None)
                    log("update: %s dismissed" % (b.get("version") or "latest"))
                return self._send(200, update.state())

            if path == "/api/update/install":
                return self._update_install()

            if path == "/api/clipboard":
                # Text only, capped, not logged. Whatever the phone sends
                # replaces the PC clipboard.
                text = str(b.get("text", ""))[:100000]
                okset = sysctl.set_clipboard_text(text)
                return self._send(200 if okset else 500,
                                  {"ok": okset, "chars": len(text)})

            if path == "/api/endtask":
                r = sysctl.end_task(b.get("pid"))
                log("end task pid=%s -> %s" % (b.get("pid"), r.get("ok")))
                return self._send(200 if r.get("ok") else 400, r)

            if path == "/api/wol/send":
                # This PC sends a magic packet on its LAN - the fallback for
                # when another Aether PC on the same network is awake. The Pi
                # is the usual sender and the phone calls it directly.
                try:
                    wol.send_wol(str(b.get("mac", "")))
                    return self._send(200, {"ok": True})
                except Exception as e:
                    return self._send(400, {"ok": False, "error": str(e)})

            # ---- desktop input ----
            if path == "/api/click":
                stream.click(int(b.get("mon", 0)),
                             float(b.get("x", 0)), float(b.get("y", 0)),
                             b.get("button", "left"), bool(b.get("double")))
                return self._send(200, {"ok": True})

            if path == "/api/movepad":
                stream.move_relative(float(b.get("dx", 0)), float(b.get("dy", 0)))
                return self._send(200, {"ok": True})

            if path == "/api/tap":
                # Trackpad mode: click where the cursor already is. Sending
                # coordinates here would undo the positioning the user just
                # did with movepad.
                return self._send(200, stream.click_here(
                    b.get("button", "left"), bool(b.get("double"))))

            if path == "/api/drag":
                stream.drag(int(b.get("mon", 0)),
                            float(b.get("x1", 0)), float(b.get("y1", 0)),
                            float(b.get("x2", 0)), float(b.get("y2", 0)))
                return self._send(200, {"ok": True})

            if path == "/api/scroll":
                stream.scroll(int(b.get("amount", 0)))
                return self._send(200, {"ok": True})

            if path == "/api/type":
                return self._send(200, stream.type_text(str(b.get("text", ""))))

            if path == "/api/press":
                return self._send(200, stream.press(str(b.get("name", "")),
                                                    b.get("mods") or []))

            # ---- layout / tiles ----
            if path == "/api/layout":
                clean = layout.sanitize(b, known_launches())
                layout.save(clean)
                log("layout saved: %d sections, %d tiles" % (
                    len(clean["sections"]),
                    sum(len(s["tiles"]) for s in clean["sections"])))
                return self._send(200, clean)

            if path == "/api/tile":
                return self._tile(b)

            if path == "/api/scene":
                return self._scene(b)

            if path == "/api/addapp":
                return self._addapp(b)

            # ---- desktop app only ----
            if path == "/api/pickapp":
                return self._pickapp()

            if path == "/api/art/custom":
                return self._custom_art(b)

            if path == "/api/settings":
                return self._save_settings(b)

            if path == "/api/security":
                act = str(b.get("action", ""))
                if act == "revoke":
                    auth.revoke_all()
                    log("all sessions revoked")
                    return self._send(200, {"ok": True})
                if act == "reset":
                    auth.reset_enrollment()
                    log("TOTP secret reset")
                    return self._send(200, {"ok": True})
                return self._send(400, {"error": "unknown action"})

            # ---- power ----
            if path == "/api/power":
                return self._power(b)

            if path == "/api/volume":
                v = int(b.get("value", 50))
                sysctl.set_volume(v)
                # Moving the slider retargets the keeper rather than fighting it.
                if sysctl.keeper.enabled:
                    sysctl.keeper.target = max(0, min(100, v))
                return self._send(200, self._state())

            if path == "/api/step":
                d = int(b.get("delta", 0))
                cur = sysctl.status()["volume"]
                v = max(0, min(100, cur + d))
                sysctl.set_volume(v)
                if sysctl.keeper.enabled:
                    sysctl.keeper.target = v
                return self._send(200, self._state())

            if path == "/api/mute":
                want = b.get("value", "toggle")
                if want == "toggle":
                    want = not sysctl.status()["muted"]
                sysctl.set_mute(bool(want))
                return self._send(200, self._state())

            if path == "/api/keeper":
                if b.get("enabled"):
                    tgt = b.get("target")
                    if tgt is None:
                        tgt = sysctl.status()["volume"]
                    sysctl.keeper.arm(int(tgt))
                else:
                    sysctl.keeper.disarm()
                return self._send(200, self._state())

            if path == "/api/key":
                sysctl.tap_key(str(b.get("name", "")))
                return self._send(200, {"ok": True})

            if path == "/api/device":
                did = b.get("id")
                if not did:
                    return self._send(400, {"error": "missing id"})
                sysctl.set_default_device(did)
                log("default device -> %s" % did)
                return self._send(200, self._state())

            if path == "/api/launch":
                lid = b.get("id")
                item = LAUNCHERS.get(lid)
                if not item:
                    return self._send(400, {"error": "unknown launcher"})
                sysctl.run_detached(item["cmd"])
                log("launch %s" % lid)
                return self._send(200, {"ok": True, "ran": item["label"]})

            if path == "/api/lock":
                sysctl.lock_workstation()
                return self._send(200, {"ok": True})

            if path == "/api/screenoff":
                sysctl.screen_off()
                return self._send(200, {"ok": True})

            return self._send(404, {"error": "no such path"})
        except Exception as e:
            log("POST %s failed: %s\n%s" % (path, e, traceback.format_exc()))
            return self._send(500, {"error": str(e)})

    # -- streaming --------------------------------------------------------
    def _stream(self, mon, quality):
        """multipart/x-mixed-replace - <img src> renders it natively, so no
        websocket and no client-side decoding."""
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store, no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        log("stream open mon=%s q=%s from %s" % (mon, quality, self._client_ip()))
        n = 0
        try:
            for chunk in stream.mjpeg_frames(mon, quality):
                self.wfile.write(chunk)
                n += 1
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            log("stream error after %d frames: %s" % (n, e))
        log("stream closed after %d frames" % n)
        self.close_connection = True

    # -- power ------------------------------------------------------------
    POWER = {
        "sleep":    "rundll32.exe powrprof.dll,SetSuspendState 0,1,0",
        "lock":     None,
        "restart":  "shutdown /r /t 0",
        "shutdown": "shutdown /s /t 0",
        "signout":  "shutdown /l",
    }
    DESTRUCTIVE = {"restart", "shutdown", "signout"}

    def _power(self, b):
        action = str(b.get("action", ""))
        if action not in self.POWER:
            return self._send(400, {"error": "unknown power action"})

        # A pocket tap must not be able to kill the machine mid-game: the
        # destructive ones only fire with an explicit confirm flag, which the
        # UI only sets after a second deliberate tap.
        if action in self.DESTRUCTIVE and not b.get("confirm"):
            return self._send(409, {"error": "needs confirm",
                                    "confirm": True, "action": action})

        if action == "lock":
            sysctl.lock_workstation()
        else:
            sysctl.run_detached(self.POWER[action])
        log("power: %s (from %s)" % (action, self._client_ip()))
        return self._send(200, {"ok": True, "action": action})

    # -- tiles ------------------------------------------------------------
    def _tile(self, b):
        """One entry point for 'the user tapped a tile'. The phone names a
        tile kind and a ref; it never sends anything executable."""
        kind = str(b.get("kind", ""))
        ref = str(b.get("ref", ""))

        if kind in ("app", "game"):
            cmd = known_launches().get(ref)
            if not cmd:
                return self._send(400, {"error": "not in the library"})
            launch_cmd = self._launch_cmd(cmd)
            # A tile may carry pre-launch actions ("set volume to 40, then
            # launch"). Look them up on the saved tile by its id - never from
            # the phone's payload - then run them and launch, off-thread so a
            # Wait step doesn't hold the request open.
            actions = self._tile_actions(str(b.get("id", "")))
            if actions:
                def _pre():
                    self._run_steps(actions, "launch %s" % ref)
                    sysctl.run_detached(launch_cmd)
                threading.Thread(target=_pre, daemon=True).start()
                log("launch %s (%d pre-actions)" % (ref, len(actions)))
            else:
                sysctl.run_detached(launch_cmd)
                log("launch %s" % ref)
            return self._send(200, {"ok": True})

        if kind == "action":
            spec = layout.ACTIONS.get(ref)
            if not spec:
                return self._send(400, {"error": "unknown action"})
            if spec["kind"] == "key":
                sysctl.tap_key(ref.split(".", 1)[1])
            elif spec["kind"] == "screenoff":
                sysctl.screen_off()
            elif spec["kind"] == "power":
                return self._power({"action": ref.split(".", 1)[1],
                                    "confirm": b.get("confirm")})
            return self._send(200, {"ok": True})

        if kind == "toggle":
            if ref == "mute":
                want = b.get("value")
                if want is None:
                    want = not sysctl.status()["muted"]
                sysctl.set_mute(bool(want))
            elif ref == "keeper":
                want = b.get("value")
                if want is None:
                    want = not sysctl.keeper.enabled
                if want:
                    sysctl.keeper.arm(int(b.get("target")
                                          or sysctl.status()["volume"]))
                else:
                    sysctl.keeper.disarm()
            else:
                return self._send(400, {"error": "unknown toggle"})
            return self._send(200, self._state())

        if kind == "scene":
            return self._scene({"run": ref})

        return self._send(400, {"error": "unknown tile kind"})

    @staticmethod
    def _tile_actions(tile_id):
        """The saved pre-launch actions for a tile, found by its id. Returns
        [] for anything unknown - the id comes from the phone, so it only ever
        selects one of OUR tiles; it can never carry an action of its own."""
        if not tile_id:
            return []
        lay = layout.load(library_items())
        for sec in lay.get("sections", []):
            for t in sec.get("tiles", []):
                if t.get("id") == tile_id and t.get("kind") in ("app", "game"):
                    return t.get("actions") or []
        return []

    @staticmethod
    def _launch_cmd(cmd):
        """steam:// and other protocol urls need the shell to open them."""
        if "://" in cmd and not cmd.lower().startswith(("cmd", "powershell")):
            return 'cmd /c start "" "%s"' % cmd
        if cmd.lower().endswith(".lnk"):
            return 'cmd /c start "" "%s"' % cmd
        return cmd

    # -- scenes -----------------------------------------------------------
    def _scene(self, b):
        cur = layout.load(library_items())

        if b.get("run"):
            scene = next((s for s in cur.get("scenes", [])
                          if s["id"] == b["run"]), None)
            if not scene:
                return self._send(404, {"error": "no such scene"})
            # Run it off-thread so a Wait step does not hold the request open.
            threading.Thread(target=self._run_scene, args=(scene,),
                             daemon=True).start()
            return self._send(200, {"ok": True, "name": scene["name"]})

        if b.get("delete"):
            cur["scenes"] = [s for s in cur.get("scenes", [])
                             if s["id"] != b["delete"]]
            layout.save(cur)
            return self._send(200, cur)

        # Create or replace one scene.
        incoming = dict(cur)
        incoming["scenes"] = [s for s in cur.get("scenes", [])
                              if s["id"] != b.get("id")] + [b]
        clean = layout.sanitize(incoming, known_launches())
        layout.save(clean)
        return self._send(200, clean)

    def _run_scene(self, scene):
        self._run_steps(scene.get("steps", []), scene.get("name"))

    def _run_steps(self, steps, name=""):
        """Execute a list of steps in order. Used by scenes AND by a tile's
        pre-launch actions - one executor, so they can never drift apart."""
        for i, step in enumerate(steps):
            op = step.get("op")
            try:
                if op == "volume":
                    sysctl.set_volume(int(step.get("value", 50)))
                elif op == "mute":
                    sysctl.set_mute(True)
                elif op == "unmute":
                    sysctl.set_mute(False)
                elif op == "keeper":
                    if step.get("value"):
                        sysctl.keeper.arm(sysctl.status()["volume"])
                    else:
                        sysctl.keeper.disarm()
                elif op == "device":
                    sysctl.set_default_device(str(step.get("value")))
                elif op == "open":
                    sysctl.run_detached(self._launch_cmd(step["launch"]))
                elif op == "close":
                    exe = os.path.basename(str(step.get("value") or ""))
                    if exe.lower().endswith(".exe"):
                        sysctl.run_detached("taskkill /IM %s /T /F" % exe)
                elif op == "key":
                    sysctl.tap_key(str(step.get("value", "playpause")))
                elif op == "type":
                    stream.type_text(str(step.get("value", "")))
                elif op == "wait":
                    time.sleep(max(0.0, min(60.0, float(step.get("value", 1)))))
                elif op == "screenoff":
                    sysctl.screen_off()
                elif op == "power":
                    act = str(step.get("value", "lock"))
                    if act == "lock":
                        sysctl.lock_workstation()
                    elif act in self.POWER and self.POWER[act]:
                        sysctl.run_detached(self.POWER[act])
            except Exception as e:
                # Stop at the failing step rather than half-running the rest.
                log("steps '%s' failed at step %d (%s): %s"
                    % (name, i + 1, op, e))
                return
        if name:
            log("steps '%s' finished" % name)

    # -- add an app by browsing the PC -----------------------------------
    def _browse(self, p):
        """Read-only directory listing for the 'add an app' picker."""
        if not p:
            roots = []
            for d in ("C:\\", "D:\\", "E:\\"):
                if os.path.isdir(d):
                    roots.append({"name": d, "path": d, "dir": True})
            for name in ("Desktop", "Downloads", "Documents"):
                d = os.path.join(os.environ.get("USERPROFILE", ""), name)
                if os.path.isdir(d):
                    roots.append({"name": name, "path": d, "dir": True})
            for d in (r"C:\Program Files", r"C:\Program Files (x86)"):
                if os.path.isdir(d):
                    roots.append({"name": os.path.basename(d), "path": d,
                                  "dir": True})
            return {"path": "", "up": None, "entries": roots}

        p = os.path.abspath(p)
        if not os.path.isdir(p):
            return {"path": p, "up": os.path.dirname(p), "entries": []}

        entries = []
        try:
            for name in sorted(os.listdir(p), key=str.lower):
                full = os.path.join(p, name)
                try:
                    isdir = os.path.isdir(full)
                except OSError:
                    continue
                if isdir:
                    if name.startswith("$") or name.lower() == "windows":
                        continue
                    entries.append({"name": name, "path": full, "dir": True})
                elif name.lower().endswith((".exe", ".lnk", ".url", ".bat")):
                    entries.append({"name": name, "path": full, "dir": False})
        except PermissionError:
            return {"path": p, "up": os.path.dirname(p), "entries": [],
                    "error": "no permission to read that folder"}
        return {"path": p, "up": os.path.dirname(p) or None,
                "entries": entries[:600]}

    def _files(self, p):
        """Directory listing for the phone's Files tab: folders and ALL files
        with their sizes. Download-only - there is no write path anywhere."""
        if not p:
            roots = []
            for d in ("C:\\", "D:\\", "E:\\"):
                if os.path.isdir(d):
                    roots.append({"name": d, "path": d, "dir": True})
            for name in ("Desktop", "Downloads", "Documents", "Pictures",
                         "Videos", "Music"):
                d = os.path.join(os.environ.get("USERPROFILE", ""), name)
                if os.path.isdir(d):
                    roots.append({"name": name, "path": d, "dir": True})
            return {"path": "", "up": None, "entries": roots}

        p = os.path.abspath(p)
        if not os.path.isdir(p):
            return {"path": p, "up": os.path.dirname(p) or None, "entries": []}

        dirs, files = [], []
        try:
            for name in sorted(os.listdir(p), key=str.lower):
                full = os.path.join(p, name)
                try:
                    if os.path.isdir(full):
                        if name.startswith("$"):
                            continue
                        dirs.append({"name": name, "path": full, "dir": True})
                    elif os.path.isfile(full):
                        files.append({"name": name, "path": full, "dir": False,
                                      "size": os.path.getsize(full)})
                except OSError:
                    continue
        except PermissionError:
            return {"path": p, "up": os.path.dirname(p) or None, "entries": [],
                    "error": "no permission to read that folder"}
        return {"path": p, "up": os.path.dirname(p) or None,
                "entries": (dirs + files)[:1500]}

    def _download(self, p):
        """Stream one file to the phone as an attachment. Read-only, and the
        only thing it can do is hand back bytes that already exist on disk."""
        p = os.path.abspath(p)
        if not os.path.isfile(p):
            return self._send(404, {"error": "no such file"})
        try:
            size = os.path.getsize(p)
            f = open(p, "rb")
        except OSError as e:
            return self._send(403, {"error": str(e)})

        name = os.path.basename(p)
        ascii_name = name.encode("ascii", "replace").decode("ascii") \
            .replace('"', "")
        disp = ("attachment; filename=\"%s\"; filename*=UTF-8''%s"
                % (ascii_name, quote(name)))
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", disp)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            with f:
                while True:
                    chunk = f.read(262144)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        log("download %s (%d bytes)" % (name, size))
        return None

    def _addapp(self, b):
        """Turn a browsed .exe into a library entry so a tile can use it."""
        p = str(b.get("path", ""))
        if not os.path.isfile(p) or not p.lower().endswith(
                (".exe", ".lnk", ".url", ".bat")):
            return self._send(400, {"error": "pick a program"})
        name = str(b.get("name") or os.path.splitext(os.path.basename(p))[0])
        item = {
            "id": "custom-" + "".join(c if c.isalnum() else "-"
                                      for c in p.lower())[-60:],
            "name": name[:60], "kind": "app", "source": "Added by you",
            "launch": p, "art": None, "installdir": os.path.dirname(p),
        }
        # Persist it, or it vanishes on the next restart. The PC app's file
        # picker writes the same file.
        store = paths.data("custom_apps.json")
        try:
            with open(store, "r", encoding="utf-8") as f:
                apps = json.load(f)
        except Exception:
            apps = []
        apps = [a for a in apps if a.get("id") != item["id"]] + [item]
        try:
            with open(store, "w", encoding="utf-8") as f:
                json.dump(apps, f, indent=1)
        except Exception as e:
            log("could not save custom app: %s" % e)

        with _lib_lock:
            items = _lib["items"] or []
            items = [i for i in items if i["id"] != item["id"]] + [item]
            _lib["items"] = items
        layout.icon_for(p)
        log("added app %s" % p)
        return self._send(200, {"ok": True, "item": {
            "id": item["id"], "name": item["name"], "kind": "app",
            "source": item["source"], "art": False}})

    # Game exes are rarely at the top of the install folder - ARK's lives in
    # ShooterGame\Binaries\Win64 - so walk a few levels and skip the helper
    # binaries every launcher ships.
    EXE_NOISE = ("unrealcefsubprocess", "crashreport", "easyanticheat",
                 "vcredist", "directx", "dxsetup", "uninstall", "setup",
                 "launcher_installer", "battleye", "epicwebhelper")

    @staticmethod
    def _find_exe(root, name=None):
        if not root or not os.path.isdir(root):
            return ""
        want = "".join(c for c in (name or "").lower() if c.isalnum())
        best, best_score = "", -1
        base_depth = root.rstrip("\\/").count(os.sep)
        for dirpath, dirs, files in os.walk(root):
            if dirpath.count(os.sep) - base_depth > 3:
                dirs[:] = []
                continue
            for fn in files:
                if not fn.lower().endswith(".exe"):
                    continue
                low = fn.lower()
                if any(n in low for n in Handler.EXE_NOISE):
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                stem = "".join(c for c in os.path.splitext(low)[0]
                               if c.isalnum())
                score = size
                if want and (stem in want or want in stem):
                    score += 10 ** 12          # a name match beats any size
                if score > best_score:
                    best, best_score = full, score
        return best

    # -- artwork ----------------------------------------------------------
    def _art(self, item_id):
        it = library_index().get(item_id)
        if not it:
            return self._send(404, {"error": "unknown item"})

        # Artwork you dropped in from the desktop app always wins.
        path = self._custom_art_for(item_id) or it.get("art")
        if not path or not os.path.isfile(path):
            # No box art (Epic, custom apps): fall back to the exe icon.
            target = it.get("launch") or ""
            if not os.path.isfile(target):
                target = self._find_exe(it.get("installdir"), it.get("name"))
            path = layout.icon_for(target) if target else None

        if not path or not os.path.isfile(path):
            return self._send(404, {"error": "no art"})

        ctype = "image/png" if path.lower().endswith(".png") else "image/jpeg"
        try:
            with open(path, "rb") as f:
                data = f.read()
        except Exception as e:
            return self._send(500, {"error": str(e)})
        return self._send(200, data, ctype,
                          {"Cache-Control": "private, max-age=86400"})

    # -- desktop-app only -------------------------------------------------
    def _pickapp(self):
        """Native Windows file dialog. Only reachable from this PC - a phone
        cannot pop a dialog on a screen nobody is looking at."""
        if self._client_ip() not in ("127.0.0.1", "::1"):
            b = None
            try:
                with open(paths.data("bound.json"), encoding="utf-8") as f:
                    b = json.load(f)
            except Exception:
                pass
            if not b or self._client_ip() != b.get("host"):
                return self._send(403, {"error":
                                        "the file picker only opens on the PC"})

        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            p = filedialog.askopenfilename(
                title="Pick a program to add",
                filetypes=[("Programs", "*.exe;*.lnk;*.url;*.bat"),
                           ("All files", "*.*")],
                initialdir=os.environ.get("ProgramFiles", "C:\\"))
        finally:
            root.destroy()

        if not p:
            return self._send(200, {"cancelled": True})
        return self._addapp({"path": p})

    def _custom_art(self, b):
        """Replace an item's artwork with an image dropped on the desktop app.

        Stored under our own folder and recorded by item id - we never write
        into Steam's cache or the game's install folder.
        """
        item_id = str(b.get("id", ""))
        if not library_index().get(item_id):
            return self._send(400, {"error": "unknown item"})
        try:
            import base64
            raw = base64.b64decode(b.get("data", ""), validate=True)
        except Exception:
            return self._send(400, {"error": "bad image data"})
        if not raw or len(raw) > 8 * 1024 * 1024:
            return self._send(400, {"error": "image missing or over 8 MB"})

        try:
            from PIL import Image
            img = Image.open(io.BytesIO(raw))
            img.load()
        except Exception:
            return self._send(400, {"error": "that file is not an image"})

        art_dir = paths.data("art")
        os.makedirs(art_dir, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in item_id)
        out = os.path.join(art_dir, safe + ".png")
        img.convert("RGBA").save(out, "PNG")

        store = paths.data("custom_art.json")
        try:
            with open(store, encoding="utf-8") as f:
                m = json.load(f)
        except Exception:
            m = {}
        m[item_id] = out
        with open(store, "w", encoding="utf-8") as f:
            json.dump(m, f, indent=1)

        log("custom art set for %s" % item_id)
        return self._send(200, {"ok": True})

    @staticmethod
    def _custom_art_for(item_id):
        try:
            with open(paths.data("custom_art.json"), encoding="utf-8") as f:
                p = json.load(f).get(item_id)
            return p if p and os.path.isfile(p) else None
        except Exception:
            return None

    def _qr(self):
        try:
            import qrcode
        except ImportError:
            return self._send(501, {"error": "qrcode not installed"})
        ip = tailscale_ip()
        if ip:
            url = "http://%s:%d/" % (ip, CFG.get("port", 8787))
        else:
            good = [i for i in lan_ips()
                    if not i.startswith(("169.254.", "192.168.56."))]
            url = "http://%s:%d/" % (good[0] if good else "127.0.0.1",
                                     CFG.get("port", 8787))
        q = qrcode.QRCode(box_size=8, border=2)
        q.add_data(url)
        q.make(fit=True)
        img = q.make_image(fill_color="#f1f0f7", back_color="#0b0a18").convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return self._send(200, buf.getvalue(), "image/png")

    STARTUP_LNK = os.path.join(
        os.environ.get("APPDATA", ""),
        r"Microsoft\Windows\Start Menu\Programs\Startup\aether-remote.vbs")

    def _settings(self):
        return {
            "host": CFG.get("host", "auto"),
            "port": CFG.get("port", 8787),
            "pc": auth.ACCOUNT,
            "autostart": os.path.isfile(self.STARTUP_LNK),
            "autostartDetail": self.STARTUP_LNK if os.path.isfile(self.STARTUP_LNK)
                               else "Not set up yet.",
        }

    def _save_settings(self, b):
        changed_net = False
        if "host" in b:
            h = str(b["host"])
            if h in ("auto", "tailscale", "0.0.0.0", "127.0.0.1"):
                CFG["host"] = h
                changed_net = True
        if "port" in b:
            try:
                p = int(b["port"])
                if 1024 <= p <= 65535:
                    CFG["port"] = p
                    changed_net = True
            except Exception:
                pass

        if changed_net:
            try:
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    disk = json.load(f)
            except Exception:
                disk = {}
            disk["host"] = CFG.get("host", "auto")
            disk["port"] = CFG.get("port", 8787)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(disk, f, indent=2)
            log("network settings changed -> %s:%s (restarting)"
                % (disk["host"], disk["port"]))

        if "autostart" in b:
            self._set_autostart(bool(b["autostart"]))

        out = self._settings()
        if changed_net:
            # Exit so whatever supervises us brings it back on the new setting.
            threading.Timer(0.8, lambda: os._exit(0)).start()
            out["restarting"] = True
        return self._send(200, out)

    def _set_autostart(self, on):
        if not on:
            try:
                os.remove(self.STARTUP_LNK)
            except Exception:
                pass
            return
        try:
            with open(self.STARTUP_LNK, "w", encoding="utf-8") as f:
                f.write(
                    "' Aether Remote - start at logon\n"
                    'Set sh = CreateObject("WScript.Shell")\n'
                    'sh.Run %s, 0, False\n' % autostart_command())
        except Exception as e:
            log("could not write autostart: %s" % e)

    # -- payloads ---------------------------------------------------------
    def _state(self):
        st = sysctl.status()
        st["keeper"] = sysctl.keeper.info()
        st["memory"] = sysctl.memory_info()
        st["stats"] = sysctl.system_stats()
        st["nowplaying"] = sysctl.now_playing()
        st["foreground"] = sysctl.foreground_app()
        st["devices"] = sysctl.devices()["devices"]
        st["launchers"] = [{"id": l["id"], "label": l["label"],
                            "group": l.get("group", "")}
                           for l in CFG.get("launchers", [])]
        st["monitors"] = stream.monitors()
        st["time"] = time.strftime("%H:%M")
        return st

    def _shot(self):
        try:
            from PIL import ImageGrab
        except ImportError:
            return self._send(501, {"error": "Pillow not installed on the PC"})
        img = ImageGrab.grab(all_screens=False)
        img.thumbnail((1000, 1000))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=70)
        return self._send(200, buf.getvalue(), "image/jpeg")


def _find_tailscale():
    """Tailscale is not always on C:, and may not be installed at all."""
    cands = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(var)
        if base:
            cands.append(os.path.join(base, "Tailscale", "tailscale.exe"))
    cands.append(r"C:\Program Files\Tailscale\tailscale.exe")
    for c in cands:
        if os.path.isfile(c):
            return c
    from shutil import which
    return which("tailscale") or cands[-1]


TAILSCALE_EXE = _find_tailscale()


def _is_tailscale_ip(ip):
    """Tailscale hands out addresses from the CGNAT block 100.64.0.0/10."""
    try:
        a, b = ip.split(".")[:2]
        return int(a) == 100 and 64 <= int(b) <= 127
    except Exception:
        return False


def tailscale_ip():
    try:
        out = subprocess.run([TAILSCALE_EXE, "ip", "-4"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=0x08000000)
        for line in out.stdout.splitlines():
            ip = line.strip()
            if _is_tailscale_ip(ip):
                return ip
    except Exception:
        pass
    return None


def resolve_host(host):
    """Three modes:

    "tailscale" - bind the tailnet address ONLY, so the remote is not
                  reachable from the Wi-Fi at all. Waits for Tailscale,
                  because at logon the remote starts first.
    "auto"      - use Tailscale when it is there, otherwise the LAN. This is
                  the default for anyone who is not running a tailnet:
                  same-Wi-Fi works with zero setup, and TOTP still gates it.
    anything else - taken literally (e.g. "0.0.0.0", "127.0.0.1").
    """
    if host == "auto":
        ip = tailscale_ip()
        if ip:
            log("tailscale available - binding %s" % ip)
            return ip
        log("no tailscale - binding all interfaces (LAN). Login is still TOTP.")
        return "0.0.0.0"

    if host != "tailscale":
        return host

    waited = 0
    while True:
        ip = tailscale_ip()
        if ip:
            if waited:
                log("tailscale came up after %ds" % waited)
            return ip
        if waited % 30 == 0:
            log("waiting for a tailscale address (%ds)..." % waited)
        time.sleep(5)
        waited += 5


def lan_ips():
    out = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            ip = info[4][0]
            if ip not in out and not ip.startswith("127."):
                out.append(ip)
    except Exception:
        pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=CFG.get("host", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=CFG.get("port", 8787))
    a = ap.parse_args()

    host = resolve_host(a.host)

    # HTTPS when this PC is on a tailnet with HTTPS certificates turned on -
    # needed for Face ID / passkeys, and it encrypts on top of Tailscale.
    # Anyone without that keeps plain HTTP exactly as before.
    https_base, ctx, domain, had = None, None, None, False
    if CFG.get("https", "auto") != "off" and _is_tailscale_ip(host):
        domain = tls.cert_domain(TAILSCALE_EXE)
        if domain:
            had = tls.have_cert()
            # First time: fetch before serving. After that, start at once on
            # the cached certificate and refresh it in the background.
            if had or tls.fetch(TAILSCALE_EXE, domain, log):
                try:
                    ctx = tls.context()
                    https_base = "https://%s:%d" % (domain, a.port)
                except Exception as e:
                    log("https unavailable, serving http: %s" % e)
        else:
            log("tailnet has no HTTPS certificates - serving http")

    srv = tls.dual_server(ThreadingHTTPServer)((host, a.port), Handler)
    srv.daemon_threads = True
    if ctx:
        srv.ssl_ctx = ctx
        srv.https_base = https_base
        srv.plain_handler = tls.redirect_handler(BaseHTTPRequestHandler)
        # Started on a cached certificate? Refresh it shortly. Just fetched
        # one? The next check is the normal interval away.
        tls.start_renewal(srv, TAILSCALE_EXE, domain, log,
                          first_delay=30 if had else tls.RENEW_EVERY)

    # Write our own pid file so it is correct no matter how we were
    # launched (remote.ps1, the Startup shortcut, or by hand).
    try:
        with open(paths.data("remote.pid"), "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

    # Publish where we actually bound, so the tray app (and anything else)
    # does not have to guess - "localhost" is wrong when we are bound to the
    # tailnet address only.
    reachable = host if host != "0.0.0.0" else (lan_ips() or ["127.0.0.1"])[0]
    url = (https_base + "/") if https_base else "http://%s:%d/" % (reachable, a.port)
    try:
        with open(paths.data("bound.json"), "w", encoding="utf-8") as f:
            json.dump({"host": host, "port": a.port, "url": url,
                       "https": bool(https_base),
                       # plain http still answers /ping on the same port, so
                       # local health checks never depend on .ts.net DNS
                       "ping": "http://%s:%d/ping" % (reachable, a.port),
                       "tailscale": _is_tailscale_ip(host),
                       "at": time.time()}, f)
    except Exception:
        pass

    log("listening on %s:%d%s" % (host, a.port,
                                  "  (tailscale only)" if _is_tailscale_ip(host) else ""))
    log("  %s  (TOTP login%s)" % (url, ", https" if https_base else ""))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
