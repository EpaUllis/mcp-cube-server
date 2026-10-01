"""Credential handling: a pre-minted token is sent verbatim; a secret mints a JWT;
neither is an error. Run: PYTHONPATH=src python -m unittest tests/test_token_mode.py
"""
import logging
import os
import sys
import unittest
from unittest import mock

import jwt

import mcp_cube_server
from mcp_cube_server import server


class FakeResponse:
    status_code = 200

    def json(self):
        return {"cubes": []}


class CredentialTests(unittest.TestCase):
    def _client(self, **kwargs):
        seen = {}

        def fake_get(url, headers=None, params=None):
            seen["authorization"] = headers["Authorization"]
            return FakeResponse()

        with mock.patch.object(server.requests, "get", fake_get):
            server.CubeClient(endpoint="http://cube.test:4000", logger=logging.getLogger("t"), **kwargs)
        return seen["authorization"]

    def test_pre_minted_token_is_sent_verbatim_and_needs_no_secret(self):
        sent = self._client(api_secret=None, token_payload={}, token="header.payload.sig")
        self.assertEqual(sent, "header.payload.sig")

    def test_secret_mints_hs256_jwt_with_the_payload(self):
        sent = self._client(api_secret="s3cret", token_payload={"agent": True}, token=None)
        self.assertEqual(jwt.decode(sent, "s3cret", algorithms=["HS256"]), {"agent": True})

    def test_no_credential_is_an_error_in_the_client(self):
        with self.assertRaises(ValueError):
            self._client(api_secret=None, token_payload={}, token=None)

    def test_no_credential_is_an_error_at_the_cli(self):
        # Negative control for the argparse guard: endpoint given, nothing to authenticate with.
        env = {k: v for k, v in os.environ.items() if not k.startswith("CUBE_")}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(sys, "argv", ["mcp_cube_server", "--endpoint", "http://cube.test:4000"]), \
             mock.patch.object(mcp_cube_server.dotenv, "load_dotenv", lambda *a, **k: None):
            with self.assertRaises(SystemExit) as cm:
                mcp_cube_server.main()
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
