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
import secrets
import json
import mimetypes
import os
import re
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
import secondfactor   # noqa: E402
import version        # noqa: E402
import media          # noqa: E402
import files          # noqa: E402
import jellyfin       # noqa: E402
import mixer          # noqa: E402
import timer          # noqa: E402
import push           # noqa: E402
import watch          # noqa: E402
import chat           # noqa: E402
import memory         # noqa: E402

# Tests only: treat EVERY request as coming from a phone, so a browser on this
# PC can exercise the Face ID / PIN lock. It can only make the server stricter
# (it removes the at-the-PC exemption, never adds access), and it is ignored
# unless the data folder has been pointed at a throwaway one.
_AS_PHONE = (os.environ.get("AETHER_TEST_AS_PHONE") == "1"
             and bool(os.environ.get("AETHER_DATA")))

# Face ID / PIN on top of the session. Keyed off the session-signing key, so
# "sign out every phone" also voids every unlock and step-up token.
SF = secondfactor.SecondFactor(paths.data("sf.json"),
                               lambda: auth.STATE["server_key"],
                               log=lambda m: log(m))


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


# The chat's "needs your OK" requests waiting on the phone, and the last few
# screenshots it took (shown in the chat, fetched with the session).
_CONFIRMS = {}
_SHOTS = {}


def _procs(image):
    """How many processes with this image name are running."""
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s" % image, "/NH", "/FO", "CSV"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return sum(1 for line in out.splitlines() if line.lower().startswith('"%s"' % image.lower()))
    except Exception:
        return 0


