"""Drive Keycloak's authorize -> login form -> callback dance from a test.

TestClient can't follow a redirect to a different host, so the OAuth route
flow has to be split: TestClient handles the iris-side hops, a real httpx
client handles the Keycloak-side hops (login page GET + form POST).

The form-action regex is the only place that's coupled to Keycloak's login
HTML. A future Keycloak major bump that changes the layout is a one-line fix
here.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient

_FORM_ACTION_RE = re.compile(r'<form[^>]*\baction="([^"]+)"', re.IGNORECASE)


def _extract_form_action(html: str) -> str:
    m = _FORM_ACTION_RE.search(html)
    if not m:
        msg = (
            'Could not find <form action="..."> in Keycloak login page. '
            "The login template may have changed; update _FORM_ACTION_RE."
        )
        raise AssertionError(msg)
    # Keycloak renders the action with HTML entities (&amp;); decode them.
    return m.group(1).replace("&amp;", "&")


def simulate_login(
    *,
    test_client: TestClient,
    http: httpx.Client,
    username: str,
    password: str,
) -> httpx.Response:
    """Drive the full Authorization Code flow against a real Keycloak.

    Returns the iris-side response that has just received the callback —
    the same response a browser would see at the end of the redirect chain.

    Raises AssertionError on any unexpected HTTP behavior so failures
    surface as clear test errors, not opaque KeyError.
    """
    # 1. iris -> Keycloak: 302 to authorize endpoint, sets oauth_state cookie
    r = test_client.get("/login", follow_redirects=False)
    if r.status_code != 302 or "location" not in r.headers:
        raise AssertionError(
            f"Expected 302 from /login, got {r.status_code}: {r.text[:200]}"
        )
    authorize_url = r.headers["location"]

    # 2. user-agent visits the Keycloak login page
    page = http.get(authorize_url)
    if page.status_code != 200:
        raise AssertionError(
            f"Expected 200 from authorize page, got {page.status_code}"
        )
    form_action = _extract_form_action(page.text)

    # 3. POST credentials; Keycloak responds with 302 to our callback
    submit = http.post(
        form_action,
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    if submit.status_code != 302:
        msg = (
            f"Expected 302 from Keycloak login form (got {submit.status_code}). "
            "If 200, Keycloak rendered the login page again — check the credentials."
        )
        raise AssertionError(msg)
    callback_url = submit.headers["location"]
    if "code=" not in callback_url:
        raise AssertionError(
            f"Keycloak redirect did not carry a `code` param: {callback_url}"
        )

    # 4. browser hits our callback with code+state, carrying the oauth_state cookie
    return test_client.get(callback_url, follow_redirects=False)


def obtain_authorization_code(
    *,
    test_client: TestClient,
    http: httpx.Client,
    username: str,
    password: str,
) -> tuple[str, str]:
    """Drive Keycloak's authorize+login flow and return (code, state).

    Stops one step short of simulate_login: instead of re-entering iris's
    callback, returns the raw `code` and `state` so a provider-level test
    can call OAuthProvider.exchange_code() directly.
    """
    r = test_client.get("/login", follow_redirects=False)
    authorize_url = r.headers["location"]
    page = http.get(authorize_url)
    form_action = _extract_form_action(page.text)
    submit = http.post(
        form_action,
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    if submit.status_code != 302:
        raise AssertionError(
            f"Keycloak login did not redirect (status={submit.status_code})"
        )
    callback_url = submit.headers["location"]
    qs = parse_qs(urlparse(callback_url).query)
    return qs["code"][0], qs["state"][0]
