"""decisiontree-web — the session gate and the token-forwarding proxy.

The security property under test: this service holds no credential of its own
and decides nothing about who may see what. It forwards the signed-in
person's own token, so the vault applies the same membership to a page view
as to a tool call. The proxy must still not become a general tunnel — it
carries somebody's bearer token, which is worth attacking.
"""

from __future__ import annotations

import pathlib

import pytest

from starlette.responses import JSONResponse

import app as web
from test_http import Server, run

BASE_ENV = {
    "AUTH0_ISSUER": "https://tenant.us.auth0.com/",
    "AUTH0_CLIENT_ID": "client-abc",
    "AUTH0_CLIENT_SECRET": "shh",
    "VAULT_API_URL": "https://vault.example.com",
    "SESSION_SECRET": "session-secret",
    # Tests speak http, and a Secure cookie is never sent back over http.
    "SESSION_INSECURE_COOKIE": "true",
}


def config(**overrides):
    env = dict(BASE_ENV)
    env.update(overrides)
    return web.Config(env)


class TestConfig:
    def test_reads_the_environment(self):
        c = config()
        assert c.issuer == "https://tenant.us.auth0.com"
        assert c.vault_url == "https://vault.example.com"
        assert c.metadata_url.endswith("/.well-known/openid-configuration")

    def test_reports_what_is_missing(self):
        assert config(AUTH0_CLIENT_ID="").missing() == ["AUTH0_CLIENT_ID"]
        assert config().missing() == []

    def test_audience_is_the_vaults_router(self):
        """One credential covers the whole read API, as it does the router."""
        assert config().audience == "https://vault.example.com/mcp"

    def test_session_secret_is_required(self):
        assert "SESSION_SECRET" in config(SESSION_SECRET="").missing()


class TestNoLocalAllowlist:
    """Access is not this service's decision any more.

    It used to keep its own list of addresses, which meant two rules for one
    question — and the first time they disagreed, one of them was wrong.
    """

    def test_the_allowlist_is_gone(self):
        assert not hasattr(web, "is_allowed")
        assert not hasattr(config(), "allowed_emails")

    def test_it_holds_no_vault_credential(self):
        assert not hasattr(config(), "vault_token")
        assert "WEB_ALLOWED_EMAILS" not in pathlib.Path(web.__file__).read_text()


class TestProxyPathAllowlist:
    @pytest.mark.parametrize(
        "path",
        ["/api/projects", "/api/projects/hopscotch/decisions", "/api/projects/rapid_manufacturing/decisions"],
    )
    def test_allows_the_two_read_routes(self, path):
        assert web.api_path_allowed(path)

    @pytest.mark.parametrize(
        "path",
        [
            "/api/projects/../../secret",
            "/api/projects/UPPER/decisions",
            "/api/projects/x/decisions/extra",
            "/api/anything",
            "/api/projects/x/write",
            "/healthz",
            "/api/projects/",
        ],
    )
    def test_refuses_everything_else(self, path):
        """Refusing beats forwarding: this proxy carries a credential."""
        assert not web.api_path_allowed(path)


class TestOverHttp:
    def _get(self, path, **overrides):
        async def scenario():
            import httpx2

            async with Server(web.build_app(config(**overrides))) as srv:
                async with httpx2.AsyncClient(follow_redirects=False) as client:
                    return await client.get(f"http://127.0.0.1:{srv.port}{path}")

        return run(scenario())

    def test_health_is_open(self):
        response = self._get("/healthz")
        assert response.status_code == 200 and response.text == "ok"

    def test_index_redirects_when_signed_out(self):
        response = self._get("/")
        assert response.status_code in (302, 307)
        assert response.headers["location"].endswith("/login")

    def test_api_requires_a_session(self):
        response = self._get("/api/projects")
        assert response.status_code == 401

    def test_api_does_not_leak_the_vault_token_when_signed_out(self):
        """The most important assertion here."""
        response = self._get("/api/projects")
        assert "vault-secret-token" not in response.text
        assert "vault-secret-token" not in str(response.headers)

    def test_whoami_requires_a_session(self):
        assert self._get("/whoami").status_code == 401

    def test_login_reports_missing_configuration(self):
        response = self._get("/login", AUTH0_CLIENT_ID="")
        assert response.status_code == 500
        assert "AUTH0_CLIENT_ID" in response.text

    def test_disallowed_api_path_is_refused_before_any_upstream_call(self):
        """Unreachable upstream would give 502; a refused path must give 404."""
        response = self._get("/api/projects/x/write", VAULT_API_URL="http://127.0.0.1:9")
        assert response.status_code in (401, 404)
        assert response.status_code != 502


