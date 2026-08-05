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

import logging
import os
import re
import secrets

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

import api
import oauth
import server

API_PREFIX = "/api/"

log = logging.getLogger("context-vault.auth")


# A .mcp.json credential written as ${VAR} arrives verbatim when the variable
# is unset where the client runs — Claude Code loads the config anyway and
# sends the literal text.
UNEXPANDED = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def unexpanded_variable(credential: str) -> str | None:
    """The variable name, if this credential is an unexpanded placeholder."""
    match = UNEXPANDED.match(credential.strip())
    return match.group(1) if match else None


def placeholder_reason(name: str) -> str:
    return (
        f"the credential arrived as the literal text ${{{name}}}, so the "
        f"{name} environment variable is not set where the client runs and was "
        "never substituted. Set it in the shell that launches the client (a "
        "GUI-launched app does not read your shell profile) and reconnect."
    )


def token_fingerprint(token: str) -> str:
    """Enough to correlate a token across log lines, useless for replay."""
    import hashlib

    return hashlib.sha256(token.encode()).hexdigest()[:8]


def describe_token(token: str) -> str:
    """Unverified peek at a JWT's claims, for diagnostics only.

    Deliberately does not validate anything — the point is to explain *why*
    verification failed, which means reading what the token actually claims.
    """
    try:
        import jwt as _jwt

        claims = _jwt.decode(token, options={"verify_signature": False})
    except Exception:
        return "opaque-or-malformed (not a readable JWT)"
    return (
        f"iss={claims.get('iss')!r} aud={claims.get('aud')!r} "
        f"sub={claims.get('sub')!r} scope={claims.get('scope')!r}"
    )

MCP_SUFFIX = "/mcp"
PREFIX = "/p/"
# The project-less endpoint: one connector, project named per call.
ROUTER_PATH = "/mcp"


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


def resource_url_for(scope, project):
    """The canonical URI of the endpoint being addressed.

    The router has no project, so its resource is the bare /mcp URL — an
    OAuth audience still has to identify something."""
    if project is None:
        return f"{request_origin(scope)}{ROUTER_PATH}"
    return f"{request_origin(scope)}{PREFIX}{project}{MCP_SUFFIX}"


def metadata_url_for(scope, project):
    """Per-endpoint metadata location, mirroring the MCP path underneath it."""
    if project is None:
        return f"{request_origin(scope)}{WELL_KNOWN_PRM}{ROUTER_PATH}"
    return f"{request_origin(scope)}{WELL_KNOWN_PRM}{PREFIX}{project}{MCP_SUFFIX}"


async def health(_request):
    return PlainTextResponse("ok")


