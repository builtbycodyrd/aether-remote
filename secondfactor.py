"""Second factor: Face ID (a passkey) or a PIN, on top of the 30-day session.

Two uses, both enforced by the SERVER, not just the app:

  unlock   - opening the app. A short-lived, signed "ru" cookie, bound to the
             session it was issued for. Without it the API answers "locked".
  step-up  - a one-time token for ONE protected command (shut down, restart,
             sign out, a scene or tile that does one of those). Issued right
             after a fresh Face ID / PIN check, good for 90 seconds, usable once.

Generic on purpose - the homelab add-on uses this same file. It needs a data
file path and a function returning the server's signing key (rotating that key
signs everyone out AND invalidates every unlock/step-up token at once).
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

UNLOCK_TTL = 12 * 3600       # the app re-locks itself sooner (5 min in background)
STEPUP_TTL = 90
CHALLENGE_TTL = 180
PIN_RE = re.compile(r"^\d{4,12}$")
MAX_FAILS = 5                # then locked out, doubling each further round


def _b64(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class SecondFactor:
    def __init__(self, path, key_fn, log=print):
        self.path = path
        self.key_fn = key_fn
        self.log = log
        self.lock = threading.Lock()
        self.challenges = {}      # challenge b64 -> (purpose, scope, expires)
        self.used = {}            # step-up jti -> expires (single use)
        self.st = self._load()

    # ------------------------------------------------------------ storage
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
        d.setdefault("method", None)       # None | "pin" | "passkey"
        d.setdefault("pin", None)
        d.setdefault("passkeys", [])
        d.setdefault("fails", 0)
        d.setdefault("locked_until", 0)
        return d

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.st, f, indent=1)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    # ------------------------------------------------------------- status
    def enabled(self):
        m = self.st.get("method")
        return (m == "pin" and bool(self.st.get("pin"))) or \
               (m == "passkey" and bool(self.st.get("passkeys")))

    def status(self):
        wait = max(0, int(self.st.get("locked_until", 0) - time.time()))
        return {"enabled": self.enabled(), "method": self.st.get("method"),
                "passkeys": len(self.st.get("passkeys", [])),
                "locked_for": wait}

    def reset(self):
        """Turn the second factor off. Only ever reachable from the machine
        itself - being at the PC (or root on the server) is the recovery."""
        with self.lock:
            self.st = {"method": None, "pin": None, "passkeys": [],
                       "fails": 0, "locked_until": 0}
            self.challenges.clear()
            self._save()
        self.log("second factor reset")

    # ------------------------------------------------------------ lockout
    def _locked(self):
        return time.time() < self.st.get("locked_until", 0)

    def _fail(self):
        self.st["fails"] = self.st.get("fails", 0) + 1
        over = self.st["fails"] - MAX_FAILS
        if over >= 0:
            # 5 min, then 10, 20 ... capped at a day.
            self.st["locked_until"] = time.time() + min(300 * 2 ** over, 86400)
        self._save()

    def _ok(self):
        if self.st.get("fails") or self.st.get("locked_until"):
            self.st["fails"] = 0
            self.st["locked_until"] = 0
            self._save()

    def _wait_msg(self):
        return "Too many wrong tries. Try again in %d min." % (
            max(1, int((self.st["locked_until"] - time.time()) // 60 + 1)))

    # ---------------------------------------------------------------- PIN
    @staticmethod
    def _hash(pin, salt):
        return hashlib.scrypt(pin.encode(), salt=salt, n=2 ** 14, r=8, p=1,
                              dklen=32)

    def set_pin(self, pin):
        if not PIN_RE.match(str(pin or "")):
            raise ValueError("A PIN is 4 to 12 digits.")
        salt = secrets.token_bytes(16)
        with self.lock:
            self.st["pin"] = {"salt": _b64(salt), "hash": _b64(self._hash(pin, salt))}
            self.st["method"] = "pin"
            # One method at a time: the old passkeys are forgotten, so a lost
            # phone's passkey can't quietly start working again later.
            self.st["passkeys"] = []
            self.st["fails"] = 0
            self.st["locked_until"] = 0
            self._save()

    def check_pin(self, pin):
        with self.lock:
            if self._locked():
                return False, self._wait_msg()
            p = self.st.get("pin")
            if self.st.get("method") != "pin" or not p:
                return False, "PIN is not set up."
            good = hmac.compare_digest(
                self._hash(str(pin or ""), _unb64(p["salt"])), _unb64(p["hash"]))
            if not good:
                self._fail()
                if self._locked():
                    return False, self._wait_msg()
                left = MAX_FAILS - self.st["fails"]
                return False, "Wrong PIN.%s" % (
                    " %d tries left." % left if 0 < left <= 3 else "")
            self._ok()
            return True, "ok"

    # ---------------------------------------------------- passkey challenges
    def challenge(self, purpose, scope=""):
        c = secrets.token_bytes(32)
        now = time.time()
        with self.lock:
            for k in [k for k, v in self.challenges.items() if v[2] < now]:
                del self.challenges[k]
            self.challenges[_b64(c)] = (purpose, scope, now + CHALLENGE_TTL)
        return c

    def _take(self, cred, purpose, scope):
        """Find, check and CONSUME the challenge the passkey signed."""
        import webauthn
        cdata = json.loads(webauthn.b64url_decode(cred["response"]["clientDataJSON"]))
        key = _b64(webauthn.b64url_decode(cdata.get("challenge", "")))
        with self.lock:
            rec = self.challenges.pop(key, None)
        if not rec or rec[2] < time.time():
            raise ValueError("That request expired - try again.")
        if rec[0] != purpose or rec[1] != scope:
            raise ValueError("That request was for something else.")
        return webauthn.b64url_decode(key)

    def register_passkey(self, cred, rp_id, origin, label=""):
        import webauthn
        ch = self._take(cred, "register", "")
        rec = webauthn.verify_registration(cred, ch, rp_id, origin)
        rec["label"] = str(label or "Passkey")[:40]
        rec["added"] = int(time.time())
        with self.lock:
            self.st["passkeys"] = [p for p in self.st["passkeys"]
                                   if p["cred_id"] != rec["cred_id"]] + [rec]
            self.st["method"] = "passkey"
            self.st["pin"] = None        # one method at a time - no weaker backdoor
            self._save()
        self.log("passkey added (%s)" % rec["label"])

    def check_passkey(self, cred, rp_id, origin, purpose, scope=""):
        import webauthn
        with self.lock:
            if self._locked():
                return False, self._wait_msg()
        try:
            ch = self._take(cred, purpose, scope)
            raw_id = base64.b64encode(webauthn.b64url_decode(cred.get("rawId") or cred["id"])).decode()
            stored = next((p for p in self.st["passkeys"] if p["cred_id"] == raw_id), None)
            if not stored:
                raise ValueError("That passkey isn't registered here.")
            count = webauthn.verify_assertion(cred, ch, rp_id, origin, stored)
        except Exception as e:
            with self.lock:
                self._fail()
            return False, str(e)
        with self.lock:
            stored["sign_count"] = count
            stored["last_used"] = int(time.time())
            self.st["fails"] = 0
            self.st["locked_until"] = 0
            self._save()
        return True, "ok"

    def allow_ids(self):
        """Registered credential ids, base64url - what the browser expects."""
        return [_b64(base64.b64decode(p["cred_id"])) for p in self.st["passkeys"]]

    # -------------------------------------------------------------- tokens
    def _sign(self, payload):
        raw = _b64(json.dumps(payload, separators=(",", ":")).encode())
        sig = hmac.new(self.key_fn().encode(), raw.encode(), hashlib.sha256).hexdigest()[:40]
        return raw + "." + sig

    def _read(self, token):
        if not token or "." not in token:
            return None
        raw, sig = token.rsplit(".", 1)
        want = hmac.new(self.key_fn().encode(), raw.encode(), hashlib.sha256).hexdigest()[:40]
        if not hmac.compare_digest(want, sig):
            return None
        try:
            p = json.loads(_unb64(raw))
        except Exception:
            return None
        return p if p.get("exp", 0) > time.time() else None

    @staticmethod
    def _sid(session_value):
        return hashlib.sha256((session_value or "").encode()).hexdigest()[:24]

    def make_unlock(self, session_value):
        return self._sign({"t": "unlock", "sid": self._sid(session_value),
                           "exp": int(time.time()) + UNLOCK_TTL})

    def valid_unlock(self, token, session_value):
        p = self._read(token)
        return bool(p and p.get("t") == "unlock"
                    and p.get("sid") == self._sid(session_value))

    def make_stepup(self, scope, session_value):
        return self._sign({"t": "stepup", "scope": scope,
                           "sid": self._sid(session_value),
                           "jti": secrets.token_hex(8),
                           "exp": int(time.time()) + STEPUP_TTL})

    def use_stepup(self, token, scope, session_value):
        """Valid for this exact scope and session, and only once."""
        p = self._read(token)
        if not (p and p.get("t") == "stepup" and p.get("scope") == scope
                and p.get("sid") == self._sid(session_value)):
            return False
        now = time.time()
        with self.lock:
            for k in [k for k, e in self.used.items() if e < now]:
                del self.used[k]
            if p["jti"] in self.used:
                return False
            self.used[p["jti"]] = p["exp"]
        return True
