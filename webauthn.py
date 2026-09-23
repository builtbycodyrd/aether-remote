"""Just enough WebAuthn to verify a passkey (Face ID / Windows Hello / a
fingerprint) against this server.

Signature verification uses `cryptography` - a vetted library, not hand-rolled
elliptic-curve math, because this is an auth path. The only thing done by hand
is a tiny CBOR reader for the attestation object and the COSE public key, which
is plain parsing.

Scope on purpose: platform authenticators with user verification. ES256 (the
near-universal one) and RS256 are supported; attestation is not checked, because
enrollment already happens inside an authenticated session over HTTPS - we are
binding a device the signed-in user is holding, not vouching for its make.
"""
import hashlib
import hmac
import json

from cryptography.hazmat.primitives.asymmetric import ec, rsa, padding, utils
from cryptography.hazmat.primitives import hashes
from cryptography.exceptions import InvalidSignature


# ------------------------------------------------------------------ CBOR
# A reader for the small subset WebAuthn uses: unsigned/negative ints, byte and
# text strings, arrays and maps. No floats, tags or indefinite lengths.

class _CBOR:
    def __init__(self, buf):
        self.b = buf
        self.i = 0

    def _byte(self):
        v = self.b[self.i]
        self.i += 1
        return v

    def _uint(self, info):
        if info < 24:
            return info
        if info == 24:
            return self._byte()
        if info == 25:
            v = int.from_bytes(self.b[self.i:self.i + 2], "big"); self.i += 2; return v
        if info == 26:
            v = int.from_bytes(self.b[self.i:self.i + 4], "big"); self.i += 4; return v
        if info == 27:
            v = int.from_bytes(self.b[self.i:self.i + 8], "big"); self.i += 8; return v
        raise ValueError("cbor length form %d" % info)

    def value(self):
        first = self._byte()
        major, info = first >> 5, first & 0x1F
        if major == 0:
            return self._uint(info)
        if major == 1:
            return -1 - self._uint(info)
        if major == 2:
            n = self._uint(info); v = self.b[self.i:self.i + n]; self.i += n; return bytes(v)
        if major == 3:
            n = self._uint(info); v = self.b[self.i:self.i + n]; self.i += n
            return v.decode("utf-8")
        if major == 4:
            return [self.value() for _ in range(self._uint(info))]
        if major == 5:
            out = {}
            for _ in range(self._uint(info)):
                k = self.value()
                out[k] = self.value()
            return out
        raise ValueError("cbor major %d" % major)


def _cbor_first(buf):
    r = _CBOR(buf)
    v = r.value()
    return v, r.i


def b64url_decode(s):
    s = s.replace("-", "+").replace("_", "/")
    return __import__("base64").b64decode(s + "=" * (-len(s) % 4))


# --------------------------------------------------------------- COSE key

def _pubkey_from_cose(cose):
    kty = cose[1]
    if kty == 2:                      # EC2
        if cose.get(-1) != 1:         # crv P-256
            raise ValueError("only P-256 passkeys are supported")
        x = int.from_bytes(cose[-2], "big")
        y = int.from_bytes(cose[-3], "big")
        return ("es256",
                ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key())
    if kty == 3:                      # RSA
        n = int.from_bytes(cose[-1], "big")
        e = int.from_bytes(cose[-2], "big")
        return ("rs256", rsa.RSAPublicNumbers(e, n).public_key())
    raise ValueError("unsupported key type %r" % kty)


# --------------------------------------------------------------- authData
# rpIdHash(32) | flags(1) | signCount(4) | [attestedCredentialData] | [ext]

def _parse_auth_data(ad, want_cred):
    rp_id_hash = ad[:32]
    flags = ad[32]
    sign_count = int.from_bytes(ad[33:37], "big")
    info = {"rp_id_hash": rp_id_hash, "up": bool(flags & 1),
            "uv": bool(flags & 4), "sign_count": sign_count}
    if want_cred:
        # aaguid(16) | credIdLen(2) | credId | credPublicKey(COSE)
        i = 37 + 16
        cid_len = int.from_bytes(ad[i:i + 2], "big"); i += 2
        info["cred_id"] = ad[i:i + cid_len]; i += cid_len
        cose, _ = _cbor_first(ad[i:])
        info["cose"] = cose
    return info


# ----------------------------------------------------------- registration

def verify_registration(cred, challenge, rp_id, origin):
    """Returns {cred_id, alg, public_key_pem, sign_count} to store, or raises."""
    cdata = json.loads(b64url_decode(cred["response"]["clientDataJSON"]))
    if cdata.get("type") != "webauthn.create":
        raise ValueError("wrong clientData type")
    if b64url_decode(cdata["challenge"]) != challenge:
        raise ValueError("challenge mismatch")
    if cdata.get("origin") != origin:
        raise ValueError("origin mismatch")

    att, _ = _cbor_first(b64url_decode(cred["response"]["attestationObject"]))
    info = _parse_auth_data(att["authData"], want_cred=True)
    if info["rp_id_hash"] != hashlib.sha256(rp_id.encode()).digest():
        raise ValueError("rpId mismatch")
    if not info["uv"]:
        raise ValueError("this passkey did not verify the user (no Face ID/PIN)")

    alg, pub = _pubkey_from_cose(info["cose"])
    from cryptography.hazmat.primitives import serialization
    pem = pub.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    import base64
    return {"cred_id": base64.b64encode(info["cred_id"]).decode(),
            "alg": alg, "pem": pem, "sign_count": info["sign_count"]}


# --------------------------------------------------------- authentication

def verify_assertion(cred, challenge, rp_id, origin, stored):
    """Verify a login. `stored` is one of the dicts from verify_registration.
    Returns the new sign_count to persist, or raises on any failure."""
    cdata = json.loads(b64url_decode(cred["response"]["clientDataJSON"]))
    if cdata.get("type") != "webauthn.get":
        raise ValueError("wrong clientData type")
    if b64url_decode(cdata["challenge"]) != challenge:
        raise ValueError("challenge mismatch")
    if cdata.get("origin") != origin:
        raise ValueError("origin mismatch")

    auth_data = b64url_decode(cred["response"]["authenticatorData"])
    info = _parse_auth_data(auth_data, want_cred=False)
    if info["rp_id_hash"] != hashlib.sha256(rp_id.encode()).digest():
        raise ValueError("rpId mismatch")
    if not info["uv"]:
        raise ValueError("the user was not verified (no Face ID/PIN)")

    signed = auth_data + hashlib.sha256(
        b64url_decode(cred["response"]["clientDataJSON"])).digest()
    sig = b64url_decode(cred["response"]["signature"])

    from cryptography.hazmat.primitives import serialization
    pub = serialization.load_pem_public_key(stored["pem"].encode())
    try:
        if stored["alg"] == "es256":
            pub.verify(sig, signed, ec.ECDSA(hashes.SHA256()))
        else:
            pub.verify(sig, signed, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        raise ValueError("signature did not verify")

    # A counter that goes backwards means a cloned authenticator. Real platform
    # authenticators often keep it at 0; only enforce when it is actually used.
    prev = stored.get("sign_count", 0)
    if info["sign_count"] and info["sign_count"] <= prev:
        raise ValueError("stale sign count - possible cloned key")
    return info["sign_count"]
