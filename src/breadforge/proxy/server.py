"""Loopback credential proxy server.

Listens on 127.0.0.1 (random port) and forwards requests to upstream AI APIs.
Each request must carry a scoped proxy token in the Authorization or x-api-key
header; the proxy validates the token, replaces it with the real API key, and
streams the response back to the caller.

Upstream routing by scope:
  anthropic → https://api.anthropic.com
  openai    → https://api.openai.com
  google    → https://generativelanguage.googleapis.com
"""

from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpx

from breadforge.proxy.token import ScopedToken, TokenError, issue_token, validate_token

# Real API key env-var names per scope
_KEY_ENVVAR: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
}

_UPSTREAM_BASE: dict[str, str] = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
    "google": "https://generativelanguage.googleapis.com",
}

# Headers that must not be forwarded verbatim
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


def _extract_token(headers: Any) -> str | None:
    """Pull token string from Authorization: Bearer/x-api-key, or x-api-key header."""
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:]
    if auth.lower().startswith("x-api-key "):
        return auth[10:]
    return headers.get("x-api-key") or headers.get("X-Api-Key") or None


class _ProxyHandler(BaseHTTPRequestHandler):
    """Request handler that validates a scoped token and proxies the request."""

    # Injected by CredentialProxy when the handler class is created
    proxy: CredentialProxy

    def _reject(self, code: int, message: str) -> None:
        body = message.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self) -> None:
        token_str = _extract_token(self.headers)
        if not token_str:
            self._reject(401, "missing credential token")
            return

        try:
            scoped: ScopedToken = validate_token(token_str, secret=self.proxy._secret)
        except TokenError as exc:
            self._reject(401, str(exc))
            return

        upstream_base = _UPSTREAM_BASE.get(scoped.scope)
        if not upstream_base:
            self._reject(400, f"unknown scope: {scoped.scope!r}")
            return

        real_key = os.environ.get(_KEY_ENVVAR.get(scoped.scope, ""), "")

        # Read request body
        content_length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(content_length) if content_length else b""

        # Build forwarded headers (strip hop-by-hop and auth)
        fwd: dict[str, str] = {}
        for k, v in self.headers.items():
            if k.lower() in _HOP_BY_HOP:
                continue
            if k.lower() in ("authorization", "x-api-key"):
                continue
            fwd[k] = v

        # Inject real credentials
        if scoped.scope == "anthropic":
            fwd["x-api-key"] = real_key
        else:
            fwd["Authorization"] = f"Bearer {real_key}"

        url = upstream_base + self.path

        try:
            with httpx.stream(
                self.command,
                url,
                headers=fwd,
                content=body,
                timeout=300.0,
            ) as resp:
                self.send_response(resp.status_code)
                for k, v in resp.headers.items():
                    if k.lower() in _HOP_BY_HOP:
                        continue
                    self.send_header(k, v)
                self.end_headers()
                for chunk in resp.iter_bytes():
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except httpx.RequestError as exc:
            self._reject(502, f"upstream request failed: {exc}")

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_PATCH = _handle

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D102
        pass  # suppress default stderr output


class CredentialProxy:
    """Loopback HTTP proxy that validates scoped tokens and injects real credentials.

    Usage::

        proxy = CredentialProxy()
        proxy.start()
        token = proxy.issue_token("anthropic", node_id="v1-build-core")
        # pass token + proxy.base_url to agent subprocess
        proxy.stop()

    Or as a context manager::

        with CredentialProxy() as proxy:
            token = proxy.issue_token("anthropic", node_id="v1-build-core")
            ...
    """

    def __init__(self, secret: bytes | None = None) -> None:
        if secret is None:
            key = os.environ.get("BREADFORGE_PROXY_SECRET", "")
            # Use env secret if available; otherwise generate an ephemeral key.
            secret = key.encode() if key else os.urandom(32)
        self._secret: bytes = secret
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Bind to a random loopback port and start serving in a daemon thread."""
        handler_cls = type("_Handler", (_ProxyHandler,), {"proxy": self})
        self._server = HTTPServer(("127.0.0.1", 0), handler_cls)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="breadforge-proxy"
        )
        self._thread.start()

    def stop(self) -> None:
        """Shutdown the server and join the daemon thread."""
        if self._server:
            self._server.shutdown()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> CredentialProxy:
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def port(self) -> int:
        """Bound port number. Only valid after start()."""
        if not self._server:
            raise RuntimeError("proxy has not been started")
        return self._server.server_address[1]  # type: ignore[return-value]

    @property
    def base_url(self) -> str:
        """Base URL to pass as ANTHROPIC_BASE_URL / OPENAI_BASE_URL."""
        return f"http://127.0.0.1:{self.port}"

    @property
    def running(self) -> bool:
        return self._server is not None

    # ------------------------------------------------------------------
    # Token issuance
    # ------------------------------------------------------------------

    def issue_token(self, scope: str, node_id: str, *, expires_seconds: int = 3600) -> str:
        """Issue a scoped token for *node_id* using this proxy's secret."""
        return issue_token(scope, node_id, secret=self._secret, expires_seconds=expires_seconds)
