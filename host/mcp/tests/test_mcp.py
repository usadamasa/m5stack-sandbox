"""Tests for the MCP wrapper around ResidentLink.

The tools themselves are thin, but the link cache underneath them is
not: it has to survive a port change, a disconnect, and a reconnect
without leaving a second reader thread on a port nobody closed. That is
what is exercised here, against a stub link rather than hardware.

Since the chatter arrived there is a second thing worth pinning down:
every tool holds the device lock for the whole of its exchange.

デバイスを触る tool のぶん。状態そのものは `test_mcp_state.py`、debug と
chatter の tool と起動口はそれぞれ別のファイルにある。
"""

import unittest
from unittest import mock

import buddy_mcp
import mcp_state
from mcp_stubs import McpTestCase, StubLink


class ProbeSerialTest(unittest.TestCase):
    def test_missing_device_node_is_reported_as_such(self) -> None:
        result = buddy_mcp.probe_serial("/dev/definitely-not-a-device")
        self.assertFalse(result["open"])
        self.assertIn("device node unavailable", result["verdict"])
        self.assertIn("errno 2", result["error"])

    def test_reports_the_port_it_probed(self) -> None:
        result = buddy_mcp.probe_serial("/dev/definitely-not-a-device")
        self.assertEqual(result["port"], "/dev/definitely-not-a-device")


class LinkCacheTest(McpTestCase):
    def test_connect_opens_the_default_port(self) -> None:
        result = buddy_mcp.buddy_connect()
        self.assertTrue(result["connected"])
        self.assertEqual(result["port"], mcp_state.DEFAULT_PORT)
        self.assertEqual(len(StubLink.instances), 1)

    def test_second_call_reuses_the_same_link(self) -> None:
        buddy_mcp.buddy_connect()
        buddy_mcp.buddy_connect()
        self.assertEqual(len(StubLink.instances), 1)

    def test_changing_port_closes_the_old_one(self) -> None:
        # Two readers on two ports would both stay alive and the second
        # would look connected while the first still held its fd.
        buddy_mcp.buddy_connect("/dev/first")
        buddy_mcp.buddy_connect("/dev/second")
        self.assertEqual([link.port for link in StubLink.instances], ["/dev/first", "/dev/second"])
        self.assertFalse(StubLink.instances[0].connected)
        self.assertTrue(StubLink.instances[1].connected)

    def test_disconnect_without_a_link_is_not_an_error(self) -> None:
        result = buddy_mcp.buddy_disconnect()
        self.assertFalse(result["connected"])
        self.assertEqual(result["note"], "was not connected")

    def test_disconnect_releases_the_port(self) -> None:
        buddy_mcp.buddy_connect()
        result = buddy_mcp.buddy_disconnect()
        self.assertFalse(result["connected"])
        self.assertFalse(StubLink.instances[0].connected)
        self.assertIsNone(mcp_state.link)


class ToolTest(McpTestCase):
    def test_status_asks_for_a_status_ack(self) -> None:
        ack = buddy_mcp.buddy_status()
        self.assertEqual(ack["ack"], "status")
        self.assertEqual(StubLink.instances[0].requests, [({"cmd": "status"}, "status")])

    def test_set_name_and_owner_carry_their_payloads(self) -> None:
        buddy_mcp.buddy_set_name("Mikawa")
        buddy_mcp.buddy_set_owner("usadamasa")
        self.assertEqual(
            StubLink.instances[0].requests,
            [
                ({"cmd": "name", "name": "Mikawa"}, "name"),
                ({"cmd": "owner", "owner": "usadamasa"}, "owner"),
            ],
        )

    def test_events_decodes_logs_leniently(self) -> None:
        # Device logs are whatever print() produced; a reset can cut a
        # line mid-character. Losing the whole batch to a UnicodeError
        # would drop the traceback we are reading the logs for.
        buddy_mcp.buddy_connect()
        StubLink.instances[0].queued_logs = [b"ok", b"\xff\xfe broken"]
        result = buddy_mcp.buddy_events()
        self.assertEqual(result["logs"], ["ok", "�� broken"])
        self.assertFalse(result["dropped"])

    def test_start_app_returns_the_startup_output(self) -> None:
        buddy_mcp.buddy_connect()
        StubLink.instances[0].queued_logs = [b"claude_buddy: run() start"]
        result = buddy_mcp.buddy_start_app(settle=0.0)
        self.assertTrue(StubLink.instances[0].started)
        self.assertEqual(result["logs"], ["claude_buddy: run() start"])


class DeviceLockTest(McpTestCase):
    """The lock the chatter's `acquire(blocking=False)` is testing for."""

    def test_a_tool_holds_the_lock_while_it_talks(self) -> None:
        buddy_mcp.buddy_status()
        self.assertEqual(StubLink.instances[0].lock_held, [True])

    def test_the_lock_is_free_again_afterwards(self) -> None:
        buddy_mcp.buddy_status()
        self.assertFalse(mcp_state.device_lock.locked())

    def test_the_lock_is_released_when_a_tool_raises(self) -> None:
        buddy_mcp.buddy_connect()
        with (
            mock.patch.object(
                StubLink, "request", side_effect=TimeoutError("no status ack within 8.0s")
            ),
            self.assertRaises(TimeoutError),
        ):
            buddy_mcp.buddy_status()
        self.assertFalse(mcp_state.device_lock.locked(), "the device lock was leaked")


if __name__ == "__main__":
    unittest.main()
