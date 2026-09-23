# auth.py - TOTP login + signed sessions for the phone remote.
# Stdlib only. RFC 6238 TOTP (SHA1, 6 digits, 30s) so any authenticator
# app works: Google Authenticator, Authy, 1Password, Bitwarden, Proton.

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import threading
import time

import paths

HERE = paths.ASSET_DIR
# The one file that must never be shipped, copied or backed up anywhere.
AUTH_PATH = paths.data("auth.json")

ISSUER = "Aether Homelab"
# Whatever PC this is installed on - never a baked-in machine name.
ACCOUNT = (os.environ.get("COMPUTERNAME")
           or os.environ.get("HOSTNAME") or "PC").lower()

STEP = 30
DIGITS = 6
WINDOW = 1          # accept the code before/after to tolerate clock drift

MAX_FAILS = 5       # per IP before a lockout
LOCKOUT = 300       # seconds

_lock = threading.Lock()
_fails = {}         # ip -> [count, locked_until]


# ------------------------------------------------------------------ storage

def _default():
    return {
        "secret": base64.b32encode(secrets.token_bytes(20)).decode().rstrip("="),
        "server_key": secrets.token_hex(32),
        "enrolled": False,
        "last_counter": 0,
        "session_days": 30,
    }


def load():
    if os.path.exists(AUTH_PATH):
        try:
            with open(AUTH_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            for k, v in _default().items():
                d.setdefault(k, v)
            return d
        except Exception:
            pass
    d = _default()
    save(d)
    return d


def save(d):
    tmp = AUTH_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, AUTH_PATH)


STATE = load()


# --------------------------------------------------------------------- totp

def _b32decode(s):
    s = s.strip().replace(" ", "").upper()
    s += "=" * (-len(s) % 8)
    return base64.b32decode(s)


def totp_at(secret, counter):
    key = _b32decode(secret)
    msg = struct.pack(">Q", counter)
    dig = hmac.new(key, msg, hashlib.sha1).digest()
    off = dig[-1] & 0x0F
    code = struct.unpack(">I", dig[off:off + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** DIGITS)).zfill(DIGITS)


def current_code():
    return totp_at(STATE["secret"], int(time.time()) // STEP)


def provisioning_uri():
    return ("otpauth://totp/%s:%s?secret=%s&issuer=%s&algorithm=SHA1"
            "&digits=%d&period=%d"
            % (ISSUER, ACCOUNT, STATE["secret"], ISSUER, DIGITS, STEP))


# --------------------------------------------------------------- rate limit

def _check_lock(ip):
    with _lock:
        rec = _fails.get(ip)
        if not rec:
            return None
        count, until = rec
        if until and time.time() < until:
            return int(until - time.time())
        if until and time.time() >= until:
            _fails.pop(ip, None)
        return None


def _note_fail(ip):
    with _lock:
        count, until = _fails.get(ip, (0, 0))
        count += 1
        if count >= MAX_FAILS:
            until = time.time() + LOCKOUT
            count = 0
        _fails[ip] = (count, until)


def _clear_fails(ip):
    with _lock:
        _fails.pop(ip, None)


# -------------------------------------------------------------- verify code

def verify_code(code, ip="?"):
    """Returns (ok, message). Enforces lockout and single-use per time step."""
    wait = _check_lock(ip)
    if wait:
        return False, "Too many attempts. Try again in %ds." % wait

    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != DIGITS:
        _note_fail(ip)
        return False, "Enter the %d-digit code." % DIGITS

    now = int(time.time()) // STEP
    for drift in range(-WINDOW, WINDOW + 1):
        counter = now + drift
        if hmac.compare_digest(totp_at(STATE["secret"], counter), code):
            # A code is good for one login only - stops a shoulder-surfed
            # code being replayed within its 30s window.
            if counter <= STATE.get("last_counter", 0):
                _note_fail(ip)
                return False, "That code was already used. Wait for the next one."
            STATE["last_counter"] = counter
            STATE["enrolled"] = True
            save(STATE)
            _clear_fails(ip)
            return True, "ok"

    _note_fail(ip)
    return False, "Wrong code."


# ----------------------------------------------------------------- sessions

def _sign(payload_b64):
    key = STATE["server_key"].encode()
    return hmac.new(key, payload_b64.encode(), hashlib.sha256).hexdigest()[:32]


def new_session():
    days = int(STATE.get("session_days", 30))
    payload = {"exp": int(time.time()) + days * 86400,
               "jti": secrets.token_hex(8)}
    raw = base64.urlsafe_b64encode(
        json.dumps(payload).encode()).decode().rstrip("=")
    return "%s.%s" % (raw, _sign(raw)), days * 86400


def valid_session(cookie_value):
    if not cookie_value or "." not in cookie_value:
        return False
    raw, sig = cookie_value.rsplit(".", 1)
    if not hmac.compare_digest(_sign(raw), sig):
        return False
    try:
        pad = "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(raw + pad))
    except Exception:
        return False
    return payload.get("exp", 0) > time.time()


def revoke_all():
    """Rotating the signing key invalidates every issued session."""
    STATE["server_key"] = secrets.token_hex(32)
    save(STATE)
    return True


def reset_enrollment():
    """New secret - every authenticator entry for this remote stops working."""
    STATE["secret"] = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
    STATE["enrolled"] = False
    STATE["last_counter"] = 0
    revoke_all()
    return provisioning_uri()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "uri":
        print(provisioning_uri())
    elif len(sys.argv) > 1 and sys.argv[1] == "code":
        print(current_code())
    elif len(sys.argv) > 1 and sys.argv[1] == "reset":
        print(reset_enrollment())
    else:
        print("enrolled:", STATE["enrolled"])
        print("uri     :", provisioning_uri())
