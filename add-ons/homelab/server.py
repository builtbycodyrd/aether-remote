"""The Aether homelab add-on HTTP server.

Same shape as Aether Remote's server - TOTP login, HMAC-signed 30-day
session cookie, a private LAN/Tailscale tool - but the tiles drive a Proxmox
node and the local systemd/Docker instead of a Windows desktop.

    python server.py --host 0.0.0.0 --port 8788
"""
import argparse
import json
import os
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import paths
import auth
import homelab
import secondfactor
import updater
import version

CFG = homelab.load_config()

# The PIN on top of the 30-day login: asked when the app opens, and for every
# VM stop / shut down / reboot. Signed with the session key, so signing
# everyone out also voids every unlock. (Face ID needs an https address; this
# add-on is served over plain http on the home network, so it's PIN-only.)
SF = secondfactor.SecondFactor(paths.data("sf.json"), lambda: auth.STATE["server_key"],
                               log=lambda m: print("[2fa]", m))
PROTECTED = ("stop", "shutdown", "reboot")

WEB = paths.asset("web")
STATIC = {
    "/": ("home.html", "text/html; charset=utf-8"),
    "/home.html": ("home.html", "text/html; charset=utf-8"),
    "/home.js": ("home.js", "application/javascript; charset=utf-8"),
}


def _read(name):
    with open(os.path.join(WEB, name), "rb") as f:
        return f.read()


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
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _cookie(self):
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == "rs":
                    return v
        return ""

    def _authed(self):
        return auth.valid_session(self._cookie())

    def _cookie_named(self, name):
        for part in self.headers.get("Cookie", "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == name:
                    return v
        return ""

    def _unlocked(self):
        return (not SF.enabled()) or SF.valid_unlock(self._cookie_named("ru"),
                                                     self._cookie())

    def _unlock_cookie(self, clear=False):
        if clear:
            return "ru=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"
        return ("ru=%s; Max-Age=%d; Path=/; HttpOnly; SameSite=Strict"
                % (SF.make_unlock(self._cookie()), secondfactor.UNLOCK_TTL))

    def _stepup(self, scope, b):
        """None = go ahead. Otherwise the refusal has been sent - return it.
        With no PIN set up yet the answer asks the phone to set one first:
        a protected command never runs unprotected."""
        if SF.enabled() and SF.use_stepup(b.get("stepup"), scope, self._cookie()):
            return None
        return self._send(403, {"error": "stepup", "scope": scope,
                                "setup": not SF.enabled()}) or True

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _client_ip(self):
        return self.client_address[0] if self.client_address else "?"

    # -- GET --------------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/ping":
                return self._send(200, {"ok": True, "app": "aether-homelab"})

            if path in STATIC:
                # The app shell loads for anyone; its own fetches are gated,
                # so an unauthed phone gets the login screen from home.js.
                name, ctype = STATIC[path]
                return self._send(200, _read(name), ctype)

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

            # ---- gated ----
            if path.startswith("/api/"):
                if not self._authed():
                    return self._send(401, {"error": "sign in"})

                if path == "/api/sf/status":
                    return self._send(200, {**SF.status(), "unlocked": self._unlocked(),
                                            "passkeys_possible": False})
                if not self._unlocked():
                    return self._send(403, {"error": "locked"})

                if path == "/api/state":
                    snap = homelab.snapshot(CFG)
                    return self._send(200, {"stats": snap["stats"],
                                            "sections": homelab.sections(snap),
                                            "version": version.VERSION})

                if path == "/api/update":
                    return self._send(200, updater.state())

                if path == "/api/update/notes":
                    from urllib.parse import parse_qs
                    tag = (parse_qs(urlparse(self.path).query).get("tag") or [""])[0]
                    try:
                        return self._send(200, updater.notes(tag),
                                          "text/plain; charset=utf-8")
                    except Exception as e:
                        return self._send(404, {"error": str(e)})

            return self._send(404, {"error": "no such path"})
        except Exception as e:
            return self._send(500, {"error": str(e),
                                    "where": traceback.format_exc()[-400:]})

    # -- POST -------------------------------------------------------------
    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/api/login":
                b = self._body()
                ok, msg = auth.verify_code(str(b.get("code", "")),
                                           self._client_ip())
                if not ok:
                    return self._send(401, {"ok": False, "error": msg})
                cookie, age = auth.new_session()
                body = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header(
                    "Set-Cookie",
                    "rs=%s; Max-Age=%d; Path=/; HttpOnly; SameSite=Lax"
                    % (cookie, age))
                self.end_headers()
                self.wfile.write(body)
                return

            # ---- gated ----
            if not self._authed():
                return self._send(401, {"error": "sign in"})
            b = self._body()

            if path.startswith("/api/sf/"):
                return self._sf_post(path, b)
            if not self._unlocked():
                return self._send(403, {"error": "locked"})

            if path == "/api/guest":
                # The phone sends the tile ref "qemu:101" and the action.
                ref = str(b.get("ref", ""))
                action = str(b.get("action", ""))
                if ":" not in ref:
                    return self._send(400, {"error": "bad guest ref"})
                kind, vmid = ref.split(":", 1)
                if not homelab.allowed_guest(CFG, vmid):
                    return self._send(403, {"error": "guest not in the allowed set"})
                if action in PROTECTED and self._stepup("guest." + action, b):
                    return
                px = homelab.Proxmox(CFG)
                try:
                    px.guest_action(kind, vmid, action)
                    return self._send(200, {"ok": True})
                except Exception as e:
                    return self._send(400, {"ok": False, "error": str(e)})

            if path == "/api/service":
                unit = str(b.get("ref", ""))
                if not unit:
                    return self._send(400, {"error": "no service"})
                if not homelab.allowed_service(CFG, unit):
                    return self._send(403, {"error": "service not in the allowed set"})
                ok = homelab.service_restart(unit)
                return self._send(200 if ok else 500, {"ok": ok})

            if path == "/api/docker":
                name = str(b.get("ref", ""))
                if not name:
                    return self._send(400, {"error": "no container"})
                if not homelab.allowed_container(CFG, name):
                    return self._send(403, {"error": "container not in the allowed set"})
                ok = homelab.docker_restart(name)
                return self._send(200 if ok else 500, {"ok": ok})

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
            return self._send(500, {"error": str(e),
                                    "where": traceback.format_exc()[-400:]})


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

        # No reset over the network - a forgotten PIN is reset from the
        # container shell:  aether-homelab reset-pin
        return self._send(404, {"error": "no such path"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=CFG.get("host", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=CFG.get("port", 8788))
    args = ap.parse_args()
    updater.clear_busy()           # a fresh process = any update has settled
    updater.start_auto(CFG)        # no-op unless auto_update is on
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("Aether Homelab on http://%s:%d  (node: %s)"
          % (args.host, args.port, CFG.get("proxmox", {}).get("node")))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
