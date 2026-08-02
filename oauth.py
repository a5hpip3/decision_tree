"""OAuth 2.0 resource-server support — Auth0 (or any OIDC issuer) as the AS.

Context Vault never issues tokens. It is a *protected resource*: it advertises
where to get authorized (RFC 9728 protected resource metadata), rejects
unauthenticated calls with a 401 pointing at that metadata, and verifies the
JWTs the authorization server issues.

Audience is the one place this gets subtle. The MCP spec (RFC 8707) has the
client send `resource` = the canonical URI of the MCP server, which for this
server is per-project (`https://host/p/<project>/mcp`). Auth0 resolves that URI
to one of its registered APIs, and its docs don't say whether the match is exact
or by prefix. So the audience check is configurable:

  per-project (default)  aud must equal the project's own URL
                         — correct if Auth0 matches per-project APIs
  fixed                  aud must equal CONTEXT_VAULT_OAUTH_AUDIENCE
                         — use when one Auth0 API covers every project

Either way the metadata advertises the exact URL the user entered, which is what
Anthropic requires.
"""

from __future__ import annotations

import dataclasses

import anyio.to_thread
import jwt


class AuthError(Exception):
    """Token rejected. The message is safe to return to a client."""


@dataclasses.dataclass(frozen=True)
class OAuthConfig:
    issuer: str
    audience: str | None = None
    required_scopes: tuple[str, ...] = ()
    algorithms: tuple[str, ...] = ("RS256",)

    @property
    def issuer_url(self) -> str:
        """Issuer with a trailing slash, the form Auth0 puts in `iss`."""
        return self.issuer if self.issuer.endswith("/") else self.issuer + "/"

    @property
    def jwks_uri(self) -> str:
        return f"{self.issuer_url}.well-known/jwks.json"

    @property
    def metadata_url(self) -> str:
        return f"{self.issuer_url}.well-known/openid-configuration"

    @classmethod
    def from_env(cls, env) -> OAuthConfig | None:
        """Build from environment, or None when OAuth isn't configured."""
        issuer = (env.get("CONTEXT_VAULT_OAUTH_ISSUER") or "").strip()
        if not issuer:
            return None
        scopes = (env.get("CONTEXT_VAULT_OAUTH_SCOPES") or "").split()
        return cls(
            issuer=issuer,
            audience=(env.get("CONTEXT_VAULT_OAUTH_AUDIENCE") or "").strip() or None,
            required_scopes=tuple(scopes),
        )


class TokenVerifier:
    """Verifies issuer JWTs, caching the signing keys between calls."""

    def __init__(self, config: OAuthConfig, jwk_client=None):
        self.config = config
        self._jwks = jwk_client or jwt.PyJWKClient(
            config.jwks_uri, cache_keys=True, lifespan=600
        )

    def _verify_sync(self, token: str, audience: str) -> dict:
        try:
            key = self._jwks.get_signing_key_from_jwt(token).key
        except Exception as exc:  # PyJWKClient raises several unrelated types
            raise AuthError(f"cannot resolve signing key: {exc}") from exc

        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=list(self.config.algorithms),
                audience=audience,
                issuer=self.config.issuer_url,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError(str(exc)) from exc

        granted = set(str(claims.get("scope", "")).split())
        missing = set(self.config.required_scopes) - granted
        if missing:
            raise AuthError(f"missing required scope(s): {' '.join(sorted(missing))}")
        return claims

    async def verify(self, token: str, resource_url: str) -> dict:
        """Verify a bearer token for the given per-project resource URL.

        Raises AuthError if the token is not valid for this resource.
        """
        audience = self.config.audience or resource_url
        # PyJWKClient does blocking network I/O when the key cache misses.
        return await anyio.to_thread.run_sync(self._verify_sync, token, audience)


def protected_resource_metadata(resource_url: str, config: OAuthConfig) -> dict:
    """RFC 9728 metadata telling a client where to get authorized.

    `resource` must equal the URL the user entered in Claude, path included, or
    Claude rejects the document.
    """
    metadata = {
        "resource": resource_url,
        # The issuer identifier verbatim, trailing slash included. Auth0's own
        # metadata reports `iss` as "https://tenant.us.auth0.com/", and a client
        # that compares this entry against that value must see them match.
        "authorization_servers": [config.issuer_url],
        "bearer_methods_supported": ["header"],
        "resource_name": "DecisionTree",
    }
    if config.required_scopes:
        metadata["scopes_supported"] = list(config.required_scopes)
    return metadata


def challenge_header(metadata_url: str, error: str | None = None) -> str:
    """The WWW-Authenticate value that starts a client's OAuth flow.

    Without the resource_metadata pointer a client has to guess where the
    metadata lives by probing well-known paths, so always include it.
    """
    parts = [f'Bearer resource_metadata="{metadata_url}"']
    if error:
        parts.append(f'error="invalid_token", error_description="{error}"')
    return ", ".join(parts)
