"""decisiontree-web — the front-end service.

Runs as its own Railway service because a volume mounts to exactly one
service, so this cannot read the vault files directly. It reads them through
the vault service's JSON API.

The shape that matters:

    browser --session cookie--> this service --bearer token--> vault API

The vault token lives only here. It is never sent to the browser, never
embedded in a page, and the proxy only forwards GETs to a fixed list of
paths — an open proxy holding a credential would be worse than no auth.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import httpx
from authlib.integrations.starlette_client import OAuth, OAuthError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

log = logging.getLogger("decisiontree.web")

STATIC_DIR = Path(__file__).parent / "static"

# Only these shapes are proxied. Anything else is refused rather than passed
# through, so a bug upstream can't turn this into a general credential-bearing
# proxy for the whole vault service.
ALLOWED_API_PATHS = (
    re.compile(r"^/api/projects$"),
    re.compile(r"^/api/projects/[a-z0-9][a-z0-9._-]{0,63}/decisions$"),
)


class Config:
    def __init__(self, env=None):
        env = env if env is not None else os.environ
        self.issuer = (env.get("AUTH0_ISSUER") or "").strip().rstrip("/")
        self.client_id = env.get("AUTH0_CLIENT_ID", "").strip()
        self.client_secret = env.get("AUTH0_CLIENT_SECRET", "").strip()
        self.vault_url = (env.get("VAULT_API_URL") or "").strip().rstrip("/")
        self.vault_token = env.get("VAULT_API_TOKEN", "").strip()
        self.session_secret = env.get("SESSION_SECRET", "").strip()
        # Secure cookies are correct in production, but a local run over
        # http:// would never keep a session, so this is opt-out by name.
        self.insecure_cookie = (
            env.get("SESSION_INSECURE_COOKIE", "").strip().lower() == "true"
        )
        self.allowed_emails = tuple(
            e.strip().lower()
            for e in (env.get("WEB_ALLOWED_EMAILS") or "").split(",")
            if e.strip()
        )

    @property
    def metadata_url(self) -> str:
        return f"{self.issuer}/.well-known/openid-configuration"

    def missing(self) -> list[str]:
        required = {
            "AUTH0_ISSUER": self.issuer,
            "AUTH0_CLIENT_ID": self.client_id,
            "AUTH0_CLIENT_SECRET": self.client_secret,
            "VAULT_API_URL": self.vault_url,
            "SESSION_SECRET": self.session_secret,
        }
        return sorted(name for name, value in required.items() if not value)


def is_allowed(email: str | None, allowed: tuple[str, ...]) -> bool:
    """Whether a signed-in identity may read the vaults.

    Fail closed when no allowlist is configured. An Auth0 tenant will happily
    let a stranger sign up through a social connection, and what is behind this
    door is decision reasoning and verbatim transcript excerpts — the content
    you would least want a stranger reading.
    """
    if not allowed:
        return False
    return bool(email) and email.strip().lower() in allowed


def api_path_allowed(path: str) -> bool:
    return any(pattern.match(path) for pattern in ALLOWED_API_PATHS)


def current_user(request) -> dict | None:
    return request.session.get("user")


def external_base(request) -> str:
    """The scheme://host a browser actually used to reach us.

    Railway terminates TLS, so the app sees plain http and would otherwise
    build an http:// callback URL — which Auth0 rejects as a mismatch against
    the registered https one, with an error that names neither cause.
    """
    headers = request.headers
    host = headers.get("x-forwarded-host") or headers.get("host") or request.url.netloc
    scheme = headers.get("x-forwarded-proto") or request.url.scheme
    return f"{scheme.split(',')[0].strip()}://{host}"


def callback_url(request) -> str:
    return f"{external_base(request)}/auth/callback"


def build_app(config: Config | None = None, oauth=None) -> Starlette:
    """Build the ASGI app.

    oauth is injectable so the callback — where the allowlist is actually
    enforced — can be exercised without standing up a real OIDC provider.
    """
    config = config or Config()

    if oauth is None:
        oauth = OAuth()
        if config.issuer and config.client_id:
            oauth.register(
                name="auth0",
                client_id=config.client_id,
                client_secret=config.client_secret,
                server_metadata_url=config.metadata_url,
                client_kwargs={"scope": "openid profile email"},
            )

    async def healthz(_request):
        return PlainTextResponse("ok")

    async def index(request):
        if current_user(request) is None:
            return RedirectResponse("/login")
        return FileResponse(STATIC_DIR / "index.html")

    async def whoami(request):
        user = current_user(request)
        if user is None:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        return JSONResponse({"email": user.get("email"), "name": user.get("name")})

    async def login(request):
        if config.missing():
            return JSONResponse(
                {"error": "not configured", "missing": config.missing()}, status_code=500
            )
        redirect_uri = callback_url(request)
        return await oauth.auth0.authorize_redirect(request, redirect_uri)

    async def callback(request):
        try:
            token = await oauth.auth0.authorize_access_token(request)
        except OAuthError as exc:
            log.warning("login: authorization failed: %s", exc)
            return JSONResponse({"error": "login failed", "detail": str(exc)}, status_code=400)

        claims = token.get("userinfo") or {}
        email = claims.get("email")
        if not is_allowed(email, config.allowed_emails):
            log.warning("login: rejected email=%r (not in WEB_ALLOWED_EMAILS)", email)
            # Name the identity that was refused: without it the operator
            # cannot tell a wrong account from a missing allowlist entry.
            return JSONResponse(
                {
                    "error": "not authorised",
                    "signed_in_as": email,
                    "detail": "Add this address to WEB_ALLOWED_EMAILS to grant access.",
                },
                status_code=403,
            )

        request.session["user"] = {"email": email, "name": claims.get("name"), "sub": claims.get("sub")}
        log.info("login: accepted %s", email)
        return RedirectResponse("/")

    async def logout(request):
        request.session.clear()
        if not config.issuer or not config.client_id:
            return RedirectResponse("/")
        returning = external_base(request)
        return RedirectResponse(
            f"{config.issuer}/v2/logout?client_id={config.client_id}&returnTo={returning}"
        )

    async def proxy(request):
        """Forward a read to the vault API, attaching the token server-side."""
        if current_user(request) is None:
            return JSONResponse({"error": "not signed in"}, status_code=401)

        path = request.url.path
        if not api_path_allowed(path):
            return JSONResponse({"error": "not found"}, status_code=404)

        url = f"{config.vault_url}{path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"
        headers = (
            {"Authorization": f"Bearer {config.vault_token}"} if config.vault_token else {}
        )
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                upstream = await client.get(url, headers=headers)
        except Exception as exc:  # noqa: BLE001 - upstream shape is not ours to assume
            log.warning("proxy: %s failed: %s", path, exc)
            return JSONResponse({"error": "vault unreachable"}, status_code=502)

        try:
            body = upstream.json()
        except ValueError:
            return JSONResponse({"error": "bad response from vault"}, status_code=502)
        return JSONResponse(body, status_code=upstream.status_code)

    routes = [
        Route("/healthz", healthz),
        Route("/login", login),
        Route("/auth/callback", callback, name="callback"),
        Route("/logout", logout),
        Route("/whoami", whoami),
        Route("/api/{rest:path}", proxy),
        Route("/", index),
        Mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static"),
    ]
    middleware = [
        Middleware(
            SessionMiddleware,
            secret_key=config.session_secret or "insecure-development-only",
            https_only=not config.insecure_cookie,
            same_site="lax",
        )
    ]
    return Starlette(routes=routes, middleware=middleware)


app = build_app()


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    config = Config()
    if config.missing():
        log.warning("starting with missing configuration: %s", ", ".join(config.missing()))
    if not config.allowed_emails:
        log.warning("WEB_ALLOWED_EMAILS is empty — every sign-in will be refused")
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        # Railway's edge is the only thing that can reach this container, and
        # uvicorn ignores X-Forwarded-* unless the peer is explicitly trusted.
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "*"),
    )
