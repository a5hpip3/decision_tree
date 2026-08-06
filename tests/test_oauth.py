"""OAuth resource-server tests.

Tokens here are real RS256 JWTs signed with a throwaway key pair and served
through a stub JWKS client, so signature, issuer, audience, expiry and scope
checks all run for real. Nothing is mocked at the verification boundary.
"""

from __future__ import annotations

import asyncio
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import http_app
import oauth
import server
from conftest import add_member
from test_http import PROJECTS, Server, call, run  # noqa: F401

ISSUER = "https://tenant.us.auth0.com/"
PROJECT_URL = "https://vault.example.com/p/decision-tree/mcp"


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


@pytest.fixture
def config():
    return oauth.OAuthConfig(issuer=ISSUER)


@pytest.fixture
def verifier(config, keypair):
    private, public = keypair

    class StubJWKS:
        """Stands in for the network fetch; the crypto is still real."""

        def __init__(self):
            self.calls = 0

        def get_signing_key_from_jwt(self, token):
            self.calls += 1
            return type("K", (), {"key": public})()

    return oauth.TokenVerifier(config, jwk_client=StubJWKS())


def make_token(keypair, **overrides):
    private, _ = keypair
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": PROJECT_URL,
        "sub": "auth0|user123",
        "iat": now,
        "exp": now + 3600,
        "scope": "",
    }
    claims.update(overrides)
    for key in [k for k, v in claims.items() if v is None]:
        del claims[key]
    return jwt.encode(claims, private, algorithm="RS256")


class TestConfig:
    def test_absent_issuer_means_no_oauth(self):
        assert oauth.OAuthConfig.from_env({}) is None
        assert oauth.OAuthConfig.from_env({"CONTEXT_VAULT_OAUTH_ISSUER": "  "}) is None

    def test_built_from_env(self):
        config = oauth.OAuthConfig.from_env(
            {
                "CONTEXT_VAULT_OAUTH_ISSUER": "https://tenant.us.auth0.com",
                "CONTEXT_VAULT_OAUTH_AUDIENCE": "https://vault.example.com",
                "CONTEXT_VAULT_OAUTH_SCOPES": "vault:read vault:write",
            }
        )
        assert config.issuer_url == "https://tenant.us.auth0.com/"
        assert config.audience == "https://vault.example.com"
        assert config.required_scopes == ("vault:read", "vault:write")

    def test_derived_urls(self, config):
        assert config.jwks_uri == f"{ISSUER}.well-known/jwks.json"
        assert config.metadata_url == f"{ISSUER}.well-known/openid-configuration"

    def test_issuer_trailing_slash_is_normalised(self):
        bare = oauth.OAuthConfig(issuer="https://tenant.us.auth0.com")
        slashed = oauth.OAuthConfig(issuer="https://tenant.us.auth0.com/")
        assert bare.issuer_url == slashed.issuer_url


