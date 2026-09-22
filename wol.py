"""Wake-on-LAN: read this PC's MAC, and send a magic packet.

The catch WoL always has: a sleeping PC can't wake itself, so something that
stays on has to send the packet. On Cody's setup that's a Raspberry Pi 400 -
PI_SCRIPT below is the tiny listener it runs. This module is the PC side: it
tells the phone this PC's MAC (so it knows what to wake) and can send a packet
itself, which is handy when another Aether PC on the same LAN is awake.
"""
import re
import socket
import subprocess

CREATE_NO_WINDOW = 0x08000000


def adapters():
    """[{name, desc, mac, ip}] parsed from ipconfig /all - every adapter that
    has a hardware address, with its IPv4 when it has one (i.e. connected)."""
    try:
        txt = subprocess.run(["ipconfig", "/all"], capture_output=True,
                             text=True, timeout=10,
                             creationflags=CREATE_NO_WINDOW).stdout
    except Exception:
        return []
    res, cur = [], None
    for line in txt.splitlines():
        if line and not line[0].isspace():
            if "adapter" in line.lower():
                name = line.rstrip(":").split("adapter", 1)[-1].strip()
                cur = {"name": name or line.strip(), "desc": "",
                       "mac": "", "ip": ""}
                res.append(cur)
            else:
                cur = None
            continue
        if cur is None:
            continue
        s = line.strip()
        low = s.lower()
        if low.startswith("physical address"):
            m = re.search(r"([0-9A-Fa-f]{2}(?:[-:][0-9A-Fa-f]{2}){5})", s)
            if m:
                cur["mac"] = m.group(1).upper().replace("-", ":")
        elif low.startswith("description"):
            cur["desc"] = s.split(":", 1)[-1].strip()
        elif "ipv4 address" in low:
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", s)
            if m:
                cur["ip"] = m.group(1)
    return [a for a in res if a.get("mac")]


_VIRTUAL = ("virtualbox", "vmware", "hyper-v", "wi-fi direct", "loopback",
            "tap-", "tunnel", "bluetooth", "virtual", "pseudo", "npcap",
            "vethernet")


def _is_virtual(desc):
    d = (desc or "").lower()
    return any(k in d for k in _VIRTUAL)


def physical():
    """Real NICs only - the virtual ones VirtualBox/VMware/Hyper-V add just
    confuse a 'which MAC do I wake' question."""
    return [a for a in adapters() if not _is_virtual(a.get("desc", ""))]


def primary():
    """The best guess for the adapter to wake. Wired beats wireless (WoL is a
    wired feature on most machines), and a live link beats a dark one - but a
    disconnected Ethernet port is still a better answer than the Wi-Fi."""
    phys = physical()
    wired = [x for x in phys if "ethernet" in (x.get("desc", "")).lower()]
    wired_up = [x for x in wired if x.get("ip")]
    conn = [x for x in phys if x.get("ip")]
    for group in (wired_up, wired, conn, phys):
        if group:
            return group[0]
    a = adapters()
    return a[0] if a else None


def magic_packet(mac):
    hexmac = re.sub(r"[^0-9A-Fa-f]", "", mac)
    if len(hexmac) != 12:
        raise ValueError("that is not a MAC address")
    return b"\xff" * 6 + bytes.fromhex(hexmac) * 16


def send_wol(mac, broadcast="255.255.255.255"):
    pkt = magic_packet(mac)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        for port in (9, 7):        # both the usual WoL ports
            s.sendto(pkt, (broadcast, port))
    finally:
        s.close()
    return True


# The listener the always-on box (Pi 400) runs. Standard library only, so it
# drops onto a Pi with nothing to install. Shown in the PC's Settings with the
# systemd steps.
PI_SCRIPT = r'''#!/usr/bin/env python3
# Aether Remote - Wake-on-LAN sender. Run this on a box that stays on (a Pi).
# It listens on :8788 and, on /wake?mac=AA:BB:CC:DD:EE:FF, broadcasts a magic
# packet so a sleeping PC on the same LAN wakes up. Standard library only.
import re, socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = 8788

def magic(mac):
    h = re.sub(r"[^0-9A-Fa-f]", "", mac)
    if len(h) != 12:
        raise ValueError("bad MAC")
    return b"\xff" * 6 + bytes.fromhex(h) * 16

def send(mac):
    p = magic(mac)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    for port in (9, 7):
        s.sendto(p, ("255.255.255.255", port))
    s.close()

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _h(self, code):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
    def do_OPTIONS(self):
        self._h(204)
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        if u.path == "/ping":
            self._h(200); self.wfile.write(b'{"ok":true,"app":"aether-wol"}'); return
        if u.path == "/wake":
            mac = (q.get("mac") or [""])[0]
            try:
                send(mac); self._h(200); self.wfile.write(b'{"ok":true}')
            except Exception as e:
                self._h(400)
                self.wfile.write(('{"ok":false,"error":%r}' % str(e)).encode())
            return
        self._h(404); self.wfile.write(b'{"ok":false}')

print("Aether WoL sender on :%d" % PORT)
ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
'''
