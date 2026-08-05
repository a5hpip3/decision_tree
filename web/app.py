"""decisiontree-web — the front-end service.

Runs as its own Railway service because a volume mounts to exactly one
service, so this cannot read the vault files directly. It reads them through
the vault service's JSON API.

The shape that matters:

    browser --session cookie--> this service --the user's own token--> vault API

This service holds no credential of its own. At sign-in it asks Auth0 for an
access token scoped to the vault, keeps it in the session, and forwards that.
So the vault applies the same membership rules to a page view as it does to a
tool call, and someone who is not on a project cannot see it here either.

Deciding access here instead would mean a second rule to keep in step with
the first, and the first time they disagreed one of them would be wrong.

The proxy still only forwards GETs to a fixed list of paths: a general proxy
that attaches somebody's bearer token is worth attacking. The token itself is
encrypted before it goes into the session, because a Starlette session is
signed and not encrypted — see seal() below.
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
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
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

# An invite code, as it appears in a link: project, a colon, the secret.
INVITE_CODE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}:[A-Za-z0-9_-]{16,128}$")


class Config:
    def __init__(self, env=None):
        env = env if env is not None else os.environ
        self.issuer = (env.get("AUTH0_ISSUER") or "").strip().rstrip("/")
        self.client_id = env.get("AUTH0_CLIENT_ID", "").strip()
        self.client_secret = env.get("AUTH0_CLIENT_SECRET", "").strip()
        self.vault_url = (env.get("VAULT_API_URL") or "").strip().rstrip("/")
        self.session_secret = env.get("SESSION_SECRET", "").strip()
        # Secure cookies are correct in production, but a local run over
        # http:// would never keep a session, so this is opt-out by name.
        self.insecure_cookie = (
            env.get("SESSION_INSECURE_COOKIE", "").strip().lower() == "true"
        )

    @property
    def metadata_url(self) -> str:
        return f"{self.issuer}/.well-known/openid-configuration"

    @property
    def audience(self) -> str:
        """What to ask Auth0 for a token against.

        The vault's router URL. The read API is cross-project in the same way
        the router is — one credential, the project named per call — so a token
        good for one is the right reach for the other, and no separate Auth0
        API has to be registered for the front-end.
        """
        return f"{self.vault_url}/mcp"

    def missing(self) -> list[str]:
        required = {
            "AUTH0_ISSUER": self.issuer,
            "AUTH0_CLIENT_ID": self.client_id,
            "AUTH0_CLIENT_SECRET": self.client_secret,
            "VAULT_API_URL": self.vault_url,
            "SESSION_SECRET": self.session_secret,
        }
        return sorted(name for name, value in required.items() if not value)


def _cipher(secret: str):
    """Symmetric key for the session token, derived from SESSION_SECRET.

    Derived rather than configured separately: one secret to rotate, and
    rotating it invalidates the sessions holding tokens encrypted under it,
    which is the behaviour you want from rotating a session secret anyway.
    """
    import base64
    import hashlib

    from cryptography.fernet import Fernet

    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def seal(secret: str, value: str) -> str:
    """Encrypt the vault token for storage in the session cookie.

    A Starlette session is signed, not encrypted: the browser can read the
    payload. A bearer token sitting there in clear would be readable by
    anything that can read the cookie and usable directly against the vault —
    outside this proxy's GET-only path allowlist and for the token's whole
    lifetime, rather than only through the routes it is meant for.
    """
    return _cipher(secret).encrypt(value.encode()).decode()


def unseal(secret: str, value: str) -> str | None:
    from cryptography.fernet import InvalidToken

    try:
        return _cipher(secret).decrypt(value.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        # A session sealed under a previous SESSION_SECRET. Treated as no
        # token at all, which sends the caller back through Auth0.
        return None


def safe_destination(value: str | None) -> str:
    """Where to send somebody after sign-in, from a remembered value.

    Only ever a path on this service. What goes into the session today is built
    from a validated invite code, so nothing hostile can reach here — but a
    post-login redirect is the classic way to hand somebody a fresh session on
    an attacker's page, and the check costs a line.
    """
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


def invite_page(details: dict | None, user: dict | None, problem: str | None) -> str:
    """The one page an invited person sees, and the only instructions they get.

    Written as a single self-contained document rather than through the graph
    front-end: whoever is reading it has just arrived, may have made an account
    thirty seconds ago, and needs one thing — how to connect. Sending them to a
    dashboard first would bury it.
    """
    import html

    vault = html.escape(os.environ.get("VAULT_API_URL", "").strip().rstrip("/"))
    signed_in = html.escape((user or {}).get("email") or "")

    if problem or not details:
        headline, body = {
            "malformed": ("That link is not right",
                          "It looks incomplete — check you copied the whole thing."),
            "unreachable": ("Something is down",
                            "The vault could not be reached. Try again shortly."),
        }.get(problem, (
            "This invitation is not valid",
            "It may have expired, already been used, or been meant for a "
            f"different address. You are signed in as <strong>{signed_in}</strong> — "
            "if that is not the address it was sent to, sign out and try again. "
            "Otherwise ask whoever invited you for a new link.",
        ))
        return _shell(f"""
          <h1>{html.escape(headline)}</h1>
          <p class="lede">{body}</p>
          <p><a class="btn" href="/logout">Sign out</a></p>
        """)

    project = html.escape(details["project"])
    role = html.escape(details["role"])
    can_write = details["role"] in ("member", "owner")
    does = ("read the history, log decisions and supersede existing ones"
            if can_write else "read the full decision history")

    return _shell(f"""
      <h1>You have access to <span class="project">{project}</span></h1>
      <p class="lede">Signed in as <strong>{signed_in}</strong>, as
         <strong>{role}</strong> — you can {does}.</p>

      <p><a class="btn primary" href="/">Open the decision graph</a></p>

      <h2>Connect your own Claude</h2>
      <p>So decisions get logged where you are working, and carry your name.</p>

      <h3>Claude (chat)</h3>
      <ol>
        <li>Settings &rarr; Connectors &rarr; <strong>Add custom connector</strong></li>
        <li>URL: <code>{vault}/mcp</code></li>
        <li>Leave the OAuth fields empty and connect — you will be asked to sign in.</li>
      </ol>
      <p class="note">One connector covers every project you are on; name the
         project when you log a decision.</p>

      <h3>Claude Code</h3>
      <p>In the repo this project belongs to:</p>
      <pre><code>claude mcp add --transport http decisiontree \
  {vault}/p/{project}/mcp</code></pre>
      <p>Then restart Claude Code, run <code>/mcp</code>, and authenticate.</p>
      <p class="note"><strong>Do not add an Authorization header.</strong> A
         configured header stops the client attempting sign-in at all, and it
         fails with a 401 it cannot recover from.</p>
    """)


def _shell(body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DecisionTree — invitation</title>
<style>
  :root {{ color-scheme: light dark; --ink:#1C1917; --muted:#6b6259; --bg:#F4F0E8;
           --card:#FFFDF9; --line:#E2DACD; --accent:#A85C3A; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --ink:#EDE7DA; --muted:#8B8377; --bg:#15140F; --card:#221F19;
             --line:#302B23; --accent:#D08258; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
         font:16px/1.6 ui-serif, Georgia, serif; padding:6vh 5vw; }}
  main {{ max-width:44rem; margin:0 auto; background:var(--card);
          border:1px solid var(--line); border-radius:14px; padding:2.5rem; }}
  h1 {{ font-size:1.6rem; margin:0 0 .5rem; line-height:1.25; }}
  h2 {{ font-size:1.1rem; margin:2.2rem 0 .4rem;
        border-top:1px solid var(--line); padding-top:1.6rem; }}
  h3 {{ font-size:.78rem; letter-spacing:.14em; text-transform:uppercase;
        color:var(--muted); margin:1.5rem 0 .4rem; }}
  .project {{ color:var(--accent); }}
  .lede {{ color:var(--muted); margin:0 0 1.4rem; }}
  .note {{ color:var(--muted); font-size:.92rem; }}
  code {{ font:13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
          background:var(--bg); border:1px solid var(--line);
          border-radius:5px; padding:.1rem .35rem; }}
  pre {{ background:var(--bg); border:1px solid var(--line); border-radius:8px;
         padding:.9rem 1rem; overflow-x:auto; }}
  pre code {{ border:0; background:none; padding:0; }}
  ol {{ padding-left:1.2rem; }} li {{ margin:.25rem 0; }}
  .btn {{ display:inline-block; text-decoration:none; color:var(--ink);
          border:1px solid var(--line); border-radius:8px;
          padding:.5rem 1rem; font-size:.9rem; }}
  .btn.primary {{ background:var(--accent); border-color:var(--accent); color:#fff; }}
</style></head>
<body><main>{body}</main></body></html>"""


