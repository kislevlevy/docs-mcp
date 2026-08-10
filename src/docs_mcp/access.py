"""Pure-ASGI access control.

The spec requires Origin validation to block DNS-rebinding attacks. The default
policy here is the one that is both safe and deployable: a request carrying *any*
Origin is rejected unless it is allow-listed (browsers always send Origin
cross-origin; MCP clients send none), while the Host check stays off until you
opt in — an empty Host allow-list combined with the check enabled would reject
every request and brick the deployment.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Sequence

_UNPROTECTED_PATHS = frozenset({"/health"})


def _matches(value: str, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        if pattern == value:
            return True
        # `example.com:*` — any port on that host.
        if pattern.endswith(":*") and value.startswith(pattern[:-1]):
            return True
    return False


class AccessControl:
    """Origin/Host validation plus optional bearer auth."""

    def __init__(
        self,
        app,
        *,
        token: str = "",
        allowed_origins: Sequence[str] = (),
        allowed_hosts: Sequence[str] = (),
    ) -> None:
        self.app = app
        self.token = token
        self.allowed_origins = list(allowed_origins)
        self.allowed_hosts = list(allowed_hosts)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope["headers"]}

        origin = headers.get("origin")
        if origin and not _matches(origin, self.allowed_origins):
            await _reject(send, 403, "Origin not allowed")
            return

        host = headers.get("host")
        if self.allowed_hosts and not (host and _matches(host, self.allowed_hosts)):
            await _reject(send, 421, "Host not allowed")
            return

        if self.token and scope.get("path") not in _UNPROTECTED_PATHS:
            provided = headers.get("authorization", "")
            scheme, _, value = provided.partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(value.strip(), self.token):
                await _reject(send, 401, "Unauthorized", {"www-authenticate": "Bearer"})
                return

        await self.app(scope, receive, send)


async def _reject(send, status: int, message: str, extra: dict[str, str] | None = None) -> None:
    body = json.dumps({"error": message}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    for key, value in (extra or {}).items():
        headers.append((key.encode("latin-1"), value.encode("latin-1")))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
