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

import oauth
import server

MCP_SUFFIX = "/mcp"
PREFIX = "/p/"


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


WELL_KNOWN_PRM = "/.well-known/oauth-protected-resource"


def request_origin(scope) -> str:
    """The externally visible scheme://host, honouring the platform's proxy.

    Behind Railway the app speaks plain HTTP, so scope["scheme"] says `http`
    while clients reached us over HTTPS. Advertising an http:// resource URL
    would not match what the user entered, and Claude rejects the metadata.
    """
    headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
    host = headers.get("x-forwarded-host") or headers.get("host", "")
    scheme = headers.get("x-forwarded-proto") or scope.get("scheme", "http")
    return f"{scheme.split(',')[0].strip()}://{host}"


def resource_url_for(scope, project: str) -> str:
    return f"{request_origin(scope)}{PREFIX}{project}{MCP_SUFFIX}"


def metadata_url_for(scope, project: str) -> str:
    """Per-project metadata location, mirroring the MCP path underneath it."""
    return f"{request_origin(scope)}{WELL_KNOWN_PRM}{PREFIX}{project}{MCP_SUFFIX}"


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


def build_app(
    token: str | None = None,
    allowed_hosts: str | None = None,
    oauth_config: oauth.OAuthConfig | None = None,
    verifier: oauth.TokenVerifier | None = None,
):
    """ASGI app routing /p/<project>/mcp to the MCP server.

    token: if set, a request may authenticate with this static bearer token.
    oauth_config: if set, a request may instead present a JWT from that issuer,
        and the server advertises RFC 9728 metadata so clients can obtain one.
    Either credential is sufficient; with neither configured the server is open.
    Health, index and the metadata documents stay unauthenticated by design.
    """
    mcp_app = server.mcp.streamable_http_app(
        stateless_http=True,
        transport_security=transport_security(allowed_hosts),
    )
    if oauth_config and verifier is None:
        verifier = oauth.TokenVerifier(oauth_config)

    routes = [Route("/", index), Route("/healthz", health)]
    shell = Starlette(routes=routes)

    async def deny(scope, receive, send, project, error=None):
        headers = {}
        if oauth_config:
            headers["WWW-Authenticate"] = oauth.challenge_header(
                metadata_url_for(scope, project), error
            )
        # With only a static token there is no OAuth flow to start, so the
        # challenge header is omitted rather than sending clients hunting for
        # metadata that does not exist.
        await JSONResponse(
            {"error": "unauthorized", "error_description": error or "credential required"},
            status_code=401,
            headers=headers,
        )(scope, receive, send)

    async def authorize(scope, project) -> str | None:
        """None if the request may proceed, else a reason to reject it."""
        if token is None and oauth_config is None:
            return None
        presented = _bearer(scope)
        if presented is None:
            return "no bearer credential presented"
        if token is not None and secrets.compare_digest(presented, token):
            return None
        if oauth_config is None:
            return "invalid token"
        try:
            await verifier.verify(presented, resource_url_for(scope, project))
        except oauth.AuthError as exc:
            return str(exc)
        return None

    async def app(scope, receive, send):
        if scope["type"] != "http":
            # Lifespan and anything else belongs to the MCP app, which owns the
            # session manager that needs starting.
            await mcp_app(scope, receive, send)
            return

        path = scope["path"]

        if path.startswith(WELL_KNOWN_PRM):
            await _metadata_response(scope, receive, send, path, oauth_config)
            return

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

        reason = await authorize(scope, project)
        if reason is not None:
            await deny(scope, receive, send, project, reason)
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


def _bearer(scope) -> str | None:
    """The credential from an `Authorization: Bearer <value>` header."""
    for key, value in scope.get("headers", []):
        if key == b"authorization":
            raw = value.decode("latin-1").strip()
            scheme, _, rest = raw.partition(" ")
            if scheme.lower() == "bearer" and rest.strip():
                return rest.strip()
            return None
    return None


async def _metadata_response(scope, receive, send, path, oauth_config):
    """Serve RFC 9728 metadata for /p/<project>/mcp, or 404 when OAuth is off."""
    if oauth_config is None:
        await JSONResponse({"error": "oauth not configured"}, status_code=404)(
            scope, receive, send
        )
        return

    suffix = path[len(WELL_KNOWN_PRM) :]
    # Claude probes the path-suffixed document first, then the bare one.
    project = parse_project(suffix) if suffix else None
    if project is None and suffix not in ("", "/"):
        await JSONResponse({"error": "unknown resource"}, status_code=404)(
            scope, receive, send
        )
        return

    resource = (
        resource_url_for(scope, project)
        if project
        else f"{request_origin(scope)}{PREFIX}<project>{MCP_SUFFIX}"
    )
    await JSONResponse(oauth.protected_resource_metadata(resource, oauth_config))(
        scope, receive, send
    )


app = build_app(
    token=os.environ.get("CONTEXT_VAULT_TOKEN") or None,
    allowed_hosts=os.environ.get("CONTEXT_VAULT_ALLOWED_HOSTS") or None,
    oauth_config=oauth.OAuthConfig.from_env(os.environ),
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
    )
