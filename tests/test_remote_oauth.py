"""Remote mode: streamable HTTP with OAuth bearer tokens from an OIDC issuer.

A fake issuer runs in-process (discovery, JWKS from a generated RSA key, userinfo) behind an
httpx MockTransport; the MCP app runs in-process behind httpx's ASGI transport. No network.
Run: PYTHONPATH=src python -m unittest tests/test_remote_oauth.py
"""
import json
import logging
import os
import sys
import time
import unittest
from unittest import mock

import anyio
import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

import mcp_cube_server
from mcp_cube_server import server
from mcp_cube_server.remote import OIDCTokenVerifier, RemoteConfig

PUBLIC_URL = "https://mcp.example.com/mcp"
ISSUER = "https://idp.example.com/oidc/2"
METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"
CUBE_SECRET = "cube-test-secret-at-least-32-bytes-long"
OPAQUE_GOOD = "opaque-token-the-issuer-accepts"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def setUpModule():
    logging.disable(logging.INFO)


def tearDownModule():
    logging.disable(logging.NOTSET)


def jwk(public_key, kid):
    d = json.loads(RSAAlgorithm.to_jwk(public_key))
    d.update(kid=kid, use="sig", alg="RS256")
    return d


class FakeIssuer:
    """Discovery, JWKS and userinfo for ISSUER. Counts userinfo calls."""

    def __init__(self, discovery_issuer=ISSUER):
        self.discovery_issuer = discovery_issuer
        self.userinfo_calls = 0
        self.userinfo_ok_body = {"sub": "u2", "email": "opaque@example.com", "groups": ["g2"]}
        self.userinfo_error_body = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == ISSUER + "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": self.discovery_issuer, "jwks_uri": ISSUER + "/jwks",
                "userinfo_endpoint": ISSUER + "/me",
            })
        if url == ISSUER + "/jwks":
            return httpx.Response(200, json={"keys": [jwk(KEY.public_key(), "k1")]})
        if url == ISSUER + "/me":
            self.userinfo_calls += 1
            if request.headers.get("authorization") == f"Bearer {OPAQUE_GOOD}":
                return httpx.Response(200, json=self.userinfo_ok_body)
            return httpx.Response(401, json=self.userinfo_error_body)
        return httpx.Response(404)


def make_jwt(key=KEY, kid="k1", **overrides):
    now = int(time.time())
    claims = {"iss": ISSUER, "sub": "u1", "email": "person@example.com", "groups": ["analysts", "admins"],
              "aud": "cube-mcp", "iat": now, "exp": now + 600, "scope": "openid email"}
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def remote_config(**kwargs):
    args = dict(public_url=PUBLIC_URL, issuer=ISSUER)
    args.update(kwargs)
    return RemoteConfig(**args)


class FakeResponse:
    status_code = 200

    def json(self):
        return {"data": [{"x": 1}]}


def call(requests_to_send, config=None, issuer=None):
    """Run the app, send [(method, path, token, json_body)] in order (the MCP session id is
    carried forward), and return (responses, Authorization headers Cube received)."""
    config = config or remote_config()
    issuer = issuer or FakeIssuer()
    cube_auth = []

    def fake_get(url, headers=None, params=None, timeout=None):
        cube_auth.append(headers["Authorization"])
        return FakeResponse()

    async def go():
        verifier = OIDCTokenVerifier(config, logging.getLogger("t"), transport=httpx.MockTransport(issuer.handler))
        mcp = server.build_server(
            dict(endpoint="http://cube.test:4000", api_secret=CUBE_SECRET, token_payload={}, token=None),
            logging.getLogger("t"), remote=config, token_verifier=verifier, json_response=True,
        )
        app = server.http_app(mcp, config)
        responses = []
        session = None
        async with mcp.session_manager.run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://127.0.0.1:8000") as client:
                for method, path, token, body in requests_to_send:
                    headers = {"accept": "application/json, text/event-stream"}
                    if token:
                        headers["authorization"] = f"Bearer {token}"
                    if session:
                        headers["mcp-session-id"] = session
                    r = await client.request(method, path, headers=headers, json=body)
                    session = r.headers.get("mcp-session-id", session)
                    responses.append(r)
        return responses

    with mock.patch.object(server.requests, "get", fake_get):
        return anyio.run(go), cube_auth


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
READ = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
    "name": "read_data", "arguments": {"query": {"measures": ["m.count"]}}}}
LIST_RESOURCES = {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}}


def session_calling(token, *extra):
    return [("POST", "/mcp", token, INIT), ("POST", "/mcp", token, INITIALIZED),
            ("POST", "/mcp", token, READ), *[("POST", "/mcp", token, body) for body in extra]]


