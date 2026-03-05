"""Loopback credential proxy — scoped token issuance and HTTP proxy server.

The proxy replaces raw API key injection in agent subprocesses.  Each agent
subprocess receives a short-lived scoped token instead of a real API key.
The loopback server validates the token and injects the real credential before
forwarding to the upstream API.

Quick usage::

    from breadforge.proxy import CredentialProxy

    with CredentialProxy() as proxy:
        token = proxy.issue_token("anthropic", node_id="v1-build-core")
        # Set in subprocess env:
        #   ANTHROPIC_BASE_URL = proxy.base_url
        #   ANTHROPIC_API_KEY  = token
"""

from breadforge.proxy.server import CredentialProxy
from breadforge.proxy.token import ScopedToken, TokenError, issue_token, validate_token

__all__ = [
    "CredentialProxy",
    "ScopedToken",
    "TokenError",
    "issue_token",
    "validate_token",
]