class TestVerification:
    def test_valid_token_accepted(self, verifier, keypair):
        claims = asyncio.run(verifier.verify(make_token(keypair), PROJECT_URL))
        assert claims["sub"] == "auth0|user123"

    def test_wrong_audience_rejected(self, verifier, keypair):
        """A token minted for another project must not work here."""
        token = make_token(keypair, aud="https://vault.example.com/p/other/mcp")
        with pytest.raises(oauth.AuthError):
            asyncio.run(verifier.verify(token, PROJECT_URL))

    def test_wrong_issuer_rejected(self, verifier, keypair):
        token = make_token(keypair, iss="https://evil.example.com/")
        with pytest.raises(oauth.AuthError):
            asyncio.run(verifier.verify(token, PROJECT_URL))

    def test_expired_token_rejected(self, verifier, keypair):
        now = int(time.time())
        token = make_token(keypair, exp=now - 60, iat=now - 3600)
        with pytest.raises(oauth.AuthError):
            asyncio.run(verifier.verify(token, PROJECT_URL))

    def test_token_signed_by_another_key_rejected(self, verifier):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = int(time.time())
        token = jwt.encode(
            {"iss": ISSUER, "aud": PROJECT_URL, "iat": now, "exp": now + 3600},
            other,
            algorithm="RS256",
        )
        with pytest.raises(oauth.AuthError):
            asyncio.run(verifier.verify(token, PROJECT_URL))

    def test_algorithm_allowlist_is_exactly_rs256(self, config):
        """Guards the allowlist itself.

        The alg=none and HS256 tests below pass even with a widened allowlist,
        because PyJWT independently refuses those algorithms once a real RSA key
        is supplied. So they do not defend this list — this does. Adding `none`
        removes signing; adding a symmetric alg like HS256 lets anyone who has
        the public key sign their own tokens with it.
        """
        assert config.algorithms == ("RS256",)

    def test_hs256_token_signed_with_the_public_key_rejected(self, verifier, keypair):
        """The classic RS256->HS256 confusion attack.

        Assembled by hand rather than with jwt.encode, which refuses to sign
        HMAC with a PEM key — an attacker has no such scruples, and building it
        through the library would fail while forging instead of while verifying.
        """
        import base64
        import hashlib
        import hmac
        import json

        from cryptography.hazmat.primitives import serialization

        _, public = keypair
        pem = public.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        def b64(raw: bytes) -> bytes:
            return base64.urlsafe_b64encode(raw).rstrip(b"=")

        now = int(time.time())
        header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        payload = b64(
            json.dumps(
                {"iss": ISSUER, "aud": PROJECT_URL, "iat": now, "exp": now + 3600}
            ).encode()
        )
        signing_input = header + b"." + payload
        signature = b64(hmac.new(pem, signing_input, hashlib.sha256).digest())
        forged = (signing_input + b"." + signature).decode()

        with pytest.raises(oauth.AuthError):
            asyncio.run(verifier.verify(forged, PROJECT_URL))

    def test_unsigned_token_rejected(self, verifier, keypair):
        """alg=none must never be honoured."""
        now = int(time.time())
        token = jwt.encode(
            {"iss": ISSUER, "aud": PROJECT_URL, "iat": now, "exp": now + 3600},
            key="",
            algorithm="none",
        )
        with pytest.raises(oauth.AuthError):
            asyncio.run(verifier.verify(token, PROJECT_URL))

    def test_garbage_rejected(self, verifier):
        with pytest.raises(oauth.AuthError):
            asyncio.run(verifier.verify("not-a-jwt", PROJECT_URL))

    def test_missing_required_claims_rejected(self, verifier, keypair):
        token = make_token(keypair, exp=None)
        with pytest.raises(oauth.AuthError):
            asyncio.run(verifier.verify(token, PROJECT_URL))

    def test_fixed_audience_overrides_per_project(self, keypair):
        """Covers an Auth0 tenant where one API serves every project."""
        config = oauth.OAuthConfig(issuer=ISSUER, audience="https://vault.example.com")
        _, public = keypair
        stub = type(
            "S", (), {"get_signing_key_from_jwt": lambda self, t: type("K", (), {"key": public})()}
        )()
        verifier = oauth.TokenVerifier(config, jwk_client=stub)
        token = make_token(keypair, aud="https://vault.example.com")
        # Valid for any project URL, because the audience is pinned.
        assert asyncio.run(verifier.verify(token, PROJECT_URL))
        assert asyncio.run(verifier.verify(token, "https://vault.example.com/p/x/mcp"))

    def test_required_scope_enforced(self, keypair):
        config = oauth.OAuthConfig(issuer=ISSUER, required_scopes=("vault:write",))
        _, public = keypair
        stub = type(
            "S", (), {"get_signing_key_from_jwt": lambda self, t: type("K", (), {"key": public})()}
        )()
        verifier = oauth.TokenVerifier(config, jwk_client=stub)

        with pytest.raises(oauth.AuthError, match="vault:write"):
            asyncio.run(verifier.verify(make_token(keypair, scope="vault:read"), PROJECT_URL))
        assert asyncio.run(
            verifier.verify(make_token(keypair, scope="vault:read vault:write"), PROJECT_URL)
        )