def api_path_allowed(path: str) -> bool:
    return any(pattern.match(path) for pattern in ALLOWED_API_PATHS)


def asset_version() -> str:
    """Short hash of the front-end bundle, used to bust browser caches."""
    import hashlib

    digest = hashlib.sha256()
    for name in ("app.js", "index.html"):
        path = STATIC_DIR / name
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


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
        # Stamp the asset URL with a hash of the file. Without this a deploy can
        # leave a browser running new JS against cached CSS, which looks like a
        # rendering bug rather than a stale cache.
        html = (STATIC_DIR / "index.html").read_text()
        html = html.replace("/static/app.js", f"/static/app.js?v={asset_version()}")
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    async def whoami(request):
        user = current_user(request)
        if user is None:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        return JSONResponse({"email": user.get("email"), "name": user.get("name")})

    async def invite(request):
        """Turn an invitation into a working connector.

        Being granted access is not the same as knowing you have it. Somebody
        added by address has no account, no idea a project exists, and nothing
        telling them what to install — so the grant sat there doing nothing
        until somebody explained it to them by hand.

        Signing in is required first, because the invitation belongs to an
        address and only the sign-in proves which one this is.
        """
        code = request.path_params["code"]
        if not INVITE_CODE.match(code):
            return HTMLResponse(invite_page(None, None, "malformed"), status_code=400)

        if current_user(request) is None:
            # Remembered across the round trip through Auth0, so a first-time
            # visitor lands back here rather than on an empty dashboard.
            request.session["after_login"] = f"/invite/{code}"
            return RedirectResponse("/login")

        sealed = request.session.get("vault_token")
        vault_token = unseal(config.session_secret, sealed) if sealed else None
        headers = {"Authorization": f"Bearer {vault_token}"} if vault_token else {}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                upstream = await client.get(
                    f"{config.vault_url}/api/invites/{code}", headers=headers
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("invite: %s failed: %s", code, exc)
            return HTMLResponse(invite_page(None, None, "unreachable"), status_code=502)

        user = current_user(request)
        if upstream.status_code != 200:
            return HTMLResponse(
                invite_page(None, user, "refused"), status_code=upstream.status_code
            )
        return HTMLResponse(invite_page(upstream.json(), user, None))

    async def login(request):
        if config.missing():
            return JSONResponse(
                {"error": "not configured", "missing": config.missing()}, status_code=500
            )
        redirect_uri = callback_url(request)
        # Ask for a token the vault will accept. Without an audience Auth0
        # returns an opaque token that is fine for /userinfo and useless as a
        # credential to another service.
        return await oauth.auth0.authorize_redirect(
            request, redirect_uri, audience=config.audience
        )

    async def callback(request):
        try:
            token = await oauth.auth0.authorize_access_token(request)
        except OAuthError as exc:
            log.warning("login: authorization failed: %s", exc)
            return JSONResponse({"error": "login failed", "detail": str(exc)}, status_code=400)

        claims = token.get("userinfo") or {}
        email = claims.get("email")
        access_token = token.get("access_token")
        if not access_token:
            # Signing in proves who someone is; it does not prove the vault
            # will talk to them. Without the token there is nothing to forward,
            # and a session that looks valid but can read nothing is worse than
            # a refused login.
            log.warning("login: no access token for %r (audience misconfigured?)", email)
            return JSONResponse(
                {
                    "error": "no vault access token",
                    "signed_in_as": email,
                    "detail": f"Auth0 returned no access token for {config.audience}.",
                },
                status_code=502,
            )

        request.session["user"] = {
            "email": email, "name": claims.get("name"), "sub": claims.get("sub")
        }
        request.session["vault_token"] = seal(config.session_secret, access_token)
        # Whether this person can see anything is the vault's answer, not ours.
        log.info("login: accepted %s", email)
        # Only ever a path on this service, so a crafted value cannot bounce
        # somebody to another host carrying a fresh session.
        return RedirectResponse(safe_destination(request.session.pop("after_login", None)))

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
        sealed = request.session.get("vault_token")
        vault_token = unseal(config.session_secret, sealed) if sealed else None
        if sealed and vault_token is None:
            request.session.clear()
            return JSONResponse({"error": "session expired"}, status_code=401)
        headers = {"Authorization": f"Bearer {vault_token}"} if vault_token else {}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                upstream = await client.get(url, headers=headers)
        except Exception as exc:  # noqa: BLE001 - upstream shape is not ours to assume
            log.warning("proxy: %s failed: %s", path, exc)
            return JSONResponse({"error": "vault unreachable"}, status_code=502)

        if upstream.status_code == 401:
            # The forwarded token has expired. Drop the session so the next
            # page load goes back through Auth0 rather than looping on 401s
            # with a cookie that looks signed in.
            request.session.clear()
            return JSONResponse({"error": "session expired"}, status_code=401)

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
        Route("/invite/{code}", invite),
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
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        # Railway's edge is the only thing that can reach this container, and
        # uvicorn ignores X-Forwarded-* unless the peer is explicitly trusted.
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "*"),
    )
