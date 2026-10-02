"""Notifications on your phone (Web Push), with you choosing what's worth a
buzz.

Each phone that turns notifications on hands the PC a "subscription" - an
address at Apple's or Google's push service plus two keys. The PC encrypts
each notification for that phone alone (RFC 8291, aes128gcm) and signs the
request with its own key (VAPID, RFC 8292), so the push service only ever
carries ciphertext. Nothing here needs an account anywhere.

Every phone has its own settings: a master switch, and for each kind of
notification a tier - Off, Quiet (no sound), Normal, Important (stays on
screen until you deal with it, and goes out with high urgency).
"""
import base64
import hashlib
import hmac
import json
import os
import struct
import threading
import time
import urllib.parse
import urllib.error
import urllib.request

import paths

STORE = paths.data("push.json")
HISTORY = paths.data("notifications.json")

TIERS = ("off", "quiet", "normal", "important")
TYPES = [
    # id, name, what it's for, default tier
    ("security", "Security", "A new phone signs in, or someone keeps typing a wrong code or PIN", "important"),
    ("timer", "Sleep timer", "A minute's warning before the timer locks, sleeps or shuts down the PC", "important"),
    ("downloads", "Downloads", "A file finishes downloading on the PC", "normal"),
    ("steam", "Steam", "A game finishes downloading or updating", "normal"),
    ("health", "PC health", "The GPU runs hot, or the system drive is nearly full", "normal"),
    ("session", "Play time", "How long you played, when you close a game", "off"),
    ("updates", "App updates", "A new version of Aether Remote is out", "quiet"),
]
TYPE_IDS = {t[0] for t in TYPES}
URGENCY = {"quiet": "low", "normal": "normal", "important": "high"}
SUB = "https://github.com/builtbycodyrd/aether-remote"

_lock = threading.Lock()


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s):
    s = str(s)
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ------------------------------------------------------------------ storage

