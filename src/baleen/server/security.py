"""Loopback, token, origin (spec §13 SEC-1…SEC-7, DR-25, AC-12).

Any web page the user visits could try to call this local server. The middleware below makes
that useless:

- SEC-4: the Host header must be 127.0.0.1:<port> or localhost:<port> (DNS rebinding).
- SEC-1 (defence in depth): requests must come from a loopback address.
- SEC-5: every state-changing method needs Origin equal to the server origin, or, when Origin
  is absent, a Referer on the server origin. GET/HEAD never change state.
- SEC-2/SEC-3: every route except /auth and /static/* needs the session cookie, compared in
  constant time. /auth exchanges the 256-bit launch token for that cookie.
- SEC-6: CSP, nosniff, Referrer-Policy on every response; no-store on dynamic ones.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import cookie_parser
from starlette.types import ASGIApp, Message, Receive, Scope, Send

COOKIE = "baleen_session"
CSP = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
SAFE_METHODS = frozenset({"GET", "HEAD"})
LOOPBACK_CLIENTS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
EXEMPT_PATHS = frozenset({"/auth"})
STATIC_PREFIX = "/static/"


@dataclass(frozen=True)
class Secrets:
    """Per-process secrets (SEC-2). The token travels in the launch URL; the session value
    lives only in the HttpOnly cookie."""

    token: str
    session: str

    @classmethod
    def generate(cls) -> Secrets:
        return cls(secrets.token_urlsafe(32), secrets.token_urlsafe(32))  # 32 bytes = 256 bits


def same(a: str | None, b: str) -> bool:
    """Constant-time string comparison (SEC-3)."""
    if a is None:
        return False
    return hmac.compare_digest(a.encode("utf-8", "surrogateescape"), b.encode("utf-8"))


def allowed_hosts(port: int) -> frozenset[str]:
    return frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})


def session_cookie_header(value: str) -> str:
    """HttpOnly, SameSite=Strict, Path=/, no Expires/Max-Age: a session cookie (SEC-2)."""
    return f"{COOKIE}={value}; HttpOnly; SameSite=Strict; Path=/"


def origin_ok(headers: Headers, host: str) -> bool:
    """SEC-5: Origin equals the server origin; when absent, Referer must be on it."""
    expected = f"http://{host}"
    origin = headers.get("origin")
    if origin is not None:
        return origin == expected
    referer = headers.get("referer")
    if referer is None:
        return False
    return referer == expected or referer.startswith(expected + "/")


def has_session(headers: Headers, session: str) -> bool:
    raw = headers.get("cookie")
    if not raw:
        return False
    return same(cookie_parser(raw).get(COOKIE), session)


class SecurityMiddleware:
    """Pure ASGI middleware wrapping the whole app, so even error responses get the headers."""

    def __init__(self, app: ASGIApp, *, secrets: Secrets, port: int,
                 forbidden_page: Callable[[], str]) -> None:
        self.app = app
        self.secrets = secrets
        self.port = port
        self.hosts = allowed_hosts(port)
        self.forbidden_page = forbidden_page

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            # No websockets in Baleen.
            await send({"type": "websocket.close", "code": 1008})
            return
        headers = Headers(scope=scope)
        host = (headers.get("host") or "").lower()
        path: str = scope.get("path", "")
        method: str = scope.get("method", "GET").upper()
        static = path.startswith(STATIC_PREFIX)

        if host not in self.hosts:
            await self._plain(send, 400, "Invalid Host header.")
            return
        client = scope.get("client")
        if client and client[0] not in LOOPBACK_CLIENTS:
            await self._plain(send, 403, "Forbidden.")
            return
        if method not in SAFE_METHODS and not origin_ok(headers, host):
            await self._forbidden(send)
            return
        if not static and path not in EXEMPT_PATHS and not has_session(headers, self.secrets.session):
            await self._forbidden(send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                add_security_headers(message, static=static)
            await send(message)

        await self.app(scope, receive, send_with_headers)

    async def _forbidden(self, send: Send) -> None:
        """W-21: the 403 page explains how to open Baleen from its launcher."""
        try:
            body = self.forbidden_page().encode("utf-8")
            ctype = b"text/html; charset=utf-8"
        except Exception:
            body, ctype = b"Forbidden.", b"text/plain; charset=utf-8"
        await self._send(send, 403, body, ctype)

    async def _plain(self, send: Send, status: int, text: str) -> None:
        await self._send(send, status, text.encode("utf-8"), b"text/plain; charset=utf-8")

    async def _send(self, send: Send, status: int, body: bytes, ctype: bytes) -> None:
        message: Message = {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", ctype), (b"content-length", str(len(body)).encode())],
        }
        add_security_headers(message, static=False)
        await send(message)
        await send({"type": "http.response.body", "body": body})


def add_security_headers(message: Message, *, static: bool) -> None:
    """SEC-6 headers. Static files are revalidated (no-cache); everything else is no-store."""
    h = MutableHeaders(scope=message)
    h["Content-Security-Policy"] = CSP
    h["X-Content-Type-Options"] = "nosniff"
    h["Referrer-Policy"] = "same-origin"
    h["X-Frame-Options"] = "DENY"
    h["Cross-Origin-Resource-Policy"] = "same-origin"
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    if static:
        if "cache-control" not in h:
            h["Cache-Control"] = "no-cache"
    else:
        h["Cache-Control"] = "no-store"
