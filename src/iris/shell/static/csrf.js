// Wires double-submit CSRF for Datastar fetches.
//
// iris.auth.csrf.verify_csrf_header expects every state-changing route to
// carry the iris_csrf cookie value in the X-CSRF-Token header. The cookie
// is set httponly=False precisely so this module can read it.
//
// Datastar issues all server actions (@get / @post / @put / @patch /
// @delete) via window.fetch and tags them with `Datastar-Request: true`.
// We monkey-patch fetch BEFORE Datastar boots (this script must precede
// datastar.js in shell.html) and inject the header on any same-origin
// request that carries that tag.
//
// Reads the cookie on every call (not at module init) so that a token
// rotation server-side — e.g. after login — is picked up immediately.

const COOKIE_NAME = "iris_csrf";

const readCookie = () => {
  const prefix = COOKIE_NAME + "=";
  for (const part of document.cookie.split("; ")) {
    if (part.startsWith(prefix)) return part.slice(prefix.length);
  }
  return "";
};

const origFetch = window.fetch.bind(window);
window.fetch = (input, init = {}) => {
  const headers = new Headers(init.headers ?? (input instanceof Request ? input.headers : undefined));
  if (headers.get("Datastar-Request") && !headers.has("X-CSRF-Token")) {
    const token = readCookie();
    if (token) headers.set("X-CSRF-Token", token);
  }
  return origFetch(input, { ...init, headers });
};
