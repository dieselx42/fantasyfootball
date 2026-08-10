"""WSGI entry point, for hosting.

Vercel (and any other WSGI host) calls :data:`app`. Locally, ``python3 run.py``
uses the stdlib server instead. Both go through :func:`ff.web.server.dispatch`,
so there is exactly one routing implementation and the two cannot drift apart.

Deliberately dependency-free: WSGI is in the standard library, so hosting adds
no framework. The only package the hosted deployment needs is a Postgres
driver.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs

from .web.server import ApiError, dispatch, static_file

#: Optional shared password. When ``APP_PASSWORD`` is set, every request needs
#: a valid session cookie. Without it the deployment is world-writable, which
#: matters because a stranger could clear your draft board. This is a stopgap
#: until per-user sign-in lands; it is not multi-user auth.
PASSWORD_ENV = "APP_PASSWORD"
COOKIE_NAME = "ff_session"
SESSION_TTL = 30 * 24 * 3600


def _password() -> str:
    return os.environ.get(PASSWORD_ENV, "")


def _secret() -> bytes:
    # Derived from the password so no second variable is needed. Rotating the
    # password invalidates existing sessions, which is the desired behaviour.
    return hashlib.sha256(("ff-session:" + _password()).encode()).digest()


def _sign(expires: int) -> str:
    mac = hmac.new(_secret(), str(expires).encode(), hashlib.sha256).hexdigest()[:32]
    return f"{expires}.{mac}"


def _valid_cookie(value: str) -> bool:
    try:
        expires_raw, mac = value.split(".", 1)
        expires = int(expires_raw)
    except (ValueError, AttributeError):
        return False
    if expires < time.time():
        return False
    return hmac.compare_digest(_sign(expires), value)


def _cookies(environ: dict[str, Any]) -> dict[str, str]:
    raw = environ.get("HTTP_COOKIE", "")
    out: dict[str, str] = {}
    for part in raw.split(";"):
        if "=" in part:
            key, _, val = part.partition("=")
            out[key.strip()] = val.strip()
    return out


def _authorised(environ: dict[str, Any]) -> bool:
    if not _password():
        return True
    return _valid_cookie(_cookies(environ).get(COOKIE_NAME, ""))


LOGIN_PAGE = """<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fantasy Assistant</title>
<style>
 body{background:#0e1116;color:#e6edf3;font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
      display:grid;place-items:center;height:100vh;margin:0}
 form{background:#161b22;border:1px solid #272e39;border-radius:10px;padding:28px;width:min(360px,90vw)}
 h1{font-size:18px;margin:0 0 6px} p{color:#8b96a5;font-size:13px;margin:0 0 18px}
 input{width:100%;padding:10px;border-radius:8px;border:1px solid #272e39;background:#1c232d;color:#e6edf3;font-size:15px}
 button{width:100%;margin-top:12px;padding:10px;border:0;border-radius:8px;background:#4c8dff;color:#fff;font-size:15px;cursor:pointer}
 .err{color:#f85149;font-size:13px;margin-top:10px}
</style>
<form method="post" action="/__login">
  <h1>Fantasy Assistant</h1>
  <p>This deployment is password protected.</p>
  <input type="password" name="password" placeholder="Password" autofocus>
  <button type="submit">Enter</button>
  __ERROR__
</form>
"""


def _login_response(start_response: Callable, error: bool = False) -> Iterable[bytes]:
    page = LOGIN_PAGE.replace(
        "__ERROR__", '<div class="err">Incorrect password.</div>' if error else ""
    ).encode()
    start_response(
        "401 Unauthorized",
        [("Content-Type", "text/html; charset=utf-8"),
         ("Content-Length", str(len(page))),
         ("Cache-Control", "no-store")],
    )
    return [page]


def _json_response(start_response: Callable, status: int, payload: Any,
                   extra_headers: list[tuple[str, str]] | None = None) -> Iterable[bytes]:
    body = json.dumps(payload, default=str).encode("utf-8")
    headers = [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
    ]
    headers.extend(extra_headers or [])
    start_response(f"{status} {'OK' if status == 200 else 'Error'}", headers)
    return [body]


def _read_body(environ: dict[str, Any]) -> bytes:
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0:
        return b""
    return environ["wsgi.input"].read(length)


def app(environ: dict[str, Any], start_response: Callable) -> Iterable[bytes]:
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "/") or "/"
    query = parse_qs(environ.get("QUERY_STRING", ""))

    # -- login -------------------------------------------------------------
    if path == "/__login" and method == "POST":
        submitted = parse_qs(_read_body(environ).decode("utf-8", "replace"))
        given = (submitted.get("password") or [""])[0]
        if _password() and hmac.compare_digest(given, _password()):
            expires = int(time.time()) + SESSION_TTL
            cookie = (
                f"{COOKIE_NAME}={_sign(expires)}; Path=/; Max-Age={SESSION_TTL}; "
                f"HttpOnly; SameSite=Lax; Secure"
            )
            start_response("302 Found", [("Location", "/"), ("Set-Cookie", cookie)])
            return [b""]
        return _login_response(start_response, error=True)

    if not _authorised(environ):
        if path.startswith("/api/"):
            return _json_response(start_response, 401, {"error": "Not signed in."})
        return _login_response(start_response)

    # -- health ------------------------------------------------------------
    if path == "/__health":
        from .storage import get_backend

        try:
            backend = get_backend()
            leagues = len(backend.list_leagues())
            return _json_response(
                start_response, 200,
                {"ok": True, "storage": backend.describe(), "leagues": leagues},
            )
        except Exception as exc:                    # noqa: BLE001
            return _json_response(
                start_response, 500,
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            )

    # -- api ---------------------------------------------------------------
    if path.startswith("/api/"):
        body: dict[str, Any] = {}
        if method != "GET":
            raw = _read_body(environ)
            if raw:
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                    body = parsed if isinstance(parsed, dict) else {}
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return _json_response(
                        start_response, 400,
                        {"error": "Request body was not valid JSON."},
                    )
        status, payload = dispatch(method, path, query, body)
        return _json_response(start_response, status, payload)

    # -- static ------------------------------------------------------------
    data, content_type = static_file(path)
    start_response(
        "200 OK",
        [("Content-Type", content_type),
         ("Content-Length", str(len(data))),
         ("Cache-Control", "no-store")],
    )
    return [data]


__all__ = ["app"]
