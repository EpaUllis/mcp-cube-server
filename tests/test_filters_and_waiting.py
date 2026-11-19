"""Filters reach Cube. A query still running at the wait limit asks the person whether to keep
waiting (MCP elicitation) when the client supports it; otherwise the tool says it is still
running and that sending the identical query again keeps waiting for the same run.
Run: PYTHONPATH=src python -m unittest tests/test_filters_and_waiting.py
"""
import json
import logging
import unittest
from unittest import mock

import anyio
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import ElicitResult

from mcp_cube_server import server


class FakeResponse:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


ONE_STATUS = {
    "measures": ["orders.count"],
    "dimensions": ["orders.status"],
    "filters": [{"member": "orders.status", "operator": "equals", "values": ["shipped"]}],
}


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def run(query, elicit=None, busy_polls=0):
    """Call read_data over a real MCP session. Cube answers "Continue wait" to the first
    `busy_polls` requests, one simulated second apart. Returns (tool text, requests Cube got)."""
    sent = []

    def fake_get(url, headers=None, params=None, timeout=None):
        sent.append(json.loads(params["query"]))
        if len(sent) <= busy_polls:
            return FakeResponse({"error": "Continue wait"})
        return FakeResponse({"data": [{"x": 1}]})

    async def go():
        mcp = server.build_server(
            dict(endpoint="http://cube.test:4000", api_secret=None, token_payload={}, token="t"),
            logging.getLogger("t"),
        )
        kwargs = {"elicitation_callback": elicit} if elicit else {}
        async with create_connected_server_and_client_session(mcp, **kwargs) as client:
            result = await client.call_tool("read_data", {"query": query})
            return result.content[0].text

    clock = FakeClock()
    with mock.patch.object(server.requests, "get", fake_get), \
         mock.patch.object(server.time, "time", clock.time), \
         mock.patch.object(server.time, "sleep", clock.sleep), \
         mock.patch.object(server.CubeClient, "max_wait_time", 10):
        text = anyio.run(go)
    return text, sent


def answers(*replies):
    """An elicitation callback giving the replies in turn: ("accept", True) or ("decline", None)."""
    asked = []

    async def callback(context, params):
        action, keep = replies[len(asked)]
        asked.append(params.message)
        return ElicitResult(action=action, content=None if keep is None else {"keep_waiting": keep})

    return callback, asked


class FilterTests(unittest.TestCase):
    def test_filter_reaches_cube_unchanged(self):
        text, sent = run(ONE_STATUS)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["filters"], ONE_STATUS["filters"])
        self.assertIn("x: 1", text)

    def test_unknown_operator_is_rejected_before_cube(self):
        bad = dict(ONE_STATUS, filters=[{"member": "orders.status", "operator": "like", "values": ["W"]}])
        text, sent = run(bad)
        self.assertEqual(sent, [])
        self.assertIn("operator", text)

    def test_no_filter_still_allowed(self):
        text, sent = run({"measures": ["orders.count"]})
        self.assertEqual(len(sent), 1)


class StillRunningWithoutElicitationTests(unittest.TestCase):
    def test_says_still_running_and_how_to_keep_waiting(self):
        text, sent = run(ONE_STATUS, busy_polls=100)
        self.assertIn("Still running in Cube after 10 seconds", text)
        self.assertIn("identical query again", text)
        self.assertNotIn("x: 1", text)

    def test_fast_query_is_unaffected(self):
        text, sent = run(ONE_STATUS, busy_polls=3)
        self.assertIn("x: 1", text)


class StillRunningWithElicitationTests(unittest.TestCase):
    def test_person_keeps_waiting_and_gets_the_data(self):
        callback, asked = answers(("accept", True))
        text, sent = run(ONE_STATUS, elicit=callback, busy_polls=15)
        self.assertEqual(len(asked), 1)
        self.assertIn("still running after 10 seconds", asked[0])
        self.assertIn("x: 1", text)
        # Every request was the same query: waiting longer never sent a different one.
        self.assertTrue(all(q == sent[0] for q in sent))

    def test_person_declines(self):
        callback, asked = answers(("decline", None))
        text, sent = run(ONE_STATUS, elicit=callback, busy_polls=100)
        self.assertEqual(len(asked), 1)
        self.assertIn("chose not to wait", text)

    def test_person_unticks(self):
        callback, asked = answers(("accept", False))
        text, sent = run(ONE_STATUS, elicit=callback, busy_polls=100)
        self.assertIn("chose not to wait", text)

    def test_asks_at_most_max_extensions_times(self):
        callback, asked = answers(*[("accept", True)] * 10)
        text, sent = run(ONE_STATUS, elicit=callback, busy_polls=1000)
        self.assertEqual(len(asked), server.MAX_EXTENSIONS)
        self.assertIn("Still running", text)

    def test_fast_query_asks_nobody(self):
        callback, asked = answers(("decline", None))
        text, sent = run(ONE_STATUS, elicit=callback, busy_polls=3)
        self.assertEqual(asked, [])
        self.assertIn("x: 1", text)


if __name__ == "__main__":
    unittest.main()
