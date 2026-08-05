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


def canonical_resource(scope) -> str:
    """The one resource identifier every endpoint advertises: the origin.

    Two rules have to hold at once, and only this satisfies both.

    RFC 8707 makes the resource an audience the authorization server has to
    already recognise, and Auth0 wants each registered as an API. A URL per
    project therefore meant an API per project — which a teammate starting one
    cannot register in someone else's tenant, and which fails outright with
    "Service not found" until an admin does.

    RFC 9728 then has the client check what is advertised against where it
    connected. The MCP SDK accepts the exact URL or its origin, and nothing
    else — so a shared identifier with any path on it is refused by the client
    even when the authorization server is happy with it.

    The origin is the only value that is both shared across every project and
    acceptable to a client connecting to one. One API to register, once.

    The per-project audience was load-bearing when holding a token was the same
    as being allowed in — it was what kept a token for one project out of
    another. Membership does that now, on every call, so scoping the audience
    as well was buying isolation that already existed.
    """
    return request_origin(scope)


def acceptable_resources(scope, project) -> list:
    """Audiences a token may name.

    Both spellings of the origin. A client turns the advertised origin into a
    URL before sending it back, and serialising a URL with an empty path adds
    the trailing slash — so what arrives is `https://host/` even though
    `https://host` was advertised. Auth0 issues the token against whichever
    string it has registered, so the audience can be either and the difference
    is not ours to impose on anyone.

    Plus the two identifiers this server advertised before: tokens already
    issued against those keep working rather than everybody being signed out
    by a deploy.
    """
    origin = request_origin(scope)
    urls = [origin, f"{origin}/", f"{origin}{ROUTER_PATH}"]
    if project is not None:
        urls.append(resource_url_for(scope, project))
    return urls


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
    allowed_hosts: str | None = None,
    oauth_config: oauth.OAuthConfig | None = None,
    verifier: oauth.TokenVerifier | None = None,
):
    """ASGI app routing /p/<project>/mcp to the MCP server.

    oauth_config: if set, a request must present a JWT from that issuer, and
        the server advertises RFC 9728 metadata so clients can obtain one.
        With it unset the server is open, which is only for local runs.

    There is deliberately no shared-secret option. One token that opens every
    project cannot say who is calling, so nothing it does can be attributed and
    revoking one holder revokes all of them — which is the whole of what made
    this single-user. Health, index and the metadata documents stay
    unauthenticated by design.
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

        The identity is whoever the token turned out to belong to. It is None
        only on an unconfigured server, which has no issuer to verify against.
        """
        if oauth_config is None:
            return None, None
        presented = _bearer(scope)
        if presented is None:
            log.warning("auth: no credential for project=%s", project)
            return "no bearer credential presented", None
        placeholder = unexpanded_variable(presented)
        if placeholder:
            log.warning("auth: unexpanded %s for project=%s", placeholder, project)
            return placeholder_reason(placeholder), None

        expected = acceptable_resources(scope, project)
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
            await _api_response(scope, receive, send, path, oauth_config, verifier)
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
        verifies_token = server.VERIFIES.set(oauth_config is not None)
        project_token = None if router else server.REMOTE_PROJECT.set(project)
        try:
            await mcp_app(inner, receive, send)
        finally:
            server.ROUTER.reset(router_token)
            server.IDENTITY.reset(identity_token)
            server.VERIFIES.reset(verifies_token)
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


async def _api_authorized(scope, oauth_config, verifier):
    """(reason, identity) for the read API. Reason is None if allowed.

    The audience checked is the router's own URL. The API is cross-project in
    the same way the router is — one credential, the project named per call —
    so a token good for one is exactly the right reach for the other, and no
    second Auth0 API has to exist for the front-end to use.

    The web service forwards the signed-in person's token rather than one of
    its own, which is what lets this apply the same membership rules the MCP
    tools do instead of a second allowlist that can drift from them.
    """
    if oauth_config is None:
        return None, None
    presented = _bearer(scope)
    if presented is None:
        return "no bearer credential presented", None
    placeholder = unexpanded_variable(presented)
    if placeholder:
        return placeholder_reason(placeholder), None
    expected = acceptable_resources(scope, None)
    try:
        claims = await verifier.verify(presented, expected)
    except oauth.AuthError as exc:
        return str(exc), None
    return None, server.identity_from_claims(claims)


async def _api_response(scope, receive, send, path, oauth_config, verifier):
    """Route and serve /api/... — read-only JSON over the hosted vaults."""
    if scope.get("method", "GET") != "GET":
        await JSONResponse({"error": "method not allowed"}, status_code=405)(
            scope, receive, send
        )
        return

    reason, identity = await _api_authorized(scope, oauth_config, verifier)
    if reason is not None:
        log.warning("api: rejected path=%s reason=%s", path, reason)
        await JSONResponse(
            {"error": "unauthorized", "error_description": reason}, status_code=401
        )(scope, receive, send)
        return

    identity_token = server.IDENTITY.set(identity)
    verifies_token = server.VERIFIES.set(oauth_config is not None)
    try:
        await _api_route(scope, receive, send, path)
    finally:
        server.IDENTITY.reset(identity_token)
        server.VERIFIES.reset(verifies_token)


async def _api_route(scope, receive, send, path):
    """Serve one /api/... path as whoever the token belongs to."""
    rest = path[len(API_PREFIX) :].strip("/")
    parts = rest.split("/") if rest else []

    if parts == ["projects"]:
        await JSONResponse(api.projects_payload())(scope, receive, send)
        return

    if len(parts) == 2 and parts[0] == "invites":
        # The invited person's own view of their invitation, for the page that
        # walks them from a link to a working connector.
        identity = server.IDENTITY.get()
        details = server.invite_details(parts[1], identity) if identity else None
        if details is None:
            await JSONResponse({"error": "invalid invite"}, status_code=404)(
                scope, receive, send
            )
            return
        await JSONResponse(details)(scope, receive, send)
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
            "routes": [
                "/api/projects",
                "/api/projects/{name}/decisions",
                "/api/invites/{code}",
            ],
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

    # Advertised for every endpoint, project-pinned or not: it is what the
    # client will send as `resource`, and it has to be something the
    # authorization server has been told about exactly once.
    resource = canonical_resource(scope)
    await JSONResponse(oauth.protected_resource_metadata(resource, oauth_config))(
        scope, receive, send
    )


app = build_app(
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