class UnauthenticatedTests(unittest.TestCase):
    def test_no_token_is_401_pointing_at_the_metadata(self):
        (r,), _ = call([("POST", "/mcp", None, INIT)])
        self.assertEqual(r.status_code, 401)
        header = r.headers["www-authenticate"]
        self.assertTrue(header.startswith("Bearer "), header)
        self.assertIn(f'resource_metadata="https://mcp.example.com{METADATA_PATH}"', header)

    def test_metadata_names_the_public_url_and_the_issuer(self):
        (r,), _ = call([("GET", METADATA_PATH, None, None)])
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["resource"], PUBLIC_URL)
        self.assertEqual(body["authorization_servers"], [ISSUER])
        self.assertNotIn("scopes_supported", body)

    def test_metadata_advertises_configured_scopes(self):
        (r,), _ = call([("GET", METADATA_PATH, None, None)],
                       config=remote_config(scopes_supported=["openid", "email", "groups"]))
        body = r.json()
        self.assertEqual(body["scopes_supported"], ["openid", "email", "groups"])
        self.assertEqual(body["resource"], PUBLIC_URL)

    def test_health_needs_no_token(self):
        (r,), _ = call([("GET", "/health", None, None)])
        self.assertEqual((r.status_code, r.text), (200, "ok"))


class ValidTokenTests(unittest.TestCase):
    def assert_cube_token_names(self, cube_auth, email, groups):
        self.assertEqual(len(cube_auth), 1)
        claims = jwt.decode(cube_auth[0], CUBE_SECRET, algorithms=["HS256"])
        self.assertEqual(claims["sub"], email)
        self.assertEqual(claims["groups"], groups)
        self.assertIs(claims["agent"], True)
        self.assertEqual(claims["exp"] - claims["iat"], 300)

    def test_jwt_tool_call_runs_as_the_person(self):
        responses, cube_auth = call(session_calling(make_jwt()))
        self.assertEqual([r.status_code for r in responses], [200, 202, 200])
        result = responses[2].json()["result"]
        self.assertFalse(result.get("isError"), result)
        self.assertIn("x: 1", result["content"][0]["text"])
        self.assert_cube_token_names(cube_auth, "person@example.com", ["analysts", "admins"])

    def test_opaque_token_checked_at_userinfo_and_cached(self):
        issuer = FakeIssuer()
        responses, cube_auth = call(session_calling(OPAQUE_GOOD), issuer=issuer)
        self.assertEqual([r.status_code for r in responses], [200, 202, 200])
        self.assert_cube_token_names(cube_auth, "opaque@example.com", ["g2"])
        self.assertEqual(issuer.userinfo_calls, 1)

    def test_configured_audience_is_accepted(self):
        responses, _ = call(session_calling(make_jwt()), config=remote_config(audience="cube-mcp"))
        self.assertEqual([r.status_code for r in responses], [200, 202, 200])

    def test_groups_claim_name_is_configurable(self):
        token = make_jwt(groups=None, roles=["r1"])
        _, cube_auth = call(session_calling(token), config=remote_config(groups_claim="roles"))
        self.assert_cube_token_names(cube_auth, "person@example.com", ["r1"])

    def test_no_data_resource_is_shared_between_people(self):
        responses, _ = call(session_calling(make_jwt(), LIST_RESOURCES))
        uris = [r["uri"] for r in responses[3].json()["result"]["resources"]]
        self.assertFalse([u for u in uris if u.startswith("data://")], uris)
        self.assertNotIn("data_id", responses[2].json()["result"]["content"][0]["text"])


class RefusedTokenTests(unittest.TestCase):
    def assert_refused(self, token, **kwargs):
        (r,), cube_auth = call([("POST", "/mcp", token, INIT)], **kwargs)
        self.assertEqual(r.status_code, 401)
        self.assertIn("resource_metadata=", r.headers["www-authenticate"])
        self.assertEqual(cube_auth, [])

    def test_expired(self):
        self.assert_refused(make_jwt(exp=int(time.time()) - 3600))

    def test_wrong_issuer(self):
        self.assert_refused(make_jwt(iss="https://other-idp.example.com"))

    def test_signed_by_another_key(self):
        self.assert_refused(make_jwt(key=OTHER_KEY))

    def test_wrong_audience(self):
        self.assert_refused(make_jwt(aud="some-other-app"), config=remote_config(audience="cube-mcp"))

    def test_no_email(self):
        # No email in the JWT, and userinfo does not accept it either.
        self.assert_refused(make_jwt(email=None))

    def test_opaque_token_the_issuer_refuses(self):
        self.assert_refused("not-a-token-the-issuer-knows")

    def test_discovery_naming_another_issuer(self):
        self.assert_refused(make_jwt(), issuer=FakeIssuer(discovery_issuer="https://evil.example.com"))


