"""Capability tokens, tenants, signed photo URLs and rate limits.

Tokens are never stored. Each is HMAC-SHA256(JANUS_SECRET, "v1|tenant|threshold|role"), 256 bits,
base64url. Holding the buyer token lets you act as the customer on that one threshold, holding the
seller token lets you act as the provider; a tenant key acts as the provider for every threshold of
that tenant, and only that tenant. Rotating JANUS_SECRET revokes every link at once.

Secrets come only from the environment: JANUS_SECRET, JANUS_TENANT_KEYS, JANUS_ADMIN_TOKEN.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Optional

from .clock import utcnow
from .constants import PHOTO_URL_TTL, TOKEN_BYTES
from .logs import log_event

MIN_KEY_CHARS = 22  # 22 base64url characters = 132 bits; shorter tenant or admin keys are refused


class SecurityConfigError(ValueError):
    pass


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def load_secret(value: Optional[str], allow_ephemeral: bool) -> bytes:
    if value:
        if len(value) < 32:
            raise SecurityConfigError("JANUS_SECRET must be at least 32 characters (use `python -m janus secret`)")
        return value.encode()
    if not allow_ephemeral:
        raise SecurityConfigError("JANUS_SECRET is required outside mock mode")
    log_event("security", "ephemeral_secret", warning="JANUS_SECRET not set: links stop working on restart")
    return secrets.token_bytes(TOKEN_BYTES)


def parse_keys(spec: str) -> dict[str, str]:
    """'acme:KEY1,beta:KEY2' -> {KEY1: 'acme', KEY2: 'beta'}"""
    out = {}
    for part in filter(None, (p.strip() for p in (spec or "").split(","))):
        tenant, _, key = part.partition(":")
        if not tenant or len(key) < MIN_KEY_CHARS:
            raise SecurityConfigError(f"tenant key for '{tenant}' must be at least {MIN_KEY_CHARS} characters")
        out[key] = tenant
    return out


@dataclass
class Principal:
    role: str                      # buyer | seller | tenant | admin | public
    tenant: Optional[str] = None
    threshold_id: Optional[str] = None


class Security:
    def __init__(self, secret: bytes, tenant_keys: Optional[dict[str, str]] = None, admin_token: str = ""):
        if admin_token and len(admin_token) < MIN_KEY_CHARS:
            raise SecurityConfigError(f"JANUS_ADMIN_TOKEN must be at least {MIN_KEY_CHARS} characters")
        self._secret, self.tenant_keys, self.admin_token = secret, tenant_keys or {}, admin_token

    def _mac(self, msg: str) -> bytes:
        return hmac.new(self._secret, msg.encode(), hashlib.sha256).digest()

    def token(self, tenant: str, threshold_id: str, role: str) -> str:
        assert role in ("buyer", "seller")
        return _b64(self._mac(f"v1|{tenant}|{threshold_id}|{role}"))

    def role_for(self, tenant: str, threshold_id: str, presented: Optional[str]) -> Optional[str]:
        """Which role (if any) the presented credential grants on this threshold."""
        if not presented:
            return None
        for role in ("buyer", "seller"):
            if hmac.compare_digest(presented, self.token(tenant, threshold_id, role)):
                return role
        t = self.tenant_of_key(presented)
        if t is not None and t == tenant:
            return "seller"
        if self.admin_token and hmac.compare_digest(presented, self.admin_token):
            return "admin"
        return None

    def tenant_of_key(self, presented: Optional[str]) -> Optional[str]:
        if not presented:
            return None
        for key, tenant in self.tenant_keys.items():
            if hmac.compare_digest(presented, key):
                return tenant
        return None

    def is_admin(self, presented: Optional[str]) -> bool:
        return bool(self.admin_token and presented and hmac.compare_digest(presented, self.admin_token))

    # --- signed, short-lived photo URLs ---

    def sign_path(self, path: str, ttl_seconds: Optional[int] = None) -> str:
        exp = int(utcnow().timestamp()) + int(ttl_seconds or PHOTO_URL_TTL.total_seconds())
        sig = _b64(self._mac(f"file|{path}|{exp}"))
        return f"{path}?exp={exp}&sig={sig}"

    def check_signed(self, path: str, exp: Optional[str], sig: Optional[str]) -> bool:
        if not exp or not sig or not exp.isdigit() or int(exp) < int(utcnow().timestamp()):
            return False
        return hmac.compare_digest(sig, _b64(self._mac(f"file|{path}|{exp}")))


class RateLimiter:
    """Token bucket per (client, bucket). Thread-safe; in-memory, so per process."""
    LIMITS = {"read": (120, 60.0), "write": (30, 60.0), "expensive": (6, 60.0), "webhook": (300, 60.0)}

    def __init__(self, limits: Optional[dict[str, tuple[int, float]]] = None):
        self.limits = limits or dict(self.LIMITS)
        self._state: dict[tuple[str, str], tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, client: str, bucket: str) -> tuple[bool, float]:
        cap, per = self.limits[bucket]
        rate = cap / per
        now = time.monotonic()
        with self._lock:
            tokens, last = self._state.get((client, bucket), (float(cap), now))
            tokens = min(float(cap), tokens + (now - last) * rate)
            if tokens >= 1:
                self._state[(client, bucket)] = (tokens - 1, now)
                return True, 0.0
            self._state[(client, bucket)] = (tokens, now)
            return False, (1 - tokens) / rate