def _steam_unstick():
    """Steam sometimes hangs after updating itself: steam.exe is running but
    its window (steamwebhelper) never comes up, and it ignores every launch.
    Give it a moment, then restart it. True when it had to be restarted."""
    if not _procs("steam.exe"):
        return False                      # not running: steam:// starts it
    for _ in range(10):
        if _procs("steamwebhelper.exe"):
            return False
        time.sleep(2)
    root = library.steam_root()
    if not root:
        return False
    log("Steam looked stuck (no window after updating) - restarting it")
    subprocess.run(["taskkill", "/IM", "steam.exe", "/F"], capture_output=True, timeout=15,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    time.sleep(3)
    sysctl.run_detached('cmd /c start "" "%s"' % os.path.join(root, "steam.exe"))
    for _ in range(30):
        time.sleep(2)
        if _procs("steamwebhelper.exe"):
            time.sleep(6)                 # let it finish signing in
            break
    return True


def _steam_started(appid, since, wait):
    """Did Steam report the game running after `since`? Steam writes that to
    its content log ("AppID 123 state changed : ...,App Running")."""
    root = library.steam_root()
    if not root:
        return True
    p = os.path.join(root, "logs", "content_log.txt")
    pat = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] AppID %s state changed : .*App Running" % re.escape(appid))
    end = time.time() + wait
    while time.time() < end:
        try:
            with open(p, "rb") as f:
                f.seek(max(0, os.path.getsize(p) - 65536))
                tail = f.read().decode("utf-8", "replace").splitlines()
            for line in reversed(tail):
                m = pat.match(line)
                if m and time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")) >= since - 3:
                    return True
        except OSError:
            pass
        time.sleep(1.5)
    return False

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

    # -- second factor (Face ID / PIN) ------------------------------------
    def _unlocked(self):
        """Past the second factor? The PC itself never needs it - being at the
        machine is stronger than anything a phone can prove - and with no
        second factor set up there is nothing to pass."""
        if self._is_local() or not SF.enabled():
            return True
        return SF.valid_unlock(self._cookie("ru"), self._cookie("rs"))

    def _rp(self):
        """(rpId, origin) for passkeys, or (None, None) without HTTPS - a
        passkey is only possible on a secure, named origin."""
        base = getattr(self.server, "https_base", None)
        if not base:
            return None, None
        return urlparse(base).hostname, base

    def _stepup(self, scope, b):
        """For a protected command. Returns None when it may run; otherwise
        sends the reply that makes the app ask for Face ID / PIN and returns
        True. The app then retries the SAME request with the one-time token,
        so every path to a protected command goes through here."""
        if self._is_local():
            return None
        if not SF.enabled():
            self._send(403, {"error": "stepup", "scope": scope, "setup": True})
            return True
        if SF.use_stepup(b.get("stepup"), scope, self._cookie("rs")):
            log("step-up ok: %s (from %s)" % (scope, self._client_ip()))
            return None
        self._send(403, {"error": "stepup", "scope": scope, "setup": False})
        return True

    def _unlock_cookie(self, clear=False):
        if clear:
            return "ru=; Path=/; Max-Age=0; SameSite=Strict; HttpOnly"
        return ("ru=%s; Path=/; Max-Age=%d; SameSite=Strict; HttpOnly%s"
                % (SF.make_unlock(self._cookie("rs")), secondfactor.UNLOCK_TTL,
                   "; Secure" if self._secure() else ""))

    def _client_ip(self):
        return self.client_address[0] if self.client_address else "?"

    def _is_local(self):
        """Is this request from the PC itself, rather than over the network?"""
        if _AS_PHONE:
            return False
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

    MAX_BODY = 1 << 20      # every request body is small JSON

    def _body(self):
        """The request's JSON body, read from the socket exactly once.

        It must be read even when the request is refused: the connection is
        kept alive, and unread body bytes would be parsed as the start of the
        NEXT request (which then fails with a baffling 501)."""
        if hasattr(self, "_parsed_body"):
            return self._parsed_body
        self._parsed_body = {}
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > self.MAX_BODY:
            self.close_connection = True     # can't skip it safely - hang up
            return self._parsed_body
        if n:
            raw = self.rfile.read(n)
            try:
                self._parsed_body = json.loads(raw.decode("utf-8"))
            except Exception:
                pass
        return self._parsed_body

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
        if path == "/sw.js":
            # The service worker that shows notifications. Plain static code;
            # scoped to the whole app so it can open the right page on a tap.
            with open(paths.asset("sw.js"), "rb") as f:
                return self._send(200, f.read(), "application/javascript; charset=utf-8",
                                  {"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

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
            # A page opened in a browser (the phone app, or the PC app from
            # the tray) goes to the sign-in page and comes back after; only
            # the app's own data calls get the bare 401.
            if path in ("/", "/pc") or (not path.startswith("/api/")
                                        and "text/html" in (self.headers.get("Accept") or "")):
                return self._send(302, b"", "text/html",
                                  {"Location": "/login" + ("" if path == "/" else "?next=" + quote(path))})
            return self._send(401, {"error": "not logged in"})

        if path == "/api/sf/status":
            rp_id, _ = self._rp()
            st = SF.status()
            st.update({"unlocked": self._unlocked(), "local": self._is_local(),
                       "passkeys_possible": bool(rp_id)})
            return self._send(200, st)

        # Past the session, the second factor: the app's pages load (so it can
        # show its own lock screen), but its data and controls do not.
        if path.startswith("/api/") and not self._unlocked():
            return self._send(403, {"error": "locked"})

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

            if path == "/api/platform":
                # The phone app is shared with the homelab add-on; this is how
                # it knows it's talking to a PC (the default for older PCs too).
                return self._send(200, {"kind": "pc", "name": auth.ACCOUNT,
                                        "version": version.VERSION})

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

            if path == "/api/jellyfin":
                return self._send(200, jellyfin.status())

            if path == "/api/mixer":
                out = []
                for a in mixer.apps():
                    out.append({"app": a["app"], "name": a["name"], "volume": a["volume"],
                                "muted": a["muted"], "icon": _icon_key(a["path"])})
                return self._send(200, {"apps": out})

            if path == "/api/appicon":
                ip = _icon_path(qs.get("k", [""])[0])
                if not ip:
                    return self._send(404, {"error": "no icon"})
                with open(ip, "rb") as f:
                    data = f.read()
                return self._send(200, data, "image/png", {"Cache-Control": "private, max-age=86400"})

            if path == "/api/windows":
                wins = []
                for w in sysctl.running_windows():
                    exe = sysctl.window_exe(w["hwnd"]) if w.get("hwnd") else ""
                    wins.append(dict(w, icon=_icon_key(exe), name=_app_name(exe, w["process"])))
                fg = sysctl.foreground_app()
                return self._send(200, {"windows": wins, "foreground": fg.get("pid")})

            if path == "/api/audio/devices":
                return self._send(200, sysctl.devices())

            if path == "/api/timer":
                return self._send(200, timer.info())

            if path == "/api/push":
                ep = qs.get("ep", [""])[0]
                return self._send(200, {"publicKey": push.public_key(),
                                        "types": [{"id": t[0], "name": t[1], "desc": t[2], "default": t[3]}
                                                  for t in push.TYPES],
                                        "tiers": list(push.TIERS),
                                        "prefs": push.prefs_for(ep) if ep else None})

            if path == "/api/notifications":
                return self._send(200, {"items": push.history()})

            if path == "/api/chat":
                return self._send(200, dict(chat.public(), convs=chat.conversations()))

            if path == "/api/chat/conv":
                c = chat.conversation(qs.get("id", [""])[0])
                if not c:
                    return self._send(404, {"error": "no such conversation"})
                return self._send(200, c)

            if path == "/api/chat/memory":
                return self._send(200, memory.graph())

            if path == "/api/chat/shot":
                img = _SHOTS.get(qs.get("id", [""])[0])
                if not img:
                    return self._send(404, {"error": "that screenshot is gone"})
                return self._send(200, img, "image/jpeg")

            if path == "/api/chat/models":
                # The phone's model picker: just names, never keys or addresses.
                try:
                    return self._send(200, {"models": chat.phone_models(), "current": chat.config()["model"]})
                except (ValueError, RuntimeError, OSError) as e:
                    return self._send(502, {"error": str(e)})

            if path == "/api/chat/config":
                # The key and the setup live on the PC: only the PC app sees them.
                if not self._is_local():
                    return self._send(403, {"error": "set the chat up from the PC app"})
                return self._send(200, chat.public(local=True))

            if path == "/api/screenshot":
                return self._screenshot()

            if path == "/api/gamestats":
                return self._send(200, {"game": media.current_game(),
                                        "stats": sysctl.system_stats()})

            if path == "/api/np/art":
                # Artwork of what's playing - re-encoded by us, served by hash.
                got = media.art(qs.get("v", [""])[0])
                if not got:
                    return self._send(404, {"error": "no art"})
                return self._send(200, got[0], got[1],
                                  {"Cache-Control": "private, max-age=86400"})

            if path == "/api/np/icon":
                ip = media.icon(qs.get("v", [""])[0])
                if not ip or not os.path.isfile(ip):
                    return self._send(404, {"error": "no icon"})
                with open(ip, "rb") as f:
                    data = f.read()
                return self._send(200, data, "image/png",
                                  {"Cache-Control": "private, max-age=86400"})

            if path == "/api/browse":
                return self._send(200, self._browse(qs.get("p", [""])[0]))

            if path == "/api/files":
                q = (qs.get("q", [""])[0] or "").strip()
                p = qs.get("p", [""])[0]
                if q:
                    return self._send(200, files.search(p, q[:100]))
                return self._send(200, files.listing(p))

            if path == "/api/download":
                return self._download(qs.get("p", [""])[0])

            if path == "/api/files/thumb":
                try:
                    size = int(qs.get("s", ["240"])[0])
                except ValueError:
                    size = 240
                data = files.thumbnail(os.path.abspath(qs.get("p", [""])[0]), size)
                if not data:
                    return self._send(404, {"error": "no thumbnail"})
                return self._send(200, data, "image/jpeg",
                                  {"Cache-Control": "private, max-age=604800"})

            if path == "/api/files/view":
                data = files.view_image(os.path.abspath(qs.get("p", [""])[0]))
                if not data:
                    return self._send(404, {"error": "can't show that picture"})
                return self._send(200, data, "image/jpeg",
                                  {"Cache-Control": "private, max-age=3600"})

            if path == "/api/files/text":
                fp = os.path.abspath(qs.get("p", [""])[0])
                t = files.text_preview(fp)
                if t is None:
                    return self._send(404, {"error": "no such file"})
                return self._send(200, t)

            if path == "/api/files/raw":
                fp = os.path.abspath(qs.get("p", [""])[0])
                ctype = files.INLINE_TYPES.get(os.path.splitext(fp)[1].lower())
                if not ctype:
                    return self._send(415, {"error": "not something the phone can play"})
                return self._serve_file(fp, ctype, inline=True)

            if path == "/api/files/zip":
                items = files.take_zip(qs.get("t", [""])[0])
                if not items:
                    return self._send(410, {"error": "that download link has expired"})
                return self._zip(items, qs.get("n", ["files.zip"])[0])

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
        # One handler object serves every request on a kept-alive connection,
        # so forget the previous request's body, then drain this one now - a
        # refusal below must not leave bytes behind to desync the next request.
        self.__dict__.pop("_parsed_body", None)
        if path == "/api/files/upload":
            return self._upload(qs)     # a raw file, not JSON - see _upload
        self._body()

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
                _LOGIN_FAILS.pop(self._client_ip(), None)
                push.notify_async("security", "New sign-in",
                                  "A phone signed in to %s from %s." % (auth.ACCOUNT, self._client_ip()),
                                  tag="signin")
                return self._send(200, {"ok": True},
                                  extra={"Set-Cookie": self._session_cookie()})
            log("login failed from %s: %s" % (self._client_ip(), msg))
            n = _LOGIN_FAILS[self._client_ip()] = _LOGIN_FAILS.get(self._client_ip(), 0) + 1
            if n == 3:
                push.notify_async("security", "Wrong codes",
                                  "Someone at %s has typed 3 wrong sign-in codes." % self._client_ip(),
                                  tag="badcodes")
            return self._send(401, {"error": msg})

        if not self._authed():
            return self._send(401, {"error": "not logged in"})

        if path.startswith("/api/sf/"):
            try:
                return self._sf_post(path, self._body())
            except Exception as e:
                log("sf %s failed: %s" % (path, e))
                return self._send(400, {"error": str(e)})

        if path != "/api/logout" and not self._unlocked():
            return self._send(403, {"error": "locked"})

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

            if path == "/api/files/zip":
                items = b.get("paths") or []
                if not isinstance(items, list):
                    return self._send(400, {"error": "bad selection"})
                try:
                    tok, name = files.prepare_zip(items)
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                return self._send(200, {"url": "/api/files/zip?t=%s&n=%s"
                                        % (tok, quote(name)), "name": name})

            if path == "/api/files/upload/begin":
                folder = os.path.abspath(str(b.get("dir", "")))
                if not files.writable_dir(folder):
                    return self._send(400, {"error": "Uploads can only go into your own "
                                            "folders (not AppData) or a non-system drive."})
                if self._stepup("upload", b):
                    return
                tok = files.upload_ticket(folder, self._cookie("rs"))
                return self._send(200, {"ticket": tok, "dir": folder})

            if path == "/api/clipboard/copy":
                # The desktop view's Copy: Ctrl+C on the PC, wait for the app
                # to actually put something on the clipboard, hand it back
                # for the phone's own clipboard. Text only, never logged.
                before = sysctl.clipboard_seq()
                stream.press("c", ["ctrl"])
                t0 = time.time()
                while time.time() - t0 < 1.5 and sysctl.clipboard_seq() == before:
                    time.sleep(0.05)
                changed = sysctl.clipboard_seq() != before
                if changed:
                    time.sleep(0.05)       # some apps set several formats
                txt = sysctl.get_clipboard_text()
                if txt is None:
                    return self._send(409, {"error": "Couldn't read the PC clipboard - is the PC locked?"})
                return self._send(200, {"text": txt[:100000], "changed": changed,
                                        "truncated": len(txt) > 100000})

            if path == "/api/clipboard/paste":
                # The desktop view's Paste: the phone's clipboard becomes the
                # PC's, then Ctrl+V. Text only, capped, never logged.
                text = str(b.get("text", ""))[:100000]
                if not text:
                    return self._send(400, {"error": "nothing to paste"})
                # Phones end lines with \n; Windows apps expect \r\n.
                text = re.sub(r"\r?\n", "\r\n", text)
                if not sysctl.set_clipboard_text(text):
                    return self._send(409, {"error": "Couldn't reach the PC clipboard - is the PC locked?"})
                time.sleep(0.05)
                stream.press("v", ["ctrl"])
                return self._send(200, {"ok": True, "chars": len(text)})

            if path == "/api/clipboard":
                # Text only, capped, not logged. Whatever the phone sends
                # replaces the PC clipboard.
                text = str(b.get("text", ""))[:100000]
                okset = sysctl.set_clipboard_text(text)
                return self._send(200 if okset else 500,
                                  {"ok": okset, "chars": len(text)})

            if path == "/api/mixer":
                app = str(b.get("app", ""))[:120]
                vol = b.get("volume")
                try:
                    vol = None if vol is None else max(0, min(100, int(vol)))
                except (TypeError, ValueError):
                    return self._send(400, {"error": "bad volume"})
                mute = b.get("mute")
                r = mixer.set_app(app, vol, None if mute is None else bool(mute))
                if not r["sessions"]:
                    return self._send(404, {"error": "That app isn't playing sound right now"})
                return self._send(200, r)

            if path == "/api/window/focus":
                try:
                    r = sysctl.focus_window(int(b.get("hwnd", 0)))
                except (TypeError, ValueError):
                    return self._send(400, {"error": "bad window"})
                return self._send(200 if r["ok"] else 409, r)

            if path == "/api/audio/default":
                did = str(b.get("id", ""))
                if did not in {d["id"] for d in sysctl.devices()["devices"]}:
                    return self._send(400, {"error": "no such output"})
                sysctl.set_default_device(did)
                log("audio output switched")
                return self._send(200, sysctl.devices())

            if path == "/api/timer":
                action = str(b.get("action", "pause"))
                # Putting the PC to sleep or off later is as serious as now.
                if action in timer.POWER and self._stepup("timer." + action, b):
                    return
                try:
                    r = timer.start(b.get("minutes", 30), action)
                except (TypeError, ValueError) as e:
                    return self._send(400, {"error": str(e)})
                log("sleep timer: %s in %s min" % (action, b.get("minutes")))
                return self._send(200, r)

            if path == "/api/timer/cancel":
                log("sleep timer cancelled")
                return self._send(200, timer.cancel())

            if path == "/api/push/subscribe":
                try:
                    prefs = push.subscribe(b.get("sub"), b.get("prefs"), b.get("device", ""))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                log("notifications: a phone subscribed")
                return self._send(200, {"ok": True, "prefs": prefs})

            if path == "/api/push/prefs":
                try:
                    return self._send(200, {"ok": True, "prefs": push.set_prefs(
                        str(b.get("endpoint", "")), b.get("prefs"))})
                except KeyError as e:
                    return self._send(404, {"error": str(e)})

            if path == "/api/push/unsubscribe":
                push.unsubscribe(str(b.get("endpoint", "")))
                return self._send(200, {"ok": True})

            if path == "/api/push/test":
                tier = b.get("tier") if b.get("tier") in push.TIERS[1:] else "normal"
                n = push.notify("test", "Aether Remote", "Notifications are working.",
                                force_tier=tier, only=str(b.get("endpoint", "")) or None)
                return self._send(200 if n else 502, {"sent": n} if n else
                                  {"error": "Your phone's push service didn't accept it - try turning notifications off and on."})

            if path == "/api/chat/send":
                return self._chat_send(b)

            if path == "/api/chat/delete":
                chat.delete(str(b.get("id", "")))
                return self._send(200, {"ok": True, "convs": chat.conversations()})

            if path == "/api/chat/clear":
                chat.clear()
                return self._send(200, {"ok": True, "convs": []})

            if path == "/api/chat/confirm":
                # Approving needs Face ID / PIN for exactly this request
                # (the phone's api() asks for it when we answer "stepup").
                c = _CONFIRMS.get(str(b.get("id", "")))
                if not c:
                    return self._send(404, {"error": "That request has expired"})
                if b.get("approve"):
                    if self._stepup(c["scope"], b):
                        return
                    c["ok"] = True
                    log("chat action approved: %s" % c["what"])
                c["ev"].set()
                return self._send(200, {"ok": True, "approved": c["ok"]})

            if path == "/api/chat/memory/forget":
                gone = memory.forget(fid=str(b.get("id", ""))[:20])
                return self._send(200 if gone else 404, dict(memory.graph(), forgot=gone))

            if path == "/api/chat/memory/clear":
                memory.clear()
                log("chat memory cleared")
                return self._send(200, memory.graph())

            if path == "/api/chat/model":
                try:
                    r = chat.pick_model(str(b.get("model", "")))
                except (ValueError, RuntimeError, OSError) as e:
                    return self._send(400, {"error": str(e)})
                log("chat model switched to %s" % r["model"])
                return self._send(200, r)

            if path in ("/api/chat/config", "/api/chat/models", "/api/chat/test"):
                if not self._is_local():
                    return self._send(403, {"error": "set the chat up from the PC app"})
                try:
                    if path == "/api/chat/config":
                        r = chat.update(b)
                        log("chat settings saved (%s)" % r["provider"])
                        return self._send(200, r)
                    if path == "/api/chat/models":
                        return self._send(200, {"models": chat.list_models(b)})
                    return self._send(200, chat.test())
                except (ValueError, RuntimeError) as e:
                    return self._send(400, {"error": str(e)})

            if path == "/api/jellyfin/connect":
                # Quick Connect: the PC gets its own Jellyfin sign-in once
                # you approve the code in Jellyfin. No password, no key.
                try:
                    code = jellyfin.start_quick_connect(str(b.get("server", "")))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                except Exception as e:
                    return self._send(502, {"error": "Jellyfin didn't answer: %s" % e})
                log("jellyfin: quick connect started")
                return self._send(200, {"code": code})

            if path == "/api/jellyfin/disconnect":
                jellyfin.disconnect()
                log("jellyfin: disconnected")
                return self._send(200, jellyfin.status())

            if path == "/api/media":
                # Now playing's own buttons: they drive the session shown on
                # the tile, not whatever the media keys happen to reach.
                pos = b.get("pos")
                try:
                    pos = float(pos) if pos is not None else None
                except (TypeError, ValueError):
                    return self._send(400, {"error": "bad position"})
                if b.get("op") == "seek" and pos is None:
                    return self._send(400, {"error": "no position"})
                ok, err = media.command(str(b.get("op", "")), pos)
                if not ok:
                    return self._send(400, {"ok": False, "error": err})
                return self._send(200, {"ok": True,
                                        "nowplaying": media.refresh_now(1.5)})

            if path == "/api/endtask":
                r = sysctl.end_task(b.get("pid"))
                log("end task pid=%s -> %s" % (b.get("pid"), r.get("ok")))
                return self._send(200 if r.get("ok") else 400, r)

            if path == "/api/wol/send":
                # This PC sends a magic packet on its own LAN (no Pi involved).
                try:
                    wol.send_wol(str(b.get("mac", "")))
                    return self._send(200, {"ok": True})
                except Exception as e:
                    return self._send(400, {"ok": False, "error": str(e)})

            if path == "/api/wol/wake":
                # Wake another PC. The phone can't call the Pi itself any
                # more: this page is https, and browsers block a plain-http
                # request from it. So this PC makes the call for it - and also
                # sends the packet on its own LAN, which covers a PC next to it.
                mac = str(b.get("mac", ""))
                pi = str(b.get("pi", "")).strip()
                try:
                    wol.magic_packet(mac)
                except ValueError as e:
                    return self._send(400, {"ok": False, "error": str(e)})
                pi_err = None
                if pi:
                    try:
                        wol.relay_via(pi, mac)
                    except Exception as e:
                        pi_err = str(e) or "no answer"
                try:
                    wol.send_wol(mac)
                    here = True
                except OSError:
                    here = False
                log("wake %s via %s -> %s" % (mac, pi or "this PC",
                                              "ok" if not pi_err else pi_err))
                if pi and pi_err:
                    return self._send(502, {"ok": False, "error": pi_err,
                                            "sent_here": here})
                return self._send(200, {"ok": True, "via": "pi" if pi else "here"})

            if path == "/api/pcs/probe":
                # "Add a PC" check for an address the https page may not call
                # itself (plain http - the homelab add-on, or an older PC).
                try:
                    j = wol.probe(str(b.get("url", ""))[:300])
                except Exception as e:
                    return self._send(502, {"ok": False, "error": str(e)})
                j = j if isinstance(j, dict) else {}
                app = str(j.get("app", ""))[:40]
                if app not in ("remote", "aether-homelab"):
                    return self._send(502, {"ok": False, "error": "not an Aether app"})
                return self._send(200, {"ok": True, "app": app,
                                        "pc": str(j.get("pc", ""))[:60]})

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
                # Signing everyone out or replacing the authenticator secret
                # from a phone needs Face ID / PIN; at the PC it's free.
                if act in ("revoke", "reset") and self._stepup("security", b):
                    return
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

    # -- second factor routes ---------------------------------------------
    def _sf_post(self, path, b):
        rs = self._cookie("rs")
        rp_id, origin = self._rp()
        purpose = str(b.get("purpose", ""))
        scope = str(b.get("scope", ""))[:80]

        def settings_ok():
            """Changing or turning off the second factor needs the CURRENT one
            (or being at the PC). First-time setup needs only the session."""
            return (not SF.enabled() or self._is_local()
                    or SF.use_stepup(b.get("stepup"), "settings", rs))

        if path == "/api/sf/options":
            if not rp_id:
                return self._send(400, {"error": "Face ID needs the secure "
                                        "(https) address of this PC."})
            if purpose == "register":
                ch = SF.challenge("register")
                import base64 as _b
                u = _b.urlsafe_b64encode(("aether-" + auth.ACCOUNT).encode()).decode().rstrip("=")
                return self._send(200, {"publicKey": {
                    "challenge": secondfactor._b64(ch),
                    "rp": {"id": rp_id, "name": "Aether Remote"},
                    "user": {"id": u, "name": auth.ACCOUNT,
                             "displayName": "Aether Remote - " + auth.ACCOUNT},
                    "pubKeyCredParams": [{"type": "public-key", "alg": -7},
                                         {"type": "public-key", "alg": -257}],
                    "authenticatorSelection": {"userVerification": "required",
                                               "residentKey": "preferred"},
                    "excludeCredentials": [{"type": "public-key", "id": i}
                                           for i in SF.allow_ids()],
                    "attestation": "none", "timeout": 60000}})
            if purpose in ("unlock", "stepup"):
                ch = SF.challenge(purpose, scope if purpose == "stepup" else "")
                return self._send(200, {"publicKey": {
                    "challenge": secondfactor._b64(ch), "rpId": rp_id,
                    "allowCredentials": [{"type": "public-key", "id": i}
                                         for i in SF.allow_ids()],
                    "userVerification": "required", "timeout": 60000}})
            return self._send(400, {"error": "unknown purpose"})

        if path == "/api/sf/verify":
            if purpose not in ("unlock", "stepup"):
                return self._send(400, {"error": "unknown purpose"})
            if not SF.enabled():
                return self._send(400, {"error": "Face ID / PIN is not set up"})
            if SF.st.get("method") == "pin":
                good, msg = SF.check_pin(b.get("pin"))
            else:
                if not rp_id:
                    return self._send(400, {"error": "Face ID needs the secure address"})
                good, msg = SF.check_passkey(b.get("credential") or {}, rp_id, origin,
                                             purpose, scope if purpose == "stepup" else "")
            if not good:
                log("second factor failed from %s: %s" % (self._client_ip(), msg))
                if SF.st.get("fails", 0) in (3, 5):
                    push.notify_async("security", "Wrong PIN",
                                      "Someone keeps getting the PIN wrong on %s." % auth.ACCOUNT, tag="badpin")
                # 400, not 401: the app reads 401 as "signed out" and would
                # bounce to the login page over a mistyped PIN.
                return self._send(400, {"error": msg, "sf_failed": True,
                                        **SF.status()})
            out = {"ok": True}
            if purpose == "stepup":
                out["stepup"] = SF.make_stepup(scope, rs)
            return self._send(200, out, extra={"Set-Cookie": self._unlock_cookie()})

        if path == "/api/sf/pin":
            if not settings_ok():
                return self._send(403, {"error": "stepup", "scope": "settings",
                                        "setup": False})
            SF.set_pin(str(b.get("pin", "")))
            log("PIN set (from %s)" % self._client_ip())
            return self._send(200, {"ok": True, **SF.status()},
                              extra={"Set-Cookie": self._unlock_cookie()})

        if path == "/api/sf/register":
            if not rp_id:
                return self._send(400, {"error": "Face ID needs the secure address"})
            if not settings_ok():
                return self._send(403, {"error": "stepup", "scope": "settings",
                                        "setup": False})
            SF.register_passkey(b.get("credential") or {}, rp_id, origin,
                                str(b.get("label", "")))
            return self._send(200, {"ok": True, **SF.status()},
                              extra={"Set-Cookie": self._unlock_cookie()})

        if path == "/api/sf/lock":
            return self._send(200, {"ok": True},
                              extra={"Set-Cookie": self._unlock_cookie(clear=True)})

        if path == "/api/sf/disable":
            if not settings_ok():
                return self._send(403, {"error": "stepup", "scope": "settings",
                                        "setup": False})
            SF.reset()
            return self._send(200, {"ok": True, **SF.status()},
                              extra={"Set-Cookie": self._unlock_cookie(clear=True)})

        if path == "/api/sf/reset":
            # The recovery path for a lost phone or a forgotten PIN: only from
            # the PC itself.
            if not self._is_local():
                return self._send(403, {"error": "reset it from the PC itself"})
            SF.reset()
            return self._send(200, {"ok": True, **SF.status()})

        return self._send(404, {"error": "no such path"})

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
        # ...and then Face ID / PIN, checked here on the server, every time.
        if action in self.DESTRUCTIVE and self._stepup("power." + action, b):
            return

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
            if self._protected_steps(actions) and \
                    self._stepup("tile:" + str(b.get("id", "")), b):
                return
            def _go():
                if actions:
                    self._run_steps(actions, "launch %s" % ref)
                if ref.startswith("steam-"):
                    _steam_unstick()      # a Steam hung after updating ignores launches
                sysctl.run_detached(launch_cmd)
            if actions or ref.startswith("steam-"):
                threading.Thread(target=_go, daemon=True).start()
            else:
                _go()
            log("launch %s%s" % (ref, " (%d pre-actions)" % len(actions) if actions else ""))
            return self._send(200, {"ok": True})

        if kind == "action":
            spec = layout.ACTIONS.get(ref)
            if not spec:
                return self._send(400, {"error": "unknown action"})
            if spec["kind"] == "key":
                key = ref.split(".", 1)[1]
                # Media buttons go to the session Now Playing shows, when
                # there is one; the media keys are the fallback.
                op = {"playpause": "toggle", "next": "next",
                      "prev": "prev"}.get(key)
                if not (op and ref.startswith("media.") and
                        media.now_playing() and media.command(op)[0]):
                    sysctl.tap_key(key)
            elif spec["kind"] == "seek":
                ok, err = media.skip(spec["delta"])
                if not ok:
                    return self._send(409, {"error": err})
                return self._send(200, {"ok": True, "nowplaying": media.now_playing()})
            elif spec["kind"] == "screenoff":
                sysctl.screen_off()
            elif spec["kind"] == "hotkey":
                # Discord's own keybinds - Ctrl+Shift+M / Ctrl+Shift+D, set
                # once in Discord (Settings > Keybinds); the PC app says how.
                stream.press({"discord.mute": "m", "discord.deafen": "d"}[ref], ["ctrl", "shift"])
            elif spec["kind"] == "phone":
                pass                    # the phone does these itself (screenshot)
            elif spec["kind"] == "power":
                return self._power({"action": ref.split(".", 1)[1],
                                    "confirm": b.get("confirm"),
                                    "stepup": b.get("stepup")})
            return self._send(200, {"ok": True})

        if kind == "toggle":
            if ref == "mute":
                want = b.get("value")
                if want is None:
                    want = not sysctl.status()["muted"]
                sysctl.set_mute(bool(want))
            elif ref == "micmute":
                want = b.get("value")
                if want is None:
                    want = not mixer.mic()["muted"]
                r = mixer.set_mic(bool(want))
                if not r["mics"]:
                    return self._send(409, {"error": "No microphone found"})
                log("microphone %s" % ("muted" if want else "on"))
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
            return self._scene({"run": ref, "stepup": b.get("stepup")})

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
            # A scene that shuts down / restarts / signs out needs the same
            # Face ID / PIN as the tile itself - no side door.
            if self._protected_steps(scene.get("steps")) and \
                    self._stepup("scene:" + scene["id"], b):
                return
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

    @classmethod
    def _protected_steps(cls, steps):
        """Does this list of steps shut down, restart or sign out?"""
        return any(s.get("op") == "power" and str(s.get("value")) in cls.DESTRUCTIVE
                   for s in (steps or []))

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

    def _download(self, p):
        """One file, as an attachment. Nothing of this app's own (its login
        secret, keys) and nothing in the assistant's folder."""
        return self._serve_file(os.path.abspath(p), "application/octet-stream",
                                inline=False)

    def _serve_file(self, p, ctype, inline):
        """Stream a file, honouring Range (iPhones won't play video without
        it). Inline ones get a sandbox CSP - they're media, never pages."""
        if not os.path.isfile(p) or not files.readable(p):
            return self._send(404, {"error": "no such file"})
        try:
            size = os.path.getsize(p)
            f = open(p, "rb")
        except OSError as e:
            return self._send(403, {"error": str(e)})
        start, end, partial = 0, size - 1, False
        rng = self.headers.get("Range") or ""
        m = re.match(r"^bytes=(\d*)-(\d*)$", rng.strip())
        if m and size and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:                                   # the last N bytes
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                f.close()
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            partial = True
        name = os.path.basename(p)
        ascii_name = name.encode("ascii", "replace").decode("ascii").replace('"', "")
        disp = ("%s; filename=\"%s\"; filename*=UTF-8''%s"
                % ("inline" if inline else "attachment", ascii_name, quote(name)))
        length = end - start + 1 if size else 0
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Disposition", disp)
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
        self.end_headers()
        sent = 0
        try:
            with f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
                    sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        if not inline and not partial:
            log("download %s (%d bytes)" % (name, size))
        return None

    def _zip(self, items, name):
        """Several files (or folders) as one zip, streamed as it's built - no
        temp file, no waiting for a 10 GB zip before the first byte."""
        name = re.sub(r'[\\/:*?"<>|\r\n]', "_", name)[:120] or "files.zip"
        if not name.lower().endswith(".zip"):
            name += ".zip"
        ascii_name = name.encode("ascii", "replace").decode("ascii")
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition",
                         "attachment; filename=\"%s\"; filename*=UTF-8''%s"
                         % (ascii_name, quote(name)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # No length up front, so the end of the zip is the end of the connection.
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            n = files.stream_zip(items, self.wfile)
            log("download zip of %d item(s) (%d bytes)" % (len(items), n))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        return None

    def _upload(self, qs):
        """A raw file body from the phone. Handled before the usual JSON body
        read (which caps at 1 MB), and only with a ticket from
        /api/files/upload/begin - which is where Face ID / the PIN is asked."""
        try:
            n = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            n = -1

        def refuse(code, msg):
            self.close_connection = True     # the body stays unread: hang up
            return self._send(code, {"error": msg})

        if not self._authed():
            return refuse(401, "not logged in")
        if not self._unlocked():
            return refuse(403, "locked")
        folder = files.check_ticket(qs.get("t", [""])[0], self._cookie("rs"))
        if not folder:
            return refuse(403, "The upload wasn't approved (or took too long) - try again.")
        if n < 0:
            return refuse(411, "no length")
        try:
            used = files.receive(folder, qs.get("name", [""])[0], n, self.rfile)
        except (ValueError, PermissionError) as e:
            return refuse(400, str(e))
        except (ConnectionError, OSError) as e:
            return refuse(500, "upload failed: %s" % e)
        log("upload %s (%d bytes) into %s" % (used, n, folder))
        return self._send(200, {"ok": True, "name": used,
                                "path": os.path.join(folder, used)})

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
        for dirpath, dirs, fnames in os.walk(root):
            if dirpath.count(os.sep) - base_depth > 3:
                dirs[:] = []
                continue
            for fn in fnames:
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
        st["nowplaying"] = media.now_playing()
        st["timer"] = timer.info()
        st["game"] = media.current_game()
        try:
            st["mic"] = _mic_cached()
        except Exception:
            st["mic"] = None
        st["foreground"] = sysctl.foreground_app()
        st["devices"] = sysctl.devices()["devices"]
        st["launchers"] = [{"id": l["id"], "label": l["label"],
                            "group": l.get("group", "")}
                           for l in CFG.get("launchers", [])]
        st["monitors"] = stream.monitors()
        st["time"] = time.strftime("%H:%M")
        return st

    def _screenshot(self):
        """The whole screen at full size, as a PNG to save on the phone."""
        try:
            from PIL import ImageGrab
        except ImportError:
            return self._send(501, {"error": "Pillow not installed on the PC"})
        mon = 0
        try:
            img = ImageGrab.grab(all_screens=False)
        except Exception as e:
            return self._send(500, {"error": "Couldn't capture the screen: %s" % e})
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=False)
        name = time.strftime("Screenshot %Y-%m-%d %H.%M.%S.png")
        log("screenshot taken (monitor %d)" % mon)
        return self._send(200, buf.getvalue(), "image/png",
                          {"Content-Disposition": 'attachment; filename="%s"' % name})

    def _chat_send(self, b):
        """Stream the AI's answer as server-sent events. The tools it may use
        are the app's own code paths - see _chat_hooks."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        gone = {"v": False}

        def emit(ev):
            if gone["v"]:
                raise ConnectionAbortedError("the phone stopped listening")
            try:
                self.wfile.write(("data: %s\n\n" % json.dumps(ev)).encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                gone["v"] = True
                raise ConnectionAbortedError("the phone stopped listening")
        try:
            chat.send(b.get("conv"), b.get("text"), self._chat_hooks(), emit)
        except ConnectionAbortedError:
            pass
        except Exception as e:
            try:
                emit({"t": "error", "d": str(e)[:400]})
            except Exception:
                pass
        return None

    def _chat_hooks(self):
        def pc_status(a):
            st = sysctl.status()
            n = media.now_playing()
            g = media.current_game()
            return {"volume": st["volume"], "muted": st["muted"], "output": st.get("device"),
                    "stats": {k: "%s %s" % (v["big"], v["unit"]) for k, v in sysctl.system_stats(wait=True).items()
                              if not v.get("na")},
                    "now_playing": n and {k: n.get(k) for k in ("title", "artist", "appName", "playing", "pos", "dur")},
                    "game": g and {"name": g["title"], "minutes_played": int(g["pos"] // 60)},
                    "open_windows": [w["title"] for w in sysctl.running_windows()][:25]}

        def media_control(a):
            act = a.get("action")
            if act in ("forward", "back"):
                secs = int(a.get("seconds") or 10)
                ok, err = media.skip(secs if act == "forward" else -secs)
            else:
                ok, err = media.command({"play_pause": "toggle", "next": "next",
                                         "previous": "prev"}.get(act, "?"))
            return {"ok": ok} if ok else {"error": err}

        def set_volume(a):
            if a.get("level") is not None:
                sysctl.set_volume(max(0, min(100, int(a["level"]))))
            if a.get("mute") is not None:
                sysctl.set_mute(bool(a["mute"]))
            return {"ok": True, "volume": sysctl.status()["volume"]}

        def open_app(a):
            want = re.sub(r"[^a-z0-9]", "", str(a.get("name", "")).lower())
            items = library_items()
            best = None
            for it in items:
                nm = re.sub(r"[^a-z0-9]", "", it["name"].lower())
                if nm == want:
                    best = it
                    break
                if want and (want in nm or nm in want) and best is None:
                    best = it
            if not best:
                return {"error": "Nothing called that in the library",
                        "some_library_items": [i["name"] for i in items][:40]}
            steam = re.match(r"steam-(\d+)$", best["id"])
            restarted = _steam_unstick() if steam else False
            t0 = time.time()
            sysctl.run_detached(self._launch_cmd(known_launches()[best["id"]]))
            log("chat opened %s" % best["id"])
            if not steam:
                return {"ok": True, "opened": best["name"]}
            # Don't just say "opened": watch Steam actually start it.
            if _steam_started(steam.group(1), t0, 75 if restarted else 45):
                out = {"ok": True, "opened": best["name"], "running": True}
                if restarted:
                    out["note"] = "Steam was stuck, so it was restarted first"
                return out
            return {"ok": False, "opened": best["name"], "running": False,
                    "error": "Steam was asked to start it but it still isn't running - Steam may be "
                             "updating the game or showing a message on the PC's screen."}

        def _norm(x):
            return re.sub(r"[^a-z0-9]", "", str(x or "").lower())

        def _windows_like(name):
            want = _norm(name)
            if not want:
                return []
            return [w for w in sysctl.running_windows()
                    if want in _norm(w["title"]) or want in _norm(w["process"].rsplit(".", 1)[0])]

        def _confirm(emit, what):
            """Show "needs your OK" in the chat and wait for Face ID / PIN."""
            cid = secrets.token_hex(6)
            c = _CONFIRMS[cid] = {"ev": threading.Event(), "ok": False, "scope": "chat:" + cid, "what": what}
            emit({"t": "confirm", "id": cid, "what": what})
            c["ev"].wait(120)
            _CONFIRMS.pop(cid, None)
            emit({"t": "confirm_done", "id": cid, "ok": c["ok"]})
            return c["ok"]

        def lock_pc(a):
            sysctl.lock_workstation()
            log("chat locked the PC")
            return {"ok": True}

        def screenshot(a, emit):
            from PIL import ImageGrab
            img = ImageGrab.grab(all_screens=False).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=85)
            sid = secrets.token_hex(8)
            _SHOTS[sid] = buf.getvalue()
            for k in list(_SHOTS)[:-4]:
                _SHOTS.pop(k, None)
            emit({"t": "image", "url": "/api/chat/shot?id=" + sid})
            log("chat took a screenshot")
            return {"ok": True, "shown_to_user": True, "size": "%dx%d" % img.size}

        def switch_audio_output(a):
            devs = sysctl.devices()["devices"]
            want = _norm(a.get("name"))
            hit = [d for d in devs if want and want in _norm(d["name"])]
            if len(hit) != 1:
                return {"error": "Say which one" if hit else "No output called that",
                        "outputs": [d["name"] for d in devs]}
            sysctl.set_default_device(hit[0]["id"])
            log("chat switched audio output")
            return {"ok": True, "now": hit[0]["name"]}

        def set_app_volume(a):
            want = _norm(a.get("app"))
            apps = mixer.apps()
            hit = [m for m in apps if want and (want in _norm(m["name"]) or want in _norm(m["app"]))]
            if not hit:
                return {"error": "That app isn't playing sound right now", "apps_with_sound": [m["name"] for m in apps]}
            lvl = max(0, min(100, int(a.get("level", 50))))
            mixer.set_app(hit[0]["app"], pct=lvl)
            return {"ok": True, "app": hit[0]["name"], "volume": lvl}

        def focus_window(a):
            wins = _windows_like(a.get("name"))
            if not wins:
                return {"error": "No open window like that", "open": [w["title"] for w in sysctl.running_windows()][:25]}
            return dict(sysctl.focus_window(wins[0]["hwnd"]), window=wins[0]["title"])

        def close_app(a, confirm):
            wins = _windows_like(a.get("name"))
            if not wins:
                return {"error": "No open app like that", "open": [w["title"] for w in sysctl.running_windows()][:25]}
            if len(wins) > 3:
                return {"error": "That matches several apps - which one?", "matches": [w["title"] for w in wins]}
            names = ", ".join(w["title"][:50] for w in wins)
            if not confirm("Close " + names):
                return {"error": "The user didn't approve it, so nothing was closed."}
            for w in wins:
                sysctl.close_window(w["hwnd"])
            time.sleep(2.5)
            left = [w["title"] for w in wins if sysctl.window_alive(w["hwnd"])]
            log("chat closed %s (approved on the phone)" % names)
            out = {"ok": True, "closed": [w["title"] for w in wins if w["title"] not in left]}
            if left:
                out["still_open"] = left
                out["note"] = "Still open - it may be asking to save something on the PC."
            return out

        def power(a, confirm):
            act = {"sign_out": "signout"}.get(a.get("action"), a.get("action"))
            what = {"sleep": "Put the PC to sleep", "restart": "Restart the PC", "shutdown": "Shut down the PC",
                    "signout": "Sign out of Windows"}.get(act)
            if not what:
                return {"error": "Unknown power action"}
            if not confirm(what):
                return {"error": "The user didn't approve it, so nothing happened."}
            cmd = self.POWER[act]
            # A few seconds' grace so the chat can still say so.
            threading.Timer(6, lambda: sysctl.run_detached(cmd)).start()
            log("chat: %s in 6s (approved on the phone)" % act)
            return {"ok": True, "doing": what, "in_seconds": 6}

        def list_library(a):
            q = re.sub(r"[^a-z0-9 ]", "", str(a.get("search") or "").lower()).split()
            items = [{"name": i["name"], "from": i["id"].split("-")[0]} for i in library_items()
                     if all(w in i["name"].lower() for w in q)]
            return {"count": len(items), "items": items[:150]}

        def search_files(a):
            folder = a.get("folder") or os.path.expanduser("~")
            r = files.search(str(folder), str(a.get("query", ""))[:100], limit=40, budget=4)
            return {"results": [{"name": e["name"], "path": e["path"], "folder": e["dir"],
                                 "size": e["size"]} for e in r["entries"]]}

        def read_text_file(a):
            p = os.path.abspath(str(a.get("path", "")))
            t = files.text_preview(p)
            if t is None:
                return {"error": "Can't read that file"}
            if t.get("binary"):
                return {"error": "That isn't a text file"}
            return {"text": t["text"][:40000], "truncated": t["truncated"] or len(t["text"]) > 40000}

        return {"pc_status": pc_status, "media_control": media_control, "set_volume": set_volume,
                "open_app": open_app, "search_files": search_files, "read_text_file": read_text_file,
                "pc_specs": lambda a: sysctl.hardware_specs(), "running_programs": lambda a: sysctl.top_processes(),
                "list_library": list_library, "_confirm": _confirm, "lock_pc": lock_pc,
                "screenshot": screenshot, "switch_audio_output": switch_audio_output,
                "set_app_volume": set_app_volume, "focus_window": focus_window,
                "close_app": close_app, "power": power}

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


def _claim_port(port):
    """A named mutex per port; the OS drops it when this process ends, however
    it ends. True = we're the only server on this port."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        h = k32.CreateMutexW(None, True, "AetherRemoteServer-%d" % port)
        if ctypes.get_last_error() == 183:     # ERROR_ALREADY_EXISTS
            return False
        _claim_port.handle = h                 # keep it for the process's life
        return True
    except Exception:
        return True                            # never let the guard block a start


_LOGIN_FAILS = {}
_ICONS = {}            # short key -> exe path (only ones we listed)
_MIC = {"at": 0.0, "v": None}


def _icon_key(exe):
    if not exe:
        return ""
    import hashlib
    k = hashlib.sha1(exe.lower().encode("utf-8", "ignore")).hexdigest()[:12]
    if len(_ICONS) > 400:
        _ICONS.clear()
    _ICONS[k] = exe
    return k


def _icon_path(k):
    exe = _ICONS.get(k)
    if not exe:
        return None
    p = media.icon_path(exe) if "\\windowsapps\\" in exe.lower() else layout.icon_for(exe)
    return p if p and os.path.isfile(p) and p.lower().endswith(".png") else None


def _app_name(exe, fallback):
    if exe:
        n = media._NAMES.get(os.path.basename(exe).lower()) or media._file_description(exe)
        if n:
            return n
    return os.path.splitext(fallback or "")[0] or "App"


def _mic_cached():
    if time.time() - _MIC["at"] > 4:
        _MIC["v"], _MIC["at"] = mixer.mic(), time.time()
    return _MIC["v"]


def _timer_fire(action):
    log("sleep timer: %s" % action)
    if action == "pause":
        n = media.now_playing()
        if n and n.get("playing"):
            if not media.command("toggle")[0]:
                sysctl.tap_key("playpause")
    elif action == "mute":
        sysctl.set_mute(True)
    elif action == "screenoff":
        sysctl.screen_off()
    elif action == "lock":
        sysctl.lock_workstation()
    elif action in ("sleep", "shutdown"):
        sysctl.run_detached(Handler.POWER[action])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=CFG.get("host", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=CFG.get("port", 8787))
    a = ap.parse_args()

    # One server per port. Windows lets a second process bind a port the
    # first already holds, then splits connections between the two at random -
    # a phone's Face ID challenge issued by one and checked by the other just
    # fails. Both the supervisor and the tray restart a dead server, and they
    # can race, so the guard lives here where it can't be skipped.
    if not _claim_port(a.port):
        log("another Aether Remote server already runs on port %d - exiting" % a.port)
        return 3

    # Now Playing also shows the game you're in: it needs the library, and
    # each game's artwork (yours if you dropped some in, else the scanned art).
    media.configure(items=library_items,
                    art_for=lambda it: Handler._custom_art_for(it["id"]) or it.get("art"))
    timer.configure(run=_timer_fire, notify=push.notify_async)
    watch.configure(game=media.current_game, stats=sysctl.system_stats,
                    update=lambda: update.state())
    sysctl.warm_specs()           # the chat's "what's in my PC" answer, ready before it's asked

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
