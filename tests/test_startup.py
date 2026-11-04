"""Reaching Cube: the server starts without calling Cube; every call has a timeout; a 403
retries once with a fresh token and returns the parsed answer.
Run: PYTHONPATH=src python -m unittest tests/test_startup.py
"""
import importlib
import logging
import os
import unittest
from unittest import mock

import requests

from mcp_cube_server import server


class FakeResponse:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code

    def json(self):
        return self.body


def make_client(**kwargs):
    args = dict(endpoint="http://cube.test:4000", api_secret=None, token_payload={},
                logger=logging.getLogger("t"), token="t")
    args.update(kwargs)
    return server.CubeClient(**args)


class StartupTests(unittest.TestCase):
    def test_client_starts_without_calling_cube(self):
        calls = []
        with mock.patch.object(server.requests, "get", lambda *a, **k: calls.append(a)):
            make_client()
        self.assertEqual(calls, [])

    def test_unreachable_cube_becomes_an_error_not_a_hang(self):
        def fake_get(url, headers=None, params=None, timeout=None):
            raise requests.exceptions.ConnectTimeout("connect timed out")

        client = make_client()
        with mock.patch.object(server.requests, "get", fake_get):
            result = client.describe()
        self.assertIn("connect timed out", result["error"])


class TimeoutTests(unittest.TestCase):
    def _timeouts_seen(self, waits):
        """Cube says Continue wait `waits` times, then answers. Returns the timeout of every call."""
        seen = []

        def fake_get(url, headers=None, params=None, timeout=None):
            seen.append(timeout)
            if len(seen) <= waits:
                return FakeResponse({"error": "Continue wait"})
            return FakeResponse({"data": []})

        client = make_client()
        with mock.patch.object(server.requests, "get", fake_get), \
             mock.patch.object(server.time, "sleep", lambda s: None):
            client._request("load", query={})
        return seen

    def test_every_call_has_a_connect_and_read_timeout(self):
        seen = self._timeouts_seen(waits=2)
        self.assertEqual(len(seen), 3)
        self.assertEqual(seen, [(10, 60)] * 3)

    def test_timeouts_default_and_follow_the_environment(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CUBE_REQUEST_TIMEOUT_SECONDS", "CUBE_READ_TIMEOUT_SECONDS")}
        with mock.patch.dict(os.environ, env, clear=True):
            reloaded = importlib.reload(server).CubeClient
            self.assertEqual((reloaded.connect_timeout, reloaded.read_timeout), (10, 60))
        with mock.patch.dict(os.environ, {"CUBE_REQUEST_TIMEOUT_SECONDS": "5",
                                          "CUBE_READ_TIMEOUT_SECONDS": "120"}):
            reloaded = importlib.reload(server).CubeClient
            self.assertEqual((reloaded.connect_timeout, reloaded.read_timeout), (5, 120))
        importlib.reload(server)


class RetryTests(unittest.TestCase):
    def test_403_retries_once_with_the_fresh_token_and_returns_parsed_json(self):
        sent = []

        def fake_get(url, headers=None, params=None, timeout=None):
            sent.append(headers["Authorization"])
            if len(sent) == 1:
                return FakeResponse({"error": "Invalid token"}, status_code=403)
            return FakeResponse({"cubes": []})

        client = make_client(api_secret="s3cret", token=None, token_payload={"n": 1})
        client.token_payload = {"n": 2}  # the refreshed token differs from the first
        with mock.patch.object(server.requests, "get", fake_get):
            result = client.describe()
        self.assertEqual(result, {"cubes": []})
        self.assertEqual(len(sent), 2)
        self.assertNotEqual(sent[0], sent[1])


if __name__ == "__main__":
    unittest.main()