class FakeAuth0:
    """Stands in for Authlib's client so the callback path can be exercised.

    Only authorize_access_token is used by the callback; the redirect half of
    the flow belongs to Authlib and is not ours to re-test.
    """

    def __init__(self, claims, access_token="vault-access-token"):
        self._claims = claims
        self._access_token = access_token
        self.auth0 = self

    async def authorize_access_token(self, request):
        token = {"userinfo": self._claims}
        if self._access_token is not None:
            token["access_token"] = self._access_token
        return token


class TestCallback:
    """Signing in has to produce something the vault will accept.

    Proving who somebody is does not prove the vault will talk to them. A
    session that looks valid but can read nothing is worse than a refused
    login, because the failure surfaces later and somewhere else.
    """

    def _callback(self, claims, access_token="vault-access-token", **overrides):
        async def scenario():
            import httpx2

            application = web.build_app(
                config(**overrides), oauth=FakeAuth0(claims, access_token)
            )
            async with Server(application) as srv:
                async with httpx2.AsyncClient(follow_redirects=False) as client:
                    started = await client.get(f"http://127.0.0.1:{srv.port}/auth/callback")
                    after = await client.get(
                        f"http://127.0.0.1:{srv.port}/whoami",
                        cookies=started.cookies,
                    )
                    return started, after

        return run(scenario())

    def test_sign_in_creates_a_session(self):
        started, after = self._callback({"email": "ash@example.com", "name": "Ash"})
        assert started.status_code in (302, 307)
        assert started.headers["location"].endswith("/")
        assert after.json()["email"] == "ash@example.com"

    def test_no_access_token_means_no_session(self):
        """Auth0 returns none when the audience is wrong or unregistered."""
        started, after = self._callback({"email": "ash@example.com"}, access_token=None)
        assert started.status_code == 502
        assert after.status_code == 401, "a failed login must not leave a session"

    def test_the_failure_names_the_audience_it_asked_for(self):
        started, _ = self._callback({"email": "ash@example.com"}, access_token=None)
        assert config().audience in started.json()["detail"]

    def test_a_stranger_is_let_in_and_simply_sees_nothing(self):
        """Not this service's call. The vault answers it, per project.

        Anyone in the Auth0 tenant can hold a session here; membership is what
        decides whether any project is visible through it.
        """
        started, after = self._callback({"email": "stranger@example.com"})
        assert started.status_code in (302, 307)
        assert after.json()["email"] == "stranger@example.com"

    def test_the_session_carries_the_users_token_to_the_vault(self):
        """Ties the session to the thing it guards."""

        async def scenario():
            import httpx2

            application = web.build_app(
                config(VAULT_API_URL="http://127.0.0.1:9"),
                oauth=FakeAuth0({"email": "ash@example.com"}),
            )
            async with Server(application) as srv:
                async with httpx2.AsyncClient(follow_redirects=False) as client:
                    started = await client.get(f"http://127.0.0.1:{srv.port}/auth/callback")
                    return await client.get(
                        f"http://127.0.0.1:{srv.port}/api/projects", cookies=started.cookies
                    )

        # Upstream is deliberately unreachable: 502 proves the request got past
        # the session gate and was actually forwarded.
        assert run(scenario()).status_code == 502


