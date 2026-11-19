"""Remote mode: streamable HTTP with OAuth (OIDC) bearer tokens.

The server is an OAuth resource server only. It never sees passwords or runs a login
flow: the MCP client (for example claude.ai) signs the person in at the OIDC issuer and
sends the access token on every request. This module checks that token and turns the
person it names into a short-lived Cube JWT.

Token checks, in order:
  1. A token that parses as a JWT is verified against the issuer's JWKS (from OIDC
     discovery): signature, iss, exp, and aud when OIDC_AUDIENCE is set. A JWT that fails
     any check is refused; it does not fall through to step 2.
  2. Any other token is sent to the issuer's userinfo endpoint. A 200 answer means the
     issuer accepts it; the answer is cached for OIDC_USERINFO_CACHE_SECONDS.
If a verified JWT carries no email claim, the userinfo endpoint is asked for it.

Tokens and secrets are never logged.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
import jwt
from pydantic import AnyHttpUrl

from mcp.server.auth.provider import AccessToken

DISCOVERY_PATH = "/.well-known/openid-configuration"


def _env_list(name: str, sep: str = ",") -> list[str]:
    return [part.strip() for part in os.getenv(name, "").split(sep) if part.strip()]


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _round_trips(url: str) -> bool:
    """The SDK stores these URLs as pydantic AnyHttpUrl, which normalises them (adds a
    trailing slash to a bare host, lowercases the host, drops a default port). Claude needs
    `resource` to equal the URL exactly, so a URL that would change is refused at startup."""
    return str(AnyHttpUrl(url)) == url


@dataclass
class RemoteConfig:
    public_url: str
    issuer: str
    audience: Optional[str] = None
    email_claim: str = "email"
    groups_claim: str = "groups"
    algorithms: list[str] = field(default_factory=lambda: ["RS256"])
    scopes_supported: list[str] = field(default_factory=list)
    required_scopes: list[str] = field(default_factory=list)
    userinfo_cache_seconds: int = 60
    leeway_seconds: int = 0
    cube_token_ttl_seconds: int = 300
    http_path: str = "/mcp"
    health_path: str = "/health"

    @classmethod
    def from_env(cls) -> "RemoteConfig":
        public_url = os.getenv("MCP_PUBLIC_URL", "")
        issuer = os.getenv("OIDC_ISSUER", "")
        missing = [name for name, value in (("MCP_PUBLIC_URL", public_url), ("OIDC_ISSUER", issuer)) if not value]
        if missing:
            raise ValueError(f"OAuth mode needs {', '.join(missing)}")
        for name, value in (("MCP_PUBLIC_URL", public_url), ("OIDC_ISSUER", issuer)):
            if not _round_trips(value):
                raise ValueError(
                    f"{name}={value!r} would be published as {str(AnyHttpUrl(value))!r}; "
                    "set it in that exact form so the published value matches"
                )
        default_path = urlparse(public_url).path or "/mcp"
        return cls(
            public_url=public_url,
            issuer=issuer,
            audience=os.getenv("OIDC_AUDIENCE") or None,
            email_claim=os.getenv("OIDC_EMAIL_CLAIM", "email"),
            groups_claim=os.getenv("OIDC_GROUPS_CLAIM", "groups"),
            algorithms=_env_list("OIDC_JWT_ALGORITHMS") or ["RS256"],
            scopes_supported=_env_list("OIDC_SCOPES", " "),
            required_scopes=_env_list("OIDC_REQUIRED_SCOPES", " "),
            userinfo_cache_seconds=int(os.getenv("OIDC_USERINFO_CACHE_SECONDS", "60")),
            leeway_seconds=int(os.getenv("OIDC_LEEWAY_SECONDS", "0")),
            cube_token_ttl_seconds=int(os.getenv("CUBE_USER_TOKEN_TTL_SECONDS", "300")),
            http_path=os.getenv("MCP_HTTP_PATH", default_path),
            health_path=os.getenv("MCP_HEALTH_PATH", "/health"),
        )


class OIDCTokenVerifier:
    """mcp TokenVerifier for an OIDC issuer. Accepts JWT and opaque access tokens."""

    jwks_refetch_seconds = 60
    userinfo_cache_max = 1024

    def __init__(self, config: RemoteConfig, logger: logging.Logger,
                 transport: Optional[httpx.AsyncBaseTransport] = None, timeout: float = 10.0):
        self.config = config
        self.logger = logger
        self._transport = transport
        self._timeout = timeout
        self._discovery: Optional[dict] = None
        self._jwks: Optional[jwt.PyJWKSet] = None
        self._jwks_fetched_at = 0.0
        self._userinfo_cache: dict[str, tuple[float, dict]] = {}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=self._timeout)

    async def _get_json(self, url: str, headers: Optional[dict] = None) -> tuple[int, Any]:
        async with self._client() as client:
            response = await client.get(url, headers=headers)
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError):
            body = None
        return response.status_code, body

    async def discovery(self) -> dict:
        if self._discovery is None:
            url = self.config.issuer.rstrip("/") + DISCOVERY_PATH
            status, body = await self._get_json(url)
            if status != 200 or not isinstance(body, dict):
                raise RuntimeError(f"OIDC discovery at {url} answered {status}")
            if body.get("issuer") != self.config.issuer:
                raise RuntimeError("OIDC discovery issuer does not match OIDC_ISSUER")
            self._discovery = body
        return self._discovery

    async def _signing_key(self, kid: Optional[str]):
        now = time.monotonic()
        if self._jwks is None or (now - self._jwks_fetched_at > self.jwks_refetch_seconds
                                  and not self._has_kid(kid)):
            jwks_uri = (await self.discovery()).get("jwks_uri")
            if not jwks_uri:
                raise RuntimeError("OIDC discovery has no jwks_uri")
            status, body = await self._get_json(jwks_uri)
            if status != 200 or not isinstance(body, dict):
                raise RuntimeError(f"JWKS fetch answered {status}")
            self._jwks = jwt.PyJWKSet.from_dict(body)
            self._jwks_fetched_at = now
        for key in self._jwks.keys:
            if kid is None or key.key_id == kid:
                return key.key
        raise jwt.InvalidTokenError("no JWKS key matches the token's kid")

    def _has_kid(self, kid: Optional[str]) -> bool:
        return self._jwks is not None and any(k.key_id == kid for k in self._jwks.keys)

    async def _userinfo(self, token: str) -> Optional[dict]:
        cache_key = hashlib.sha256(token.encode()).hexdigest()
        now = time.monotonic()
        hit = self._userinfo_cache.get(cache_key)
        if hit and hit[0] > now:
            return hit[1]
        endpoint = (await self.discovery()).get("userinfo_endpoint")
        if not endpoint:
            raise RuntimeError("OIDC discovery has no userinfo_endpoint")
        status, body = await self._get_json(endpoint, {"Authorization": f"Bearer {token}"})
        if status != 200 or not isinstance(body, dict):
            self.logger.info("userinfo refused the token (HTTP %s)", status)
            return None
        if len(self._userinfo_cache) >= self.userinfo_cache_max:
            self._userinfo_cache = {k: v for k, v in self._userinfo_cache.items() if v[0] > now}
            if len(self._userinfo_cache) >= self.userinfo_cache_max:
                self._userinfo_cache.clear()
        self._userinfo_cache[cache_key] = (now + self.config.userinfo_cache_seconds, body)
        return body

    async def _verify_jwt(self, token: str, header: dict) -> Optional[dict]:
        alg = header.get("alg")
        if alg not in self.config.algorithms:
            self.logger.info("JWT refused: algorithm %r is not in OIDC_JWT_ALGORITHMS", alg)
            return None
        key = await self._signing_key(header.get("kid"))
        options = {"require": ["exp", "iss"], "verify_aud": bool(self.config.audience)}
        return jwt.decode(
            token, key, algorithms=[alg], issuer=self.config.issuer,
            audience=self.config.audience, options=options, leeway=self.config.leeway_seconds,
        )

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        try:
            try:
                header = jwt.get_unverified_header(token)
            except jwt.DecodeError:
                header = None
            if header is not None:
                claims = await self._verify_jwt(token, header)
                if claims is None:
                    return None
                if self.config.email_claim not in claims:
                    extra = await self._userinfo(token)
                    if extra is None:
                        return None
                    claims = {**extra, **claims}
            else:
                claims = await self._userinfo(token)
                if claims is None:
                    return None
            return self._access_token(token, claims)
        except jwt.InvalidTokenError as e:
            self.logger.info("JWT refused: %s", e)
            return None
        except Exception as e:
            self.logger.error("token check failed: %s", e)
            return None

    def _access_token(self, token: str, claims: dict) -> Optional[AccessToken]:
        email = claims.get(self.config.email_claim)
        if not isinstance(email, str) or not email:
            self.logger.info("token refused: no %r claim", self.config.email_claim)
            return None
        groups = claims.get(self.config.groups_claim) or []
        if isinstance(groups, str):
            groups = [groups]
        groups = [str(g) for g in groups]
        scope = claims.get("scope")
        scopes = scope.split() if isinstance(scope, str) else list(claims.get("scp") or [])
        exp = claims.get("exp")
        return AccessToken(
            token=token,
            client_id=str(claims.get("client_id") or claims.get("azp") or "oidc"),
            scopes=scopes,
            expires_at=int(exp) if isinstance(exp, (int, float)) else None,
            subject=str(claims.get("sub") or email),
            # iss is always this issuer: either verified in the JWT or the issuer we asked.
            claims={"iss": self.config.issuer, "email": email, "groups": groups},
        )


def mint_cube_token(api_secret: str, email: str, groups: list[str], ttl_seconds: int) -> str:
    """A Cube JWT naming the person, so Cube's security context carries who is asking."""
    now = int(time.time())
    payload = {"sub": email, "groups": list(groups), "agent": True, "iat": now, "exp": now + ttl_seconds}
    return jwt.encode(payload, api_secret, algorithm="HS256")