def _load():
    try:
        with open(STORE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    d.setdefault("subs", [])
    if not d.get("vapid_private"):
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        k = ec.generate_private_key(ec.SECP256R1())
        d["vapid_private"] = k.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode()
        d["vapid_public"] = b64u(k.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
        _save(d)
    return d


def _save(d):
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, STORE)


def public_key():
    with _lock:
        return _load()["vapid_public"]


def default_prefs():
    return {"enabled": True, "types": {t[0]: t[3] for t in TYPES}}


def clean_prefs(p):
    p = p if isinstance(p, dict) else {}
    out = default_prefs()
    out["enabled"] = bool(p.get("enabled", True))
    types = p.get("types") if isinstance(p.get("types"), dict) else {}
    for k, v in types.items():
        if k in TYPE_IDS and v in TIERS:
            out["types"][k] = v
    return out


def _valid_sub(sub):
    if not isinstance(sub, dict):
        return None
    ep = str(sub.get("endpoint") or "")
    keys = sub.get("keys") if isinstance(sub.get("keys"), dict) else {}
    u = urllib.parse.urlparse(ep)
    if u.scheme != "https" or not u.hostname:
        return None
    try:
        p256 = unb64u(keys.get("p256dh", ""))
        auth = unb64u(keys.get("auth", ""))
    except Exception:
        return None
    if len(p256) != 65 or p256[0] != 4 or len(auth) < 16:
        return None
    return {"endpoint": ep[:1000], "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}}


def subscribe(sub, prefs, device=""):
    s = _valid_sub(sub)
    if not s:
        raise ValueError("That isn't a push subscription")
    with _lock:
        d = _load()
        d["subs"] = [x for x in d["subs"] if x["endpoint"] != s["endpoint"]]
        s.update(prefs=clean_prefs(prefs), device=str(device)[:80], added=time.time())
        d["subs"].append(s)
        d["subs"] = d["subs"][-12:]
        _save(d)
        _wants["at"] = 0
    return s["prefs"]


def unsubscribe(endpoint):
    with _lock:
        d = _load()
        n = len(d["subs"])
        d["subs"] = [x for x in d["subs"] if x["endpoint"] != endpoint]
        _save(d)
        return n != len(d["subs"])


def set_prefs(endpoint, prefs):
    with _lock:
        d = _load()
        for x in d["subs"]:
            if x["endpoint"] == endpoint:
                x["prefs"] = clean_prefs(prefs)
                _save(d)
                _wants["at"] = 0
                return x["prefs"]
    raise KeyError("this phone isn't subscribed")


def prefs_for(endpoint):
    with _lock:
        for x in _load()["subs"]:
            if x["endpoint"] == endpoint:
                return x["prefs"]
    return None


_wants = {"at": 0.0, "v": set()}


def wants(kind):
    """Does any phone want this kind of notification at all? (Cached a bit -
    the watchers ask every second.)"""
    if time.time() - _wants["at"] > 10:
        try:
            with _lock:
                subs = _load()["subs"]
            v = set()
            for x in subs:
                p = x.get("prefs") or default_prefs()
                if p.get("enabled", True):
                    v |= {k for k, t in p["types"].items() if t != "off"}
            _wants["v"] = v
        except Exception:
            _wants["v"] = set()
        _wants["at"] = time.time()
    return kind in _wants["v"]


def history(limit=30):
    try:
        with open(HISTORY, encoding="utf-8") as f:
            return json.load(f)[-limit:][::-1]
    except Exception:
        return []


def _remember(item):
    try:
        h = []
        if os.path.isfile(HISTORY):
            with open(HISTORY, encoding="utf-8") as f:
                h = json.load(f)
        h = (h + [item])[-60:]
        with open(HISTORY + ".tmp", "w", encoding="utf-8") as f:
            json.dump(h, f)
        os.replace(HISTORY + ".tmp", HISTORY)
    except Exception:
        pass


# --------------------------------------------------------------- the crypto

def _hkdf_extract(salt, ikm):
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def _hkdf_expand(prk, info, n):
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:n]


def encrypt(payload, p256dh_b64, auth_b64, salt=None, private_key=None):
    """RFC 8291 aes128gcm: the body of one Web Push message."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ua_pub = unb64u(p256dh_b64)
    auth = unb64u(auth_b64)
    as_key = private_key or ec.generate_private_key(ec.SECP256R1())
    as_pub = as_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub)
    secret = as_key.exchange(ec.ECDH(), peer)
    ikm = _hkdf_expand(_hkdf_extract(auth, secret), b"WebPush: info\x00" + ua_pub + as_pub, 32)
    salt = salt or os.urandom(16)
    prk = _hkdf_extract(salt, ikm)
    cek = _hkdf_expand(prk, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf_expand(prk, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack(">I", 4096) + bytes([len(as_pub)]) + as_pub + body


def _vapid(endpoint, private_pem):
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    u = urllib.parse.urlparse(endpoint)
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claims = b64u(json.dumps({"aud": "%s://%s" % (u.scheme, u.netloc),
                              "exp": int(time.time()) + 12 * 3600, "sub": SUB}).encode())
    key = serialization.load_pem_private_key(private_pem.encode(), None)
    der = key.sign((head + "." + claims).encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return head + "." + claims + "." + b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def _post(sub, payload, tier, vapid_private, vapid_public, ttl):
    body = encrypt(json.dumps(payload).encode(), sub["keys"]["p256dh"], sub["keys"]["auth"])
    req = urllib.request.Request(sub["endpoint"], data=body, method="POST", headers={
        "TTL": str(ttl), "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "Urgency": URGENCY.get(tier, "normal"),
        "Authorization": "vapid t=%s, k=%s" % (_vapid(sub["endpoint"], vapid_private), vapid_public)})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


# ----------------------------------------------------------------- sending

_last = {}


def notify(kind, title, body, url="/", tag=None, force_tier=None, only=None):
    """Send to every phone that wants this kind. Returns how many it reached.
    The same (kind, tag) isn't repeated within a minute - no buzz storms."""
    if kind not in TYPE_IDS and kind != "test":
        raise ValueError("unknown notification type")
    tag = tag or kind
    now = time.time()
    if kind != "test" and now - _last.get((kind, tag), 0) < 60:
        return 0
    _last[(kind, tag)] = now
    with _lock:
        d = _load()
        subs = [dict(x) for x in d["subs"]]
        vpriv, vpub = d["vapid_private"], d["vapid_public"]
    if kind != "test":
        _remember({"kind": kind, "title": title, "body": body, "ts": now})
    sent, dead = 0, []
    for s in subs:
        if only and s["endpoint"] != only:
            continue
        p = s.get("prefs") or default_prefs()
        tier = force_tier or p["types"].get(kind, "normal")
        if kind != "test" and (not p.get("enabled", True) or tier == "off"):
            continue
        payload = {"title": title, "body": body, "tag": tag, "tier": tier,
                   "url": url, "ts": int(now * 1000), "kind": kind}
        try:
            code = _post(s, payload, tier, vpriv, vpub, 6 * 3600 if tier == "quiet" else 24 * 3600)
        except Exception:
            continue
        if code in (404, 410):
            dead.append(s["endpoint"])           # the phone unsubscribed / reinstalled
        elif 200 <= code < 300:
            sent += 1
    for ep in dead:
        unsubscribe(ep)
    return sent


def notify_async(*a, **kw):
    threading.Thread(target=lambda: _safe(notify, *a, **kw), daemon=True).start()


def _safe(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except Exception:
        pass