class VerifierTests(unittest.TestCase):
    """The verifier on its own. Over HTTP the SDK also refuses an expired AccessToken, so the
    HTTP tests above would pass even if the verifier let an expired JWT through."""

    def verify(self, token, issuer=None, config=None):
        issuer = issuer or FakeIssuer()
        verifier = OIDCTokenVerifier(config or remote_config(), logging.getLogger("t"),
                                     transport=httpx.MockTransport(issuer.handler))
        return anyio.run(verifier.verify_token, token)

    def test_valid_jwt(self):
        token = self.verify(make_jwt())
        self.assertEqual(token.claims, {"iss": ISSUER, "email": "person@example.com",
                                        "groups": ["analysts", "admins"]})
        self.assertEqual(token.scopes, ["openid", "email"])

    def test_expired_jwt(self):
        self.assertIsNone(self.verify(make_jwt(exp=int(time.time()) - 3600)))

    def test_userinfo_error_status_with_a_json_body(self):
        issuer = FakeIssuer()
        issuer.userinfo_error_body = {"email": "attacker@example.com"}
        self.assertIsNone(self.verify("unknown-opaque-token", issuer=issuer))

    def test_identity_without_email(self):
        issuer = FakeIssuer()
        issuer.userinfo_ok_body = {"sub": "u2", "groups": ["g2"]}
        self.assertIsNone(self.verify(OPAQUE_GOOD, issuer=issuer))


class ConfigTests(unittest.TestCase):
    def test_url_that_would_be_rewritten_is_refused(self):
        env = {"MCP_PUBLIC_URL": "https://mcp.example.com", "OIDC_ISSUER": ISSUER}
        with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError):
            RemoteConfig.from_env()

    def test_mcp_path_follows_the_public_url(self):
        env = {"MCP_PUBLIC_URL": "https://mcp.example.com/cube/mcp", "OIDC_ISSUER": ISSUER}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(RemoteConfig.from_env().http_path, "/cube/mcp")

    def test_oauth_mode_refuses_a_shared_cube_token(self):
        with self.assertRaises(ValueError):
            server.build_server(dict(endpoint="http://cube.test:4000", api_secret=CUBE_SECRET,
                                     token_payload={}, token="shared"),
                                logging.getLogger("t"), remote=remote_config())


def run_cli(env):
    """Run the CLI entry point with `env`; return the kwargs server.main was given."""
    seen = {}
    with mock.patch.dict(os.environ, env, clear=True), \
         mock.patch.object(sys, "argv", ["mcp_cube_server"]), \
         mock.patch.object(mcp_cube_server.dotenv, "load_dotenv", lambda *a, **k: None), \
         mock.patch.object(mcp_cube_server.server, "main", lambda *a, **k: seen.update(k)):
        mcp_cube_server.main()
    return seen


class CliTests(unittest.TestCase):
    BASE = {"CUBE_ENDPOINT": "http://cube.test:4000", "CUBE_TOKEN": "t"}

    def test_default_is_stdio_without_oauth(self):
        seen = run_cli(self.BASE)
        self.assertEqual((seen["transport"], seen["remote"]), ("stdio", None))

    def test_stdio_main_runs_fastmcp_over_stdio(self):
        calls = []
        with mock.patch.object(server.FastMCP, "run", lambda self, *a, **k: calls.append((a, k))):
            server.main(dict(endpoint="http://cube.test:4000", api_secret=None, token_payload={}, token="t"),
                        logging.getLogger("t"))
        self.assertEqual(calls, [((), {})])

    def test_http_without_oidc_settings_is_an_error(self):
        with self.assertRaises(SystemExit):
            run_cli(dict(self.BASE, MCP_TRANSPORT="streamable-http"))

    def test_http_with_oidc_settings(self):
        seen = run_cli({"CUBE_ENDPOINT": "http://cube.test:4000", "CUBE_API_SECRET": "s",
                        "MCP_TRANSPORT": "streamable-http", "MCP_PUBLIC_URL": PUBLIC_URL,
                        "OIDC_ISSUER": ISSUER, "MCP_HOST": "0.0.0.0", "MCP_PORT": "9000"})
        self.assertEqual(seen["transport"], "streamable-http")
        self.assertEqual((seen["remote"].public_url, seen["remote"].issuer), (PUBLIC_URL, ISSUER))
        self.assertEqual((seen["host"], seen["port"]), ("0.0.0.0", 9000))

    def test_http_oauth_refuses_cube_token(self):
        with self.assertRaises(SystemExit):
            run_cli({"CUBE_ENDPOINT": "http://cube.test:4000", "CUBE_API_SECRET": "s", "CUBE_TOKEN": "t",
                     "MCP_TRANSPORT": "streamable-http", "MCP_PUBLIC_URL": PUBLIC_URL, "OIDC_ISSUER": ISSUER})


if __name__ == "__main__":
    unittest.main()
