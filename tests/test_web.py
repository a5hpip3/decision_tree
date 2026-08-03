"""decisiontree-web — session gate, allowlist, and the credential-bearing proxy.

The security property under test: the vault token lives only in this service.
It must never reach the browser, and the proxy must not become a general
credential-bearing tunnel into the vault API.
"""

from __future__ import annotations

import pytest

import app as web
from test_http import Server, run

BASE_ENV = {
    "AUTH0_ISSUER": "https://tenant.us.auth0.com/",
    "AUTH0_CLIENT_ID": "client-abc",
    "AUTH0_CLIENT_SECRET": "shh",
    "VAULT_API_URL": "https://vault.example.com",
    "VAULT_API_TOKEN": "vault-secret-token",
    "SESSION_SECRET": "session-secret",
    "WEB_ALLOWED_EMAILS": "ash@example.com, Other@Example.com",
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

    def test_allowlist_is_parsed_and_normalised(self):
        assert config().allowed_emails == ("ash@example.com", "other@example.com")

    def test_vault_token_is_not_required_config(self):
        """An open vault is a valid deployment; a missing session secret is not."""
        assert "VAULT_API_TOKEN" not in config(VAULT_API_TOKEN="").missing()
        assert "SESSION_SECRET" in config(SESSION_SECRET="").missing()


class TestAllowlist:
    def test_listed_address_allowed(self):
        assert web.is_allowed("ash@example.com", config().allowed_emails)

    def test_case_and_whitespace_insensitive(self):
        assert web.is_allowed("  ASH@Example.com ", config().allowed_emails)

    def test_unlisted_address_refused(self):
        assert not web.is_allowed("stranger@example.com", config().allowed_emails)

    def test_empty_allowlist_refuses_everyone(self):
        """Fail closed: an Auth0 tenant will let a stranger sign up."""
        assert not web.is_allowed("ash@example.com", ())

    def test_missing_email_refused(self):
        assert not web.is_allowed(None, config().allowed_emails)
        assert not web.is_allowed("", config().allowed_emails)


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

    def __init__(self, claims):
        self._claims = claims
        self.auth0 = self

    async def authorize_access_token(self, request):
        return {"userinfo": self._claims}


class TestCallbackEnforcesTheAllowlist:
    """The single most security-critical decision in this service: who gets in.

    Exercised through the real route rather than the helper, because the check
    being present in is_allowed() says nothing about the callback calling it.
    """

    def _callback(self, claims, **overrides):
        async def scenario():
            import httpx2

            application = web.build_app(config(**overrides), oauth=FakeAuth0(claims))
            async with Server(application) as srv:
                async with httpx2.AsyncClient(follow_redirects=False) as client:
                    started = await client.get(f"http://127.0.0.1:{srv.port}/auth/callback")
                    after = await client.get(
                        f"http://127.0.0.1:{srv.port}/whoami",
                        cookies=started.cookies,
                    )
                    return started, after

        return run(scenario())

    def test_allowed_email_gets_a_session(self):
        started, after = self._callback({"email": "ash@example.com", "name": "Ash"})
        assert started.status_code in (302, 307)
        assert started.headers["location"].endswith("/")
        assert after.status_code == 200
        assert after.json()["email"] == "ash@example.com"

    def test_stranger_is_refused_and_gets_no_session(self):
        started, after = self._callback({"email": "stranger@example.com"})
        assert started.status_code == 403
        assert after.status_code == 401, "a refused login must not leave a session"

    def test_refusal_names_the_identity_it_refused(self):
        """Otherwise a wrong account is indistinguishable from a missing entry."""
        started, _ = self._callback({"email": "stranger@example.com"})
        assert started.json()["signed_in_as"] == "stranger@example.com"

    def test_missing_email_is_refused(self):
        started, after = self._callback({"name": "No Email"})
        assert started.status_code == 403
        assert after.status_code == 401

    def test_empty_allowlist_refuses_a_valid_identity(self):
        started, after = self._callback(
            {"email": "ash@example.com"}, WEB_ALLOWED_EMAILS=""
        )
        assert started.status_code == 403
        assert after.status_code == 401

    def test_signed_in_user_can_reach_the_proxy(self):
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
        assert BASE_ENV["VAULT_API_TOKEN"] not in decoded
        assert BASE_ENV["AUTH0_CLIENT_SECRET"] not in decoded
