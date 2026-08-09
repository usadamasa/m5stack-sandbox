"""Tests for the MCP wrapper around ResidentLink.

The tools themselves are thin, but the link cache underneath them is
not: it has to survive a port change, a disconnect, and a reconnect
without leaving a second reader thread on a port nobody closed. That is
what is exercised here, against a stub link rather than hardware.
"""

import unittest
from typing import Any, ClassVar

import buddy_mcp
from buddy_bridge import Message


class StubLink:
    """Stands in for ResidentLink; records what the server did to it."""

    instances: ClassVar[list["StubLink"]] = []

    def __init__(self, port: str) -> None:
        self.port = port
        self.connected = False
        self.dropped = False
        self.requests: list[tuple[Message, str]] = []
        self.queued_messages: list[Message] = []
        self.queued_logs: list[bytes] = []
        self.started = False
        StubLink.instances.append(self)

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def request(self, obj: Message, expect: str, timeout: float = 5.0) -> Message:
        self.requests.append((obj, expect))
        return {"ack": expect, "ok": True}

    def events(self) -> tuple[list[Message], list[bytes]]:
        msgs, logs = self.queued_messages, self.queued_logs
        self.queued_messages, self.queued_logs = [], []
        return msgs, logs

    def start_app(self, settle: float = 8.0) -> None:
        self.started = True


class _McpTestCase(unittest.TestCase):
    def setUp(self) -> None:
        StubLink.instances = []
        real_cls: Any = buddy_mcp.ResidentLink
        buddy_mcp.ResidentLink = StubLink  # pyright: ignore[reportAttributeAccessIssue]
        self.addCleanup(setattr, buddy_mcp, "ResidentLink", real_cls)
        # The module holds one link for the life of the server process.
        buddy_mcp._link = None
        self.addCleanup(setattr, buddy_mcp, "_link", None)


class ProbeSerialTest(unittest.TestCase):
    def test_missing_device_node_is_reported_as_such(self) -> None:
        result = buddy_mcp.probe_serial("/dev/definitely-not-a-device")
        self.assertFalse(result["open"])
        self.assertIn("device node unavailable", result["verdict"])
        self.assertIn("errno 2", result["error"])

    def test_reports_the_port_it_probed(self) -> None:
        result = buddy_mcp.probe_serial("/dev/definitely-not-a-device")
        self.assertEqual(result["port"], "/dev/definitely-not-a-device")


class LinkCacheTest(_McpTestCase):
    def test_connect_opens_the_default_port(self) -> None:
        result = buddy_mcp.buddy_connect()
        self.assertTrue(result["connected"])
        self.assertEqual(result["port"], buddy_mcp.DEFAULT_PORT)
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
        self.assertIsNone(buddy_mcp._link)


class ToolTest(_McpTestCase):
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


if __name__ == "__main__":
    unittest.main()
