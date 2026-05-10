from __future__ import annotations

from typing import override

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response


# 'unsafe-eval' on script-src: required by Datastar's reactivity engine.
# It compiles `data-on:click="..."`, `data-text="$x"`, and similar attribute
# expressions via `new Function(...)` at runtime — Function() is treated
# the same as eval() by CSP and is blocked without 'unsafe-eval'.
# Trade-off accepted: Datastar is a first-class dependency of the UI;
# without 'unsafe-eval' every reactive expression in every template fails.
# Datastar itself is vendored under the shell static dir
# (served from /static/shell/datastar.js), so 'self' is sufficient for the
# script source — no CDN allowlist required.
# 'unsafe-inline' on style-src is similarly relaxed for inline style
# attributes that templates and Datastar generate.
#
# Directives below `frame-ancestors` close CSP3 gaps that `default-src`
# does NOT inherit: form-action restricts where forms can POST,
# base-uri 'none' blocks `<base>` injection that would subvert every
# relative URL on the page, and object-src 'none' kills `<embed>` /
# `<object>` / legacy plugin pivots.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "base-uri 'none'; "
    "object-src 'none'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Set defense-in-depth headers on every response.

    - X-Content-Type-Options: nosniff   (no MIME sniffing)
    - X-Frame-Options: DENY             (clickjacking)
    - Referrer-Policy                   (don't leak full URL cross-origin)
    - Content-Security-Policy           (XSS defense-in-depth)
    """

    @override
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        response.headers.setdefault("Content-Security-Policy", _CSP)
        return response