class TestMetadataDocument:
    def test_shape(self, config):
        doc = oauth.protected_resource_metadata(PROJECT_URL, config)
        assert doc["resource"] == PROJECT_URL
        assert doc["authorization_servers"] == ["https://tenant.us.auth0.com/"]
        assert doc["bearer_methods_supported"] == ["header"]

    def test_authorization_server_matches_auth0_issuer_exactly(self, config):
        """Trailing slash included — Auth0 reports `iss` with one, and a client
        comparing this entry against the issuer must see them match."""
        doc = oauth.protected_resource_metadata(PROJECT_URL, config)
        assert doc["authorization_servers"] == [config.issuer_url]
        assert doc["authorization_servers"][0].endswith("/")

    def test_scopes_only_when_required(self, config):
        assert "scopes_supported" not in oauth.protected_resource_metadata(
            PROJECT_URL, config
        )
        scoped = oauth.OAuthConfig(issuer=ISSUER, required_scopes=("vault:read",))
        assert oauth.protected_resource_metadata(PROJECT_URL, scoped)[
            "scopes_supported"
        ] == ["vault:read"]

    def test_challenge_header_points_at_metadata(self):
        header = oauth.challenge_header("https://v.example.com/.well-known/x")
        assert header.startswith("Bearer ")
        assert 'resource_metadata="https://v.example.com/.well-known/x"' in header

    def test_challenge_header_carries_error(self):
        header = oauth.challenge_header("https://v/x", "token expired")
        assert 'error="invalid_token"' in header
        assert "token expired" in header


# --------------------------------------------------------------------------
# Over real HTTP
# --------------------------------------------------------------------------


