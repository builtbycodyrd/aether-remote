"""HTTPS for Aether Remote, using this machine's Tailscale certificate.

Why: passkeys (Face ID / fingerprint / Windows Hello) only work on a secure
origin with a real hostname. Tailscale issues a real Let's Encrypt certificate
for  <machine>.<tailnet>.ts.net  when HTTPS is enabled on the tailnet, so the
phone gets https://<that name>:8787 with no warnings and no setup.

When there is no Tailscale, or its HTTPS certificates are off, nothing here
applies and the server keeps serving plain HTTP exactly as before.

One port serves both: a TLS handshake is answered as HTTPS, and a plain HTTP
request is redirected to the https address (so an old bookmark still works) -
except /ping, which answers directly so local health checks never depend on
DNS for the .ts.net name.
"""
import json
import os
import socket
import ssl
import subprocess
import threading
import time

import paths

TLS_DIR = paths.data("tls")
CERT = os.path.join(TLS_DIR, "cert.pem")
KEY = os.path.join(TLS_DIR, "key.pem")
RENEW_EVERY = 12 * 3600      # `tailscale cert` only re-issues near expiry
NO_WINDOW = 0x08000000


def _run(args, timeout=60):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          creationflags=NO_WINDOW if os.name == "nt" else 0)


def cert_domain(tailscale_exe):
    """This machine's tailnet HTTPS name, or None if HTTPS certs are off."""
    try:
        st = json.loads(_run([tailscale_exe, "status", "--json"], 15).stdout)
        doms = st.get("CertDomains") or []
        return doms[0] if doms else None
    except Exception:
        return None


def fetch(tailscale_exe, domain, log=print):
    """Get or renew the certificate. Keeps the old files if it fails."""
    os.makedirs(TLS_DIR, exist_ok=True)
    tmp_c, tmp_k = CERT + ".new", KEY + ".new"
    try:
        r = _run([tailscale_exe, "cert", "--cert-file", tmp_c,
                  "--key-file", tmp_k, domain], 120)
        if r.returncode != 0 or not (os.path.isfile(tmp_c) and os.path.isfile(tmp_k)):
            log("tailscale cert failed: %s" % (r.stderr or r.stdout).strip()[:200])
            return False
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(tmp_c, tmp_k)   # sanity
        os.replace(tmp_c, CERT)
        os.replace(tmp_k, KEY)
        return True
    except Exception as e:
        log("tailscale cert error: %s" % e)
        return False
    finally:
        for p in (tmp_c, tmp_k):
            try:
                os.remove(p)
            except OSError:
                pass


def context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(CERT, KEY)
    return ctx


def have_cert():
    return os.path.isfile(CERT) and os.path.isfile(KEY)


def start_renewal(server, tailscale_exe, domain, log=print, first_delay=RENEW_EVERY):
    """Renew in the background and swap the live context - new connections
    pick up the new certificate; open ones are untouched."""
    def loop():
        delay = first_delay
        while True:
            time.sleep(delay)
            delay = RENEW_EVERY
            if fetch(tailscale_exe, domain, log):
                try:
                    server.ssl_ctx = context()
                except Exception as e:
                    log("could not load renewed certificate: %s" % e)
    threading.Thread(target=loop, daemon=True).start()


def dual_server(base_cls):
    """A server class that speaks HTTPS and redirects plain HTTP, on one port."""

    class Dual(base_cls):
        ssl_ctx = None          # set when HTTPS is on
        https_base = None       # e.g. https://pc.tailnet.ts.net:8787
        plain_handler = None    # handler for non-TLS connections

        def finish_request(self, request, client_address):
            ctx = self.ssl_ctx
            if ctx is None:
                return super().finish_request(request, client_address)
            try:
                request.settimeout(10)
                first = request.recv(1, socket.MSG_PEEK)
            except OSError:
                return
            if first == b"\x16":                     # a TLS ClientHello
                try:
                    tls = ctx.wrap_socket(request, server_side=True)
                except (ssl.SSLError, OSError):
                    return                            # bad handshake - drop it
                tls.settimeout(None)
                try:
                    self.RequestHandlerClass(tls, client_address, self)
                finally:
                    try:
                        tls.close()
                    except OSError:
                        pass
                return
            request.settimeout(None)
            return self.plain_handler(request, client_address, self)

    return Dual


def redirect_handler(base_handler):
    """Plain-HTTP visitors get sent to the https address; /ping answers."""

    class Redirect(base_handler):
        def log_message(self, *a):
            pass

        def _handoff(self):
            """The phone app page at the old address. Browser storage belongs
            to one exact address, so a plain redirect would leave the phone's
            PC list (names, Wake-on-LAN details) behind. This page reads it and
            carries it across in the URL fragment, which is never sent to any
            server, then goes to the https address."""
            dest = json.dumps(self.server.https_base + "/")
            body = ("<!doctype html><meta charset=utf-8>"
                    "<meta name=viewport content='width=device-width'>"
                    "<title>Aether Remote</title>"
                    "<p style='font-family:sans-serif;color:#888'>Moving to the "
                    "secure address…</p><script>"
                    "var d=" + dest + ";"
                    "try{var p=localStorage.getItem('aether.pcs');"
                    "if(p)d+='#import='+encodeURIComponent(p)"
                    "+'&from='+encodeURIComponent(location.origin+'/');}catch(e){}"
                    "location.replace(d);</script>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _go(self):
            if self.command == "GET" and self.path.split("?")[0] in ("/", "/index.html"):
                return self._handoff()
            if self.path.split("?")[0].rstrip("/") == "/ping":
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            # 308 keeps the method, so a POST is re-sent as a POST.
            self.send_response(308 if self.command not in ("GET", "HEAD") else 301)
            self.send_header("Location", self.server.https_base + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = _go

    return Redirect