class TestProxyForwarding:
    """What actually reaches the vault.

    Asserting the proxy returns 502 against a dead upstream proves the request
    left the building; it says nothing about what was on it. These stand a
    real upstream up and look at the request.
    """

    def _through_proxy(self, handler, **overrides):
        from starlette.routing import Route

        async def scenario():
            import httpx2
            from starlette.applications import Starlette

            upstream = Starlette(routes=[Route("/api/{rest:path}", handler)])
            async with Server(upstream) as vault_srv:
                application = web.build_app(
                    config(VAULT_API_URL=f"http://127.0.0.1:{vault_srv.port}", **overrides),
                    oauth=FakeAuth0({"email": "ash@example.com"}),
                )
                async with Server(application) as srv:
                    # One client with its own cookie jar, so a cleared session
                    # is actually cleared here too. Passing cookies per request
                    # would keep handing back the stale one and the test would
                    # pass whatever the service did.
                    async with httpx2.AsyncClient(follow_redirects=False) as client:
                        base = f"http://127.0.0.1:{srv.port}"
                        await client.get(f"{base}/auth/callback")
                        proxied = await client.get(f"{base}/api/projects")
                        after = await client.get(f"{base}/whoami")
                        return proxied, after

        return run(scenario())

    def test_the_users_own_token_is_forwarded(self):
        seen = {}

        async def handler(request):
            seen["authorization"] = request.headers.get("authorization")
            return JSONResponse({"projects": []})

        proxied, _ = self._through_proxy(handler)
        assert proxied.status_code == 200
        assert seen["authorization"] == "Bearer vault-access-token"

    def test_an_expired_token_ends_the_session(self):
        """Otherwise the browser loops on 401s holding a cookie that looks fine."""

        async def handler(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        proxied, after = self._through_proxy(handler)
        assert proxied.status_code == 401
        assert after.status_code == 401, "the stale session must be dropped"


class TestCookieSecurity:
    def test_secure_cookie_is_the_default(self):
        """Only an explicit opt-out drops the Secure flag."""
        assert web.Config({}).insecure_cookie is False
        assert web.Config({"SESSION_INSECURE_COOKIE": "false"}).insecure_cookie is False
        assert web.Config({"SESSION_INSECURE_COOKIE": "true"}).insecure_cookie is True

    def test_production_config_sets_secure_and_httponly(self):
        async def scenario():
            import httpx2

            application = web.build_app(
                config(SESSION_INSECURE_COOKIE="false"),
                oauth=FakeAuth0({"email": "ash@example.com"}),
            )
            async with Server(application) as srv:
                async with httpx2.AsyncClient(follow_redirects=False) as client:
                    return await client.get(f"http://127.0.0.1:{srv.port}/auth/callback")

        header = run(scenario()).headers.get("set-cookie", "").lower()
        assert "secure" in header
        assert "httponly" in header
        assert "samesite=lax" in header

    def test_session_carries_no_secret(self):
        """Starlette sessions are signed, not encrypted — the browser can read
        the payload. It must therefore hold identity only, never the vault
        token."""

        async def scenario():
            import httpx2

            application = web.build_app(
                config(), oauth=FakeAuth0({"email": "ash@example.com"})
            )
            async with Server(application) as srv:
                async with httpx2.AsyncClient(follow_redirects=False) as client:
                    return await client.get(f"http://127.0.0.1:{srv.port}/auth/callback")

        import base64

        cookie = run(scenario()).headers.get("set-cookie", "")
        payload = cookie.split("=", 1)[1].split(".")[0]
        decoded = base64.b64decode(payload + "==").decode("utf-8", "replace")
        assert "ash@example.com" in decoded
        assert "vault-access-token" not in decoded
        assert BASE_ENV["AUTH0_CLIENT_SECRET"] not in decoded

    def test_the_sealed_token_still_round_trips(self):
        """Unreadable to the browser, still usable by this service."""
        secret = BASE_ENV["SESSION_SECRET"]
        assert web.unseal(secret, web.seal(secret, "abc.def.ghi")) == "abc.def.ghi"

    def test_a_token_sealed_under_another_secret_is_refused(self):
        assert web.unseal("rotated", web.seal("original", "abc.def.ghi")) is None

    def test_the_cookie_stays_within_the_browser_limit(self):
        """A session too big to set is a login that silently never completes."""

        async def scenario():
            import httpx2

            application = web.build_app(
                config(),
                oauth=FakeAuth0({"email": "ash@example.com"}, "x" * 1200),
            )
            async with Server(application) as srv:
                async with httpx2.AsyncClient(follow_redirects=False) as client:
                    return await client.get(f"http://127.0.0.1:{srv.port}/auth/callback")

        assert len(run(scenario()).headers.get("set-cookie", "")) < 4096


class TestForwardedHeaders:
    """Railway terminates TLS, so the app sees http. Every externally visible
    URL it builds must still be the https one the browser used — Auth0 rejects
    a callback that doesn't match what was registered."""

    def _request(self, headers):
        class FakeURL:
            scheme = "http"
            netloc = "internal:8000"

        class FakeRequest:
            def __init__(self, headers):
                self.headers = headers
                self.url = FakeURL()

        return FakeRequest(headers)

    def test_uses_forwarded_proto_and_host(self):
        request = self._request(
            {
                "host": "internal:8000",
                "x-forwarded-proto": "https",
                "x-forwarded-host": "web.example.com",
            }
        )
        assert web.external_base(request) == "https://web.example.com"
        assert web.callback_url(request) == "https://web.example.com/auth/callback"

    def test_callback_is_https_behind_the_proxy(self):
        """The exact bug seen in production: an http:// redirect_uri."""
        request = self._request({"host": "web.example.com", "x-forwarded-proto": "https"})
        assert web.callback_url(request).startswith("https://")

    def test_falls_back_to_the_host_header(self):
        request = self._request({"host": "127.0.0.1:8000"})
        assert web.external_base(request) == "http://127.0.0.1:8000"

    def test_takes_the_first_proto_when_chained(self):
        request = self._request({"host": "web.example.com", "x-forwarded-proto": "https,http"})
        assert web.external_base(request) == "https://web.example.com"


class TestAssetCacheBusting:
    """New JS against cached CSS renders as a layout bug, not a cache problem —
    it cost a debugging cycle once, so the asset URL carries a content hash."""

    def test_version_changes_with_the_bundle(self, tmp_path, monkeypatch):
        import app as web_module

        monkeypatch.setattr(web_module, "STATIC_DIR", tmp_path)
        (tmp_path / "app.js").write_text("const a = 1;")
        (tmp_path / "index.html").write_text("<html></html>")
        first = web_module.asset_version()

        (tmp_path / "app.js").write_text("const a = 2;")
        assert web_module.asset_version() != first

    def test_version_is_stable_for_unchanged_files(self, tmp_path, monkeypatch):
        import app as web_module

        monkeypatch.setattr(web_module, "STATIC_DIR", tmp_path)
        (tmp_path / "app.js").write_text("const a = 1;")
        (tmp_path / "index.html").write_text("<html></html>")
        assert web_module.asset_version() == web_module.asset_version()

    def test_css_changes_also_bust_the_cache(self, tmp_path, monkeypatch):
        """The styles live in index.html, so a CSS-only edit must bump it too."""
        import app as web_module

        monkeypatch.setattr(web_module, "STATIC_DIR", tmp_path)
        (tmp_path / "app.js").write_text("const a = 1;")
        (tmp_path / "index.html").write_text("<style>.a{}</style>")
        first = web_module.asset_version()
        (tmp_path / "index.html").write_text("<style>.a{color:red}</style>")
        assert web_module.asset_version() != first


class TestInvitePage:
    """The page that turns an invitation into a working connector.

    Being granted access is not the same as knowing you have it. Somebody
    invited by address has no account, no idea the project exists and nothing
    telling them what to install, so the grant sat there doing nothing until
    someone explained it by hand.
    """

    CODE = "acme:" + "a" * 32

    def _visit(self, code, handler=None, signed_in=True, **overrides):
        from starlette.routing import Route

        async def default(request):
            return JSONResponse(
                {"project": "acme", "role": "member", "member": True, "spent": False}
            )

        async def scenario():
            import httpx2
            from starlette.applications import Starlette

            upstream = Starlette(routes=[Route("/api/{rest:path}", handler or default)])
            async with Server(upstream) as vault_srv:
                application = web.build_app(
                    config(VAULT_API_URL=f"http://127.0.0.1:{vault_srv.port}", **overrides),
                    oauth=FakeAuth0({"email": "ash@example.com"}),
                )
                async with Server(application) as srv:
                    async with httpx2.AsyncClient(follow_redirects=False) as client:
                        base = f"http://127.0.0.1:{srv.port}"
                        if signed_in:
                            await client.get(f"{base}/auth/callback")
                        return await client.get(f"{base}/invite/{code}")

        return run(scenario())

    def test_a_signed_out_visitor_is_sent_to_sign_in(self):
        """They may not have an account at all — that is the normal case."""
        response = self._visit(self.CODE, signed_in=False)
        assert response.status_code in (302, 307)
        assert response.headers["location"] == "/login"

    def test_and_comes_back_to_the_invitation_afterwards(self):
        """Landing on an empty dashboard would lose the instructions entirely."""
        from starlette.routing import Route

        async def handler(request):
            return JSONResponse(
                {"project": "acme", "role": "member", "member": True, "spent": False}
            )

        async def scenario():
            import httpx2
            from starlette.applications import Starlette

            upstream = Starlette(routes=[Route("/api/{rest:path}", handler)])
            async with Server(upstream) as vault_srv:
                application = web.build_app(
                    config(VAULT_API_URL=f"http://127.0.0.1:{vault_srv.port}"),
                    oauth=FakeAuth0({"email": "ash@example.com"}),
                )
                async with Server(application) as srv:
                    async with httpx2.AsyncClient(follow_redirects=False) as client:
                        base = f"http://127.0.0.1:{srv.port}"
                        await client.get(f"{base}/invite/{self.CODE}")   # remembers
                        return await client.get(f"{base}/auth/callback")

        assert run(scenario()).headers["location"] == f"/invite/{self.CODE}"

    def test_it_names_the_project_and_the_role(self):
        body = self._visit(self.CODE).text
        assert "acme" in body and "member" in body

    def test_it_gives_both_sets_of_connection_instructions(self):
        body = self._visit(self.CODE).text
        assert "claude mcp add" in body
        assert "/p/acme/mcp" in body           # Claude Code, pinned
        assert "/mcp" in body                  # chat, via the router

    def test_it_warns_off_the_header_that_breaks_sign_in(self):
        assert "Authorization header" in self._visit(self.CODE).text

    def test_a_refused_invitation_says_so_without_saying_whose(self):
        async def handler(request):
            return JSONResponse({"error": "invalid invite"}, status_code=404)

        response = self._visit(self.CODE, handler)
        assert response.status_code == 404
        assert "not valid" in response.text
        # The address it was meant for is exactly what must not leak.
        assert "example.invalid" not in response.text

    def test_a_malformed_code_never_reaches_the_vault(self):
        async def handler(request):          # noqa: ARG001
            raise AssertionError("should not have been called")

        assert self._visit("not-a-code", handler).status_code == 400

    def test_an_unreachable_vault_is_not_a_stack_trace(self):
        async def scenario():
            import httpx2

            application = web.build_app(
                config(VAULT_API_URL="http://127.0.0.1:9"),
                oauth=FakeAuth0({"email": "ash@example.com"}),
            )
            async with Server(application) as srv:
                async with httpx2.AsyncClient(follow_redirects=False) as client:
                    base = f"http://127.0.0.1:{srv.port}"
                    await client.get(f"{base}/auth/callback")
                    return await client.get(f"{base}/invite/{self.CODE}")

        response = run(scenario())
        assert response.status_code == 502
        assert "Something is down" in response.text

    def test_a_viewer_is_told_what_a_viewer_can_do(self):
        async def handler(request):
            return JSONResponse(
                {"project": "acme", "role": "viewer", "member": True, "spent": False}
            )

        body = self._visit(self.CODE, handler).text
        assert "read the full decision history" in body
        assert "log decisions" not in body


class TestOpenRedirect:
    """`after_login` comes off a URL, so it is somebody else's input."""

    def _callback_with(self, destination):
        async def scenario():
            import httpx2

            application = web.build_app(
                config(), oauth=FakeAuth0({"email": "ash@example.com"})
            )
            async with Server(application) as srv:
                async with httpx2.AsyncClient(follow_redirects=False) as client:
                    base = f"http://127.0.0.1:{srv.port}"
                    await client.get(f"{base}/invite/{destination}")
                    return await client.get(f"{base}/auth/callback")

        return run(scenario())

    @pytest.mark.parametrize("code", ["acme:" + "b" * 20])
    def test_a_valid_code_round_trips(self, code):
        assert self._callback_with(code).headers["location"] == f"/invite/{code}"

    def test_a_rejected_code_leaves_no_destination_behind(self):
        """A malformed code is refused before anything is remembered."""
        assert self._callback_with("//evil.example.com").headers["location"] == "/"

    @pytest.mark.parametrize("hostile", [
        "//evil.example.com",
        "https://evil.example.com",
        "http://evil.example.com/x",
        "///evil.example.com",
        "", None,
    ])
    def test_only_a_path_on_this_service_is_honoured(self, hostile):
        """The check itself, not just the one caller that happens to feed it."""
        assert web.safe_destination(hostile) == "/"

    def test_an_ordinary_path_is_kept(self):
        assert web.safe_destination("/invite/acme:abc") == "/invite/acme:abc"
