from __future__ import annotations

from fastapi import Request, Response

from iris.auth.csrf import CSRF_FORM_FIELD, attach_csrf_cookie, mint_csrf_token


def render_login_form(
    request: Request, error_messages: dict[str, str]
) -> Response:
    """Render the username/password login form with CSRF + error messaging.

    Shared by MockProvider and LDAPProvider. `error_messages` maps each
    provider-specific error token to its user-facing string; unknown tokens
    fall back to "An error occurred."; absent `error` query param shows "".
    """
    templates = request.app.state.templates
    next_url = request.query_params.get("next", "/")
    error = request.query_params.get("error")
    error_message = (
        error_messages.get(error or "", "An error occurred.") if error else ""
    )
    token = mint_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "auth/ldap_form.html",
        {
            "csrf_field": CSRF_FORM_FIELD,
            "csrf_token": token,
            "next_url": next_url,
            "error": bool(error),
            "error_message": error_message,
        },
    )
    attach_csrf_cookie(request, response, token)
    return response
