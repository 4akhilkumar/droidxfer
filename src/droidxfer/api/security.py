"""ASGI middleware: localhost-only Host check, session-token cookie, CSRF/cross-site defenses, headers."""

from __future__ import annotations

import json
import secrets
from http.cookies import SimpleCookie
from urllib.parse import parse_qs

from starlette.types import ASGIApp, Message, Receive, Scope, Send

ALLOWED_FETCH_SITES = {"same-origin", "none"}


class SecurityMiddleware:
    def __init__(self, app: ASGIApp, *, token: str, port: int) -> None:
        self.app = app
        self.token = token
        self.cookie_name = f"droidxfer_{port}"
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://{h}" for h in self.hosts}
        ws = " ".join(f"ws://{h}" for h in sorted(self.hosts))
        self.csp = (
            "default-src 'self'; img-src 'self' data: blob:; media-src 'self'; style-src 'self'; "
            f"script-src 'self'; connect-src 'self' {ws}; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if headers.get("host", "") not in self.hosts:
            await self._reject(scope, receive, send, 403, "invalid host header")
            return
        site = headers.get("sec-fetch-site")
        if site and site not in ALLOWED_FETCH_SITES:
            await self._reject(scope, receive, send, 403, "cross-site request blocked")
            return
        origin = headers.get("origin")
        if origin and origin not in self.origins:
            await self._reject(scope, receive, send, 403, "origin not allowed")
            return

        path = scope["path"]
        if scope["type"] == "http" and path == "/":
            query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            if "token" in query:
                if secrets.compare_digest(query["token"][0].encode(), self.token.encode()):
                    await self._login(send)
                else:
                    await self._reject(scope, receive, send, 403, "invalid token")
                return

        if path.startswith("/api/") and not self._authenticated(headers.get("cookie", "")):
            await self._reject(scope, receive, send, 401, "not authenticated: open the URL printed by `droidxfer serve`")
            return

        if scope["type"] == "websocket":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw = list(message.get("headers", []))
                present = {k.lower() for k, _ in raw}
                extra = {
                    b"x-content-type-options": b"nosniff",
                    b"referrer-policy": b"no-referrer",
                    b"x-frame-options": b"DENY",
                    b"content-security-policy": self.csp.encode(),
                    b"cross-origin-resource-policy": b"same-origin",
                }
                raw.extend((k, v) for k, v in extra.items() if k not in present)
                message["headers"] = raw
            await send(message)

        await self.app(scope, receive, send_with_headers)

    def _authenticated(self, cookie_header: str) -> bool:
        jar = SimpleCookie()
        try:
            jar.load(cookie_header)
        except Exception:
            return False
        morsel = jar.get(self.cookie_name)
        return bool(morsel) and secrets.compare_digest(morsel.value.encode(), self.token.encode())

    async def _login(self, send: Send) -> None:
        cookie = f"{self.cookie_name}={self.token}; HttpOnly; SameSite=Strict; Path=/"
        await send(
            {
                "type": "http.response.start",
                "status": 303,
                "headers": [
                    (b"location", b"/"),
                    (b"set-cookie", cookie.encode()),
                    (b"cache-control", b"no-store"),
                    (b"referrer-policy", b"no-referrer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    async def _reject(self, scope: Scope, receive: Receive, send: Send, status: int, detail: str) -> None:
        if scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.close", "code": 4000 + status})
            return
        body = json.dumps({"detail": detail}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})
