"""Resolve a request's client IP, honoring trusted X-Forwarded-For when configured."""
from __future__ import annotations

from fastapi import Request


def client_ip(request: Request, *, trust_forwarded: bool) -> str:
    """Return the client IP for rate-limiting / audit logging.

    When ``trust_forwarded`` is True and ``X-Forwarded-For`` is non-empty,
    return its leftmost (original-client) entry. Otherwise return
    ``request.client.host`` (or "unknown" if Starlette didn't populate it).

    Per OWASP, the leftmost IP in X-Forwarded-For is the original client;
    subsequent IPs are intermediate proxies. Operators MUST configure their
    trusted proxy to strip any client-supplied X-Forwarded-For before adding
    its own — otherwise an attacker can spoof the leftmost value.
    """
    if trust_forwarded:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            first = xff.split(",", 1)[0].strip()
            if first:
                return first
    return request.client.host if request.client else "unknown"
