"""Waiting on Cube's "Continue wait": a slow query inside the limit returns its data;
one past the limit returns an error that says not to resend at once.
Run: PYTHONPATH=src python -m unittest tests/test_wait.py
"""
import importlib
import logging
import os
import unittest
from unittest import mock

from mcp_cube_server import server


class FakeResponse:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class WaitTests(unittest.TestCase):
    def _load(self, waits, max_wait):
        """Cube says Continue wait `waits` times, one simulated second apart, then answers."""
        calls = {"n": 0}

        def fake_get(url, headers=None, params=None):
            calls["n"] += 1
            if url.endswith("/meta"):
                return FakeResponse({"cubes": []})
            if calls["n"] <= waits + 1:
                return FakeResponse({"error": "Continue wait"})
            return FakeResponse({"data": [{"v": 1}]})

        clock = FakeClock()
        with mock.patch.object(server.requests, "get", fake_get), \
             mock.patch.object(server.time, "time", clock.time), \
             mock.patch.object(server.time, "sleep", clock.sleep), \
             mock.patch.object(server.CubeClient, "max_wait_time", max_wait):
            client = server.CubeClient(endpoint="http://cube.test:4000", api_secret=None,
                                       token_payload={}, logger=logging.getLogger("t"), token="t")
            calls["n"] = 0
            return client._request("load", query={})

    def test_slow_query_inside_the_limit_returns_data(self):
        self.assertEqual(self._load(waits=30, max_wait=90), {"data": [{"v": 1}]})

    def test_query_past_the_limit_says_not_to_resend(self):
        # Negative control: the same 30-second query with the old 10-second limit fails.
        result = self._load(waits=30, max_wait=10)
        self.assertIn("within 10 seconds", result["error"])
        self.assertIn("Do not resend it straight away", result["error"])

    def test_limit_defaults_to_90_and_follows_the_environment(self):
        env = {k: v for k, v in os.environ.items() if k != "CUBE_MAX_WAIT_SECONDS"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(importlib.reload(server).CubeClient.max_wait_time, 90)
        with mock.patch.dict(os.environ, {"CUBE_MAX_WAIT_SECONDS": "120"}):
            self.assertEqual(importlib.reload(server).CubeClient.max_wait_time, 120)
        importlib.reload(server)


if __name__ == "__main__":
    unittest.main()
