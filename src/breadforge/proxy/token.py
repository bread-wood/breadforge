"""HMAC-signed scoped credential tokens for the loopback proxy.

Token format: base64url(json_payload).<hmac_sha256_hex>

The payload carries:
  scope   — which upstream API this token may access ("anthropic"|"openai"|"google")
  node_id — graph node that was issued this token (for audit)
  exp     — unix timestamp after which the token is rejected
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass

VALID_SCOPES = frozenset({"anthropic", "openai", "google"})


class TokenError(Exception):
    """Raised when a token cannot be issued or validated."""


@dataclass
class ScopedToken:
    scope: str
    node_id: str
    expires_at: float

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at


def _require_secret() -> bytes:
    key = os.environ.get("BREADFORGE_PROXY_SECRET", "")
    if not key:
        raise TokenError("BREADFORGE_PROXY_SECRET not set")
    return key.encode()


def issue_token(
    scope: str,
    node_id: str,
    *,
    secret: bytes | None = None,
    expires_seconds: int = 3600,
) -> str:
    """Return a signed scoped token string.

    Args:
        scope: One of "anthropic", "openai", "google".
        node_id: Graph node that is being issued this token.
        secret: HMAC key bytes. Falls back to BREADFORGE_PROXY_SECRET env var.
        expires_seconds: Lifetime in seconds (default 1 hour).
    """
    if scope not in VALID_SCOPES:
        raise TokenError(f"unknown scope {scope!r}; must be one of {sorted(VALID_SCOPES)}")
    if secret is None:
        secret = _require_secret()

    payload = {"scope": scope, "node_id": node_id, "exp": time.time() + expires_seconds}
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = hmac.new(secret, payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def validate_token(token: str, *, secret: bytes | None = None) -> ScopedToken:
    """Validate *token* and return a ScopedToken.

    Raises TokenError on any validation failure (malformed, bad signature, expired).
    """
    if secret is None:
        secret = _require_secret()

    try:
        payload_b64, sig = token.rsplit(".", 1)
    except ValueError:
        raise TokenError("malformed token: missing signature separator") from None

    expected = hmac.new(secret, payload_b64.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise TokenError("invalid token signature")

    # Restore padding stripped by issue_token
    padding = 4 - len(payload_b64) % 4
    if padding != 4:
        payload_b64 += "=" * padding

    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception as exc:
        raise TokenError(f"malformed token payload: {exc}") from exc

    try:
        tok = ScopedToken(
            scope=payload["scope"],
            node_id=payload["node_id"],
            expires_at=float(payload["exp"]),
        )
    except KeyError as exc:
        raise TokenError(f"missing field in token payload: {exc}") from exc

    if tok.scope not in VALID_SCOPES:
        raise TokenError(f"unknown scope in token: {tok.scope!r}")
    if tok.expired:
        raise TokenError("token has expired")

    return tok