class TestOverHttp:
    def _app(self, keypair, **kwargs):
        _, public = keypair
        stub = type(
            "S", (), {"get_signing_key_from_jwt": lambda self, t: type("K", (), {"key": public})()}
        )()
        config = oauth.OAuthConfig(issuer=ISSUER)
        return http_app.build_app(
            oauth_config=config, verifier=oauth.TokenVerifier(config, jwk_client=stub), **kwargs
        )

    def test_every_endpoint_advertises_one_resource(self, vault, keypair):
        """One identifier for the whole server, and it has to be the origin.

        The client sends this back as `resource`, and RFC 8707 makes that an
        audience the authorization server must already know — so a URL per
        project meant an API per project, which a teammate starting one cannot
        register. RFC 9728 then has the client check the advertised value
        against where it connected, and the MCP SDK accepts only the exact URL
        or its origin. The origin is the one value that satisfies both.
        """

        async def scenario():
            import httpx2

            async with Server(self._app(keypair)) as srv:
                base = f"http://127.0.0.1:{srv.port}"
                async with httpx2.AsyncClient() as client:
                    return base, [
                        await client.get(f"{base}{path}")
                        for path in (
                            "/.well-known/oauth-protected-resource/p/decision-tree/mcp",
                            "/.well-known/oauth-protected-resource/p/hopscotch/mcp",
                            "/.well-known/oauth-protected-resource/mcp",
                            "/.well-known/oauth-protected-resource",
                        )
                    ]

        base, responses = run(scenario())
        assert [r.status_code for r in responses] == [200, 200, 200, 200]
        resources = {r.json()["resource"] for r in responses}
        assert resources == {f"{base}/"}, resources
        assert responses[0].json()["authorization_servers"] == [
            "https://tenant.us.auth0.com/"
        ]

    def test_the_advertised_resource_is_what_a_client_will_accept(self, vault, keypair):
        """The check the MCP SDK makes before it will start a flow at all.

        It refused a shared identifier with a path on it: "Protected resource
        .../mcp does not match expected .../p/hopscotch/mcp (or origin)". A
        client compares origins, so the trailing slash is immaterial here — it
        matters to the authorization server, which matches strings.
        """
        from urllib.parse import urlsplit

        async def scenario():
            import httpx2

            async with Server(self._app(keypair)) as srv:
                base = f"http://127.0.0.1:{srv.port}"
                async with httpx2.AsyncClient() as client:
                    doc = await client.get(
                        f"{base}/.well-known/oauth-protected-resource/p/hopscotch/mcp"
                    )
                    return base, doc.json()["resource"]

        base, advertised = run(scenario())
        connected_to = f"{base}/p/hopscotch/mcp"
        origin = lambda u: urlsplit(u)[:2]              # noqa: E731
        assert advertised == connected_to or origin(advertised) == origin(base)

    def test_the_advertised_resource_is_spelled_as_an_authorization_server_wants(
        self, vault, keypair
    ):
        """And an authorization server matches the string, not the origin.

        Clients disagree about the trailing slash: one turns the advertised
        value into a URL before sending it, which adds the slash, and the other
        sends it verbatim. Advertising the bare form therefore worked for one
        and failed for the other with "not authorized to access resource
        server" — which reads like a permissions problem and is a spelling one.
        """

        async def scenario():
            import httpx2

            async with Server(self._app(keypair)) as srv:
                base = f"http://127.0.0.1:{srv.port}"
                async with httpx2.AsyncClient() as client:
                    doc = await client.get(
                        f"{base}/.well-known/oauth-protected-resource/mcp"
                    )
                    return base, doc.json()["resource"]

        base, advertised = run(scenario())
        assert advertised == f"{base}/"
        # Verbatim and URL-normalised both land on the registered identifier.
        from urllib.parse import urljoin

        assert urljoin(advertised, "") == advertised

    def test_a_token_for_the_server_works_on_any_project_endpoint(self, vault, keypair):
        """One audience, and membership decides which projects it reaches.

        Before membership existed the audience was the access control, so a
        per-project one was load-bearing. It is not any more, and keeping it
        cost an Auth0 API registration per project.
        """
        add_member("hopscotch", "member@example.com", "owner")

        async def scenario():
            async with Server(self._app(keypair)) as srv:
                token = make_token(
                    keypair,
                    aud=f"http://127.0.0.1:{srv.port}",
                    sub="auth0|m",
                    **{server.EMAIL_CLAIM: "member@example.com"},
                )
                return await call(
                    srv.url("hopscotch"), "get_project_brief",
                    headers={"Authorization": f"Bearer {token}"},
                )

        assert "hopscotch" in run(scenario())

    def test_a_token_for_a_different_project_is_still_rejected(self, vault, keypair):
        """Relaxing the audience must not make any old audience acceptable."""

        async def scenario():
            import httpx2

            async with Server(self._app(keypair)) as srv:
                token = make_token(keypair, aud="https://somewhere.else/p/x/mcp")
                async with httpx2.AsyncClient() as client:
                    return await client.post(
                        srv.url("hopscotch"), json={},
                        headers={"Authorization": f"Bearer {token}"},
                    )

        assert run(scenario()).status_code == 401

    def test_unauthenticated_call_challenges_with_metadata_pointer(self, vault, keypair):
        async def scenario():
            import httpx2

            async with Server(self._app(keypair)) as srv:
                async with httpx2.AsyncClient() as client:
                    return await client.post(srv.url("decision-tree"), json={})

        response = run(scenario())
        assert response.status_code == 401
        challenge = response.headers["www-authenticate"]
        assert "resource_metadata=" in challenge
        assert "/.well-known/oauth-protected-resource/p/decision-tree/mcp" in challenge

    def test_valid_jwt_reaches_the_tools(self, vault, keypair):
        # Authentication and authorisation are separate layers: the token has
        # to get past the first, and membership is what gets it past the second.
        add_member("decision-tree", "user@example.com", "owner")

        async def scenario():
            async with Server(self._app(keypair)) as srv:
                token = make_token(
                    keypair, aud=srv.url("decision-tree"),
                    **{server.EMAIL_CLAIM: "user@example.com"},
                )
                return await call(
                    srv.url("decision-tree"),
                    "get_project_brief",
                    headers={"Authorization": f"Bearer {token}"},
                )

        assert "no recorded history" in run(scenario())

    def test_jwt_for_another_project_is_rejected(self, vault, keypair):
        """The audience is the per-project URL, so tokens don't cross over."""

        async def scenario():
            import httpx2

            async with Server(self._app(keypair)) as srv:
                token = make_token(keypair, aud=srv.url("alpha"))
                async with httpx2.AsyncClient() as client:
                    return await client.post(
                        srv.url("beta"),
                        json={},
                        headers={"Authorization": f"Bearer {token}"},
                    )

        assert run(scenario()).status_code == 401

