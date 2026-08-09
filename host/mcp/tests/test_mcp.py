"""Tests for the MCP wrapper around ResidentLink.

The tools themselves are thin, but the link cache underneath them is
not: it has to survive a port change, a disconnect, and a reconnect
without leaving a second reader thread on a port nobody closed. That is
what is exercised here, against a stub link rather than hardware.

Since the chatter arrived there is a second thing worth pinning down:
every tool holds the device lock for the whole of its exchange, and the
chatter's link provider never opens a port of its own.
"""

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, ClassVar
from unittest import mock

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
        self.lock_held: list[bool] = []
        StubLink.instances.append(self)

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def request(self, obj: Message, expect: str, timeout: float = 5.0) -> Message:
        self.requests.append((obj, expect))
        # Recorded at the moment the device is being talked to, which is
        # the only place the answer is meaningful.
        self.lock_held.append(buddy_mcp._device_lock.locked())
        return {"ack": expect, "ok": True}

    def events(self) -> tuple[list[Message], list[bytes]]:
        msgs, logs = self.queued_messages, self.queued_logs
        self.queued_messages, self.queued_logs = [], []
        return msgs, logs

    def start_app(self, settle: float = 8.0, wait: float = 15.0) -> None:
        self.started = True
        self.start_wait = wait


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


class DeviceLockTest(_McpTestCase):
    """The lock the chatter's `acquire(blocking=False)` is testing for."""

    def test_a_tool_holds_the_lock_while_it_talks(self) -> None:
        buddy_mcp.buddy_status()
        self.assertEqual(StubLink.instances[0].lock_held, [True])

    def test_the_lock_is_free_again_afterwards(self) -> None:
        buddy_mcp.buddy_status()
        self.assertFalse(buddy_mcp._device_lock.locked())

    def test_the_lock_is_released_when_a_tool_raises(self) -> None:
        buddy_mcp.buddy_connect()
        with (
            mock.patch.object(
                StubLink, "request", side_effect=TimeoutError("no status ack within 8.0s")
            ),
            self.assertRaises(TimeoutError),
        ):
            buddy_mcp.buddy_status()
        self.assertFalse(buddy_mcp._device_lock.locked(), "the device lock was leaked")


class ChatterWiringTest(_McpTestCase):
    def test_the_chatter_is_given_a_link_only_once_one_exists(self) -> None:
        # Handing it `_get_link` instead would have the chatter claim the
        # port at server start, which is what esptool needs it not to do.
        self.assertIsNone(buddy_mcp._live_link())
        self.assertEqual(StubLink.instances, [], "asking for the link must not open a port")
        buddy_mcp.buddy_connect()
        self.assertIs(buddy_mcp._live_link(), StubLink.instances[0])

    def test_a_disconnected_link_is_not_offered(self) -> None:
        buddy_mcp.buddy_connect()
        buddy_mcp.buddy_disconnect()
        self.assertIsNone(buddy_mcp._live_link())


class ChatterToolTest(unittest.TestCase):
    def setUp(self) -> None:
        # A temp socket, so a chatter that happens to be running for real
        # on this machine does not have its own unlinked out from under
        # it by `start()`.
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.socket_path = Path(tmp.name) / "chatter.sock"
        env = mock.patch.dict(os.environ, {"BUDDY_CHATTER_SOCKET": str(self.socket_path)})
        env.start()
        self.addCleanup(env.stop)
        buddy_mcp._chatter = None
        self.addCleanup(setattr, buddy_mcp, "_chatter", None)
        self.addCleanup(lambda: buddy_mcp.buddy_chatter_stop())

    def test_status_before_start_reports_it_is_not_running(self) -> None:
        status = buddy_mcp.buddy_chatter_status()
        self.assertFalse(status["running"])
        self.assertEqual(status["spoken"], 0)

    def test_start_binds_and_stop_releases(self) -> None:
        self.assertTrue(buddy_mcp.buddy_chatter_start()["running"])
        self.assertTrue(self.socket_path.exists())
        self.assertFalse(buddy_mcp.buddy_chatter_stop()["running"])
        self.assertFalse(self.socket_path.exists())

    def test_retuning_rebuilds_the_service_with_the_new_pacing(self) -> None:
        buddy_mcp.buddy_chatter_start()
        status = buddy_mcp.buddy_chatter_start(gap_min=5.0, gap_max=5.0, voice_every=4)
        self.assertTrue(status["running"])
        self.assertEqual(status["voice_every"], 4)
        self.assertEqual(status["next_gap_s"], 5.0)

    def test_arguments_left_alone_keep_their_value(self) -> None:
        buddy_mcp.buddy_chatter_start(voice_every=7)
        cfg = buddy_mcp._chatter_service().cfg
        self.assertEqual(cfg.voice_every, 7)
        self.assertEqual(cfg.gap_min, 40.0)


if __name__ == "__main__":
    unittest.main()
