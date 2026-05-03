def test_html_responses_have_security_headers(authed_client):
    """Standard security headers are set on HTML responses."""
    r = authed_client.get("/")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    csp = r.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert "cdn.jsdelivr.net" in csp  # Datastar
    assert "frame-ancestors 'none'" in csp


def test_api_responses_have_security_headers(authed_client):
    """Same headers on API responses (defense-in-depth, harmless on JSON)."""
    r = authed_client.get("/api/whoami")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"


def test_login_form_has_security_headers(client):
    """Login is unauthenticated; headers must still apply."""
    r = client.get("/login")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert "default-src 'self'" in r.headers.get("Content-Security-Policy", "")