class TestForwardedHeaders:
    """Behind a TLS-terminating proxy the app speaks http, but the advertised
    resource URL must be the https one the user actually entered."""

    def test_uses_forwarded_proto_and_host(self):
        scope = {
            "scheme": "http",
            "headers": [
                (b"host", b"internal:8000"),
                (b"x-forwarded-proto", b"https"),
                (b"x-forwarded-host", b"vault.example.com"),
            ],
        }
        assert http_app.request_origin(scope) == "https://vault.example.com"
        assert (
            http_app.resource_url_for(scope, "decision-tree")
            == "https://vault.example.com/p/decision-tree/mcp"
        )

    def test_falls_back_to_host_header(self):
        scope = {"scheme": "http", "headers": [(b"host", b"127.0.0.1:8000")]}
        assert http_app.request_origin(scope) == "http://127.0.0.1:8000"

    def test_takes_first_proto_when_chained(self):
        scope = {
            "scheme": "http",
            "headers": [(b"host", b"v.example.com"), (b"x-forwarded-proto", b"https,http")],
        }
        assert http_app.request_origin(scope) == "https://v.example.com"


class TestDiagnostics:
    """The logs are the only window into a failed handshake in production."""

    def test_fingerprint_is_stable_and_not_the_token(self):
        a = http_app.token_fingerprint("secret-token")
        assert a == http_app.token_fingerprint("secret-token")
        assert a != http_app.token_fingerprint("other-token")
        assert "secret-token" not in a
        assert len(a) == 8

    def test_describe_reads_claims_without_verifying(self, keypair):
        token = make_token(keypair, aud="https://wrong.example.com")
        described = http_app.describe_token(token)
        assert "https://wrong.example.com" in described
        assert ISSUER in described

    def test_describe_handles_opaque_tokens(self):
        """Auth0 issues an opaque token when the resource param is ignored."""
        assert "opaque-or-malformed" in http_app.describe_token("not-a-jwt")

    def test_rejection_is_logged_with_both_sides(self, vault, keypair, caplog):
        _, public = keypair
        stub = type(
            "S", (), {"get_signing_key_from_jwt": lambda self, t: type("K", (), {"key": public})()}
        )()
        config = oauth.OAuthConfig(issuer=ISSUER)
        app = http_app.build_app(
            oauth_config=config, verifier=oauth.TokenVerifier(config, jwk_client=stub)
        )

        async def scenario():
            import httpx2

            async with Server(app) as srv:
                bad = make_token(keypair, aud="https://wrong.example.com/p/x/mcp")
                async with httpx2.AsyncClient() as client:
                    return await client.post(
                        srv.url("decision-tree"),
                        json={},
                        headers={"Authorization": f"Bearer {bad}"},
                    )

        with caplog.at_level("WARNING", logger="context-vault.auth"):
            assert run(scenario()).status_code == 401
        logged = caplog.text
        assert "wrong.example.com" in logged, "should record what the token claimed"
        assert "decision-tree" in logged, "should record what was expected"


class TestAudiencesAlreadyIssued:
    """A deploy must not sign everybody out.

    Tokens minted against what this server advertised before — the router URL,
    and the per-project URL before that — stay valid until they expire.

    The trailing-slash spelling is not history but the present: a client
    serialises the advertised origin as a URL before sending it back, which
    adds the slash, and the authorization server issues against whichever
    string it was told to register.
    """

    def _app(self, keypair):
        _, public = keypair
        stub = type("S", (), {
            "get_signing_key_from_jwt": lambda self, t: type("K", (), {"key": public})()
        })()
        config = oauth.OAuthConfig(issuer=ISSUER)
        return http_app.build_app(
            oauth_config=config,
            verifier=oauth.TokenVerifier(config, jwk_client=stub),
        )

    @pytest.mark.parametrize("suffix", ["", "/", "/mcp", "/p/hopscotch/mcp"])
    def test_previously_advertised_audiences_still_verify(self, vault, keypair, suffix):
        add_member("hopscotch", "member@example.com", "owner")

        async def scenario():
            async with Server(self._app(keypair)) as srv:
                token = make_token(
                    keypair,
                    aud=f"http://127.0.0.1:{srv.port}{suffix}",
                    sub="auth0|m",
                    **{server.EMAIL_CLAIM: "member@example.com"},
                )
                return await call(
                    srv.url("hopscotch"), "get_project_brief",
                    headers={"Authorization": f"Bearer {token}"},
                )

        assert "hopscotch" in run(scenario())