async def index(_request):
    return JSONResponse(
        {
            "service": "decisiontree",
            "connect": "/p/<project>/mcp",
            "router": "/mcp",
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

    async def authorize(scope, project):
        """(reason, identity). Reason is None if the request may proceed.

        The identity is whoever the token turned out to belong to, and is None
        whenever the request was let through without one — an open server, or
        the static shared token, which names a machine rather than a person.
        """
        if token is None and oauth_config is None:
            return None, None
        presented = _bearer(scope)
        if presented is None:
            log.warning("auth: no credential for project=%s", project)
            return "no bearer credential presented", None
        if token is not None and secrets.compare_digest(presented, token):
            return None, None
        placeholder = unexpanded_variable(presented)
        if placeholder:
            log.warning("auth: unexpanded %s for project=%s", placeholder, project)
            return placeholder_reason(placeholder), None
        if oauth_config is None:
            log.warning("auth: static token mismatch for project=%s", project)
            return "invalid token", None

        expected = resource_url_for(scope, project)
        try:
            claims = await verifier.verify(presented, expected)
        except oauth.AuthError as exc:
            # Log what the token claimed next to what we required — an audience
            # or issuer mismatch is otherwise indistinguishable from a bad
            # signature in the access log.
            log.warning(
                "auth: rejected token=%s project=%s reason=%s | expected aud=%r "
                "iss=%r | presented %s",
                token_fingerprint(presented),
                project,
                exc,
                oauth_config.audience or expected,
                oauth_config.issuer_url,
                describe_token(presented),
            )
            return str(exc), None
        # Claim names, not values: whether a tenant Action actually fired is
        # otherwise invisible from this side, and the failure mode is silent —
        # Auth0 drops a custom claim whose namespace is malformed rather than
        # erroring, so a missing email and a mistyped namespace look identical.
        log.info(
            "auth: accepted token=%s project=%s sub=%s claims=%s",
            token_fingerprint(presented),
            project,
            claims.get("sub"),
            ",".join(sorted(claims)),
        )
        return None, server.identity_from_claims(claims)

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

        if path.startswith(API_PREFIX):
            await _api_response(scope, receive, send, path, token, oauth_config, verifier)
            return

        router = path == ROUTER_PATH
        if not router and not path.startswith(PREFIX):
            await shell(scope, receive, send)
            return

        project = None if router else parse_project(path)
        if not router and project is None:
            await JSONResponse(
                {"error": "invalid project", "expected": "/p/<project>/mcp"},
                status_code=404,
            )(scope, receive, send)
            return

        reason, identity = await authorize(scope, project)
        if reason is not None:
            await deny(scope, receive, send, project, reason)
            return

        # Hand the MCP app the path it expects, and publish the project for the
        # duration of this request.
        inner = dict(scope, path=MCP_SUFFIX, raw_path=MCP_SUFFIX.encode())
        router_token = server.ROUTER.set(router)
        identity_token = server.IDENTITY.set(identity)
        project_token = None if router else server.REMOTE_PROJECT.set(project)
        try:
            await mcp_app(inner, receive, send)
        finally:
            server.ROUTER.reset(router_token)
            server.IDENTITY.reset(identity_token)
            if project_token is not None:
                server.REMOTE_PROJECT.reset(project_token)

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


async def _api_authorized(scope, token, oauth_config, verifier) -> str | None:
    """Auth for the read API. None if allowed, else a reason.

    The API is cross-project, so there is no per-project resource URL to check
    a JWT audience against. A JWT is therefore only accepted when a single
    fixed audience is configured; otherwise the static token is the way in,
    which is what the web service uses.
    """
    if token is None and oauth_config is None:
        return None
    presented = _bearer(scope)
    if presented is None:
        return "no bearer credential presented"
    if token is not None and secrets.compare_digest(presented, token):
        return None
    placeholder = unexpanded_variable(presented)
    if placeholder:
        return placeholder_reason(placeholder)
    if oauth_config is None or not oauth_config.audience:
        return "invalid token"
    try:
        await verifier.verify(presented, oauth_config.audience)
    except oauth.AuthError as exc:
        return str(exc)
    return None


async def _api_response(scope, receive, send, path, token, oauth_config, verifier):
    """Route and serve /api/... — read-only JSON over the hosted vaults."""
    if scope.get("method", "GET") != "GET":
        await JSONResponse({"error": "method not allowed"}, status_code=405)(
            scope, receive, send
        )
        return

    reason = await _api_authorized(scope, token, oauth_config, verifier)
    if reason is not None:
        log.warning("api: rejected path=%s reason=%s", path, reason)
        await JSONResponse(
            {"error": "unauthorized", "error_description": reason}, status_code=401
        )(scope, receive, send)
        return

    rest = path[len(API_PREFIX) :].strip("/")
    parts = rest.split("/") if rest else []

    if parts == ["projects"]:
        await JSONResponse(api.projects_payload())(scope, receive, send)
        return

    if len(parts) == 3 and parts[0] == "projects" and parts[2] == "decisions":
        name = parts[1]
        # Check existence first: server.connect() creates the file, so reading
        # an unknown project would otherwise conjure an empty vault for every
        # typo'd URL.
        if not api.exists(name):
            await JSONResponse({"error": "unknown project", "project": name}, status_code=404)(
                scope, receive, send
            )
            return
        query = scope.get("query_string", b"").decode()
        include_retired = "include_retired=true" in query
        await JSONResponse(api.decisions_payload(name, include_retired))(
            scope, receive, send
        )
        return

    await JSONResponse(
        {
            "error": "not found",
            "routes": ["/api/projects", "/api/projects/{name}/decisions"],
        },
        status_code=404,
    )(scope, receive, send)


async def _metadata_response(scope, receive, send, path, oauth_config):
    """Serve RFC 9728 metadata for /p/<project>/mcp, or 404 when OAuth is off."""
    if oauth_config is None:
        await JSONResponse({"error": "oauth not configured"}, status_code=404)(
            scope, receive, send
        )
        return

    suffix = path[len(WELL_KNOWN_PRM) :]
    # Claude probes the path-suffixed document first, then the bare one.
    router = suffix == ROUTER_PATH
    project = None if router else (parse_project(suffix) if suffix else None)
    if not router and project is None and suffix not in ("", "/"):
        await JSONResponse({"error": "unknown resource"}, status_code=404)(
            scope, receive, send
        )
        return

    if router:
        resource = f"{request_origin(scope)}{ROUTER_PATH}"
    elif project:
        resource = resource_url_for(scope, project)
    else:
        resource = f"{request_origin(scope)}{PREFIX}<project>{MCP_SUFFIX}"
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

    logging.basicConfig(
        level=os.environ.get("CONTEXT_VAULT_LOG_LEVEL", "INFO"),
        format="%(levelname)s %(name)s %(message)s",
    )

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
    )
