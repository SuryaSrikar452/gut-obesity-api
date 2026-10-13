"""Stateless HMAC-signed, expiring tokens (sessions and QR PDF downloads). Stdlib only."""
import base64, hashlib, hmac, json, os, time


class TokenError(Exception):
    pass


def _secret() -> bytes:
    s = os.environ.get("TOKEN_SECRET", "")
    if len(s) < 32:
        raise RuntimeError("TOKEN_SECRET env var must be set to >= 32 random characters")
    return s.encode()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(purpose: str, claims: dict, ttl_seconds: int, now=None) -> str:
    body = dict(claims, p=purpose, exp=int((now or time.time()) + ttl_seconds))
    payload = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def read_token(token: str, purpose: str, now=None) -> dict:
    try:
        payload, sig = token.split(".")
    except ValueError:
        raise TokenError("invalid")
    good = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, good):
        raise TokenError("invalid")
    try:
        body = json.loads(_unb64(payload))
    except Exception:
        raise TokenError("invalid")
    if body.get("p") != purpose:
        raise TokenError("invalid")
    if body.get("exp", 0) < (now or time.time()):
        raise TokenError("expired")
    return body
