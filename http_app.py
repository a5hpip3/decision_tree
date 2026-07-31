"""HTTP transport for Context Vault — one connector URL per project.

Serving over HTTP means there is no project filesystem to inspect, so the
project comes from the URL instead:

    https://<host>/p/<project>/mcp

The project name is read off the path, validated, and published on the
REMOTE_PROJECT contextvar that server.db_path() reads. Verified by spike and by
tests/test_http.py: contextvars set here stay correct across concurrent
requests to different projects in stateless mode.

Stateless is deliberate. Every tool is an independent SQLite transaction, so
there is no session state worth keeping, and stateless avoids needing session
affinity if this ever runs on more than one replica.
"""

from __future__ import annotations

import os
import secrets

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

import server

MCP_SUFFIX = "/mcp"
PREFIX = "/p/"


def _unauthorized() -> JSONResponse:
    # Deliberately no WWW-Authenticate header. Claude reads `401` +
    # `WWW-Authenticate: Bearer` as an OAuth challenge and goes looking for
    # protected-resource metadata, probing /.well-known/oauth-protected-resource*
    # and failing with a misleading "Couldn't reach the MCP server". This server
    # authenticates with a static header (Anthropic's `static_headers` mode),
    # not OAuth, so a bare 401 gives an honest error instead.
    # https://claude.com/docs/connectors/building/authentication
    return JSONResponse(
        {"error": "unauthorized", "hint": "set the Authorization request header"},
        status_code=401,
    )


def parse_project(path: str) -> str | None:
    """Extract a valid project name from /p/<project>/mcp, else None.

    Returns None rather than coercing: the name becomes a filename, so an
    invalid one is rejected at the edge instead of being sanitised into some
    neighbouring project's vault.
    """
    if not path.startswith(PREFIX) or not path.endswith(MCP_SUFFIX):
        return None
    name = path[len(PREFIX) : -len(MCP_SUFFIX)]
    if not server.PROJECT_NAME.match(name):
        return None
    return name


async def health(_request):
    return PlainTextResponse("ok")


async def index(_request):
    return JSONResponse(
        {
            "service": "context-vault",
            "connect": "/p/<project>/mcp",
            "project_name": server.PROJECT_NAME.pattern,
        }
    )


def transport_security(allowed_hosts: str | None) -> TransportSecuritySettings | None:
    """DNS-rebinding protection tuned for wherever this is deployed.

    The SDK defaults to allowing only 127.0.0.1, so a hosted deployment answers
    every request with `421 Invalid Host header` until its own domain is
    allowed. Set CONTEXT_VAULT_ALLOWED_HOSTS to a comma-separated list of the
    hostnames it is served on, or to `*` to disable the check when a trusted
    proxy already terminates and validates the hostname.
    """
    if not allowed_hosts:
        return None
    hosts = [h.strip() for h in allowed_hosts.split(",") if h.strip()]
    if "*" in hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    # Ports are part of the Host header, so allow both forms.
    expanded = [h for host in hosts for h in (host, f"{host}:*")]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=expanded,
        allowed_origins=[f"https://{h}" for h in hosts] + [f"http://{h}" for h in hosts],
    )


def build_app(token: str | None = None, allowed_hosts: str | None = None):
    """ASGI app routing /p/<project>/mcp to the MCP server.

    token: if set, every /p/... request must present it as a bearer token.
    Health and index stay open so platform health checks work unauthenticated.
    allowed_hosts: see transport_security().
    """
    mcp_app = server.mcp.streamable_http_app(
        stateless_http=True,
        transport_security=transport_security(allowed_hosts),
    )

    routes = [Route("/", index), Route("/healthz", health)]
    shell = Starlette(routes=routes)

    async def app(scope, receive, send):
        if scope["type"] != "http":
            # Lifespan and anything else belongs to the MCP app, which owns the
            # session manager that needs starting.
            await mcp_app(scope, receive, send)
            return

        path = scope["path"]
        if not path.startswith(PREFIX):
            await shell(scope, receive, send)
            return

        project = parse_project(path)
        if project is None:
            await JSONResponse(
                {"error": "invalid project", "expected": "/p/<project>/mcp"},
                status_code=404,
            )(scope, receive, send)
            return

        if token is not None and not _authorized(scope, token):
            await _unauthorized()(scope, receive, send)
            return

        # Hand the MCP app the path it expects, and publish the project for the
        # duration of this request.
        inner = dict(scope, path=MCP_SUFFIX, raw_path=MCP_SUFFIX.encode())
        reset = server.REMOTE_PROJECT.set(project)
        try:
            await mcp_app(inner, receive, send)
        finally:
            server.REMOTE_PROJECT.reset(reset)

    return app


def _authorized(scope, token: str) -> bool:
    for key, value in scope.get("headers", []):
        if key == b"authorization":
            return secrets.compare_digest(value.strip(), f"Bearer {token}".encode())
    return False


app = build_app(
    token=os.environ.get("CONTEXT_VAULT_TOKEN") or None,
    allowed_hosts=os.environ.get("CONTEXT_VAULT_ALLOWED_HOSTS") or None,
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
    )
