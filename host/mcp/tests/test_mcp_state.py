"""MCP server の土台のテスト — リンクの差し出し方と、起動時の 1 回の接続。

ここが見ているのは tool ではなくその下の状態。chatter に渡すリンクは既に
上がっているときしか出てこないこと、そして `connect_on_start` がその唯一の
例外として 1 回だけポートを開くこと。
"""

import unittest

import buddy_mcp
import mcp_chatter_tools
import mcp_state
from mcp_stubs import McpTestCase, StubLink


class ChatterWiringTest(McpTestCase):
    def test_the_chatter_is_given_a_link_only_once_one_exists(self) -> None:
        # Handing it `get_link` instead would have the chatter claim the
        # port at server start, which is what esptool needs it not to do.
        self.assertIsNone(mcp_state.live_link())
        self.assertEqual(StubLink.instances, [], "asking for the link must not open a port")
        buddy_mcp.buddy_connect()
        self.assertIs(mcp_state.live_link(), StubLink.instances[0])

    def test_a_disconnected_link_is_not_offered(self) -> None:
        buddy_mcp.buddy_connect()
        buddy_mcp.buddy_disconnect()
        self.assertIsNone(mcp_state.live_link())


class _LockWatchingLink(StubLink):
    """Records whether the device lock was held as the port opened."""

    def connect(self) -> None:
        self.lock_held.append(mcp_state.device_lock.locked())
        super().connect()


class _RefusingLink(StubLink):
    """A port that is not there — the board unplugged, or already taken."""

    def connect(self) -> None:
        raise OSError("no such port")


class ConnectOnStartTest(McpTestCase):
    """The one opening the server makes for itself.

    The chatter never opens the port, so a fresh session is silent until
    somebody calls `buddy_connect`. This is the single exception: one
    attempt as the server starts, so the muttering is on from the first
    tool call rather than from whenever the agent happens to connect.

    One attempt, and no retry loop. `buddy_disconnect` before a deploy
    has to keep meaning what it says.
    """

    def setUp(self) -> None:
        super().setUp()
        mcp_state.startup_connect = None
        self.addCleanup(setattr, mcp_state, "startup_connect", None)
        mcp_state.chatter = None
        self.addCleanup(setattr, mcp_state, "chatter", None)

    def test_a_spawned_server_leaves_the_port_alone_unless_asked(self) -> None:
        self.assertFalse(mcp_state.connect_on_start_wanted({}))
        self.assertFalse(mcp_state.connect_on_start_wanted({"BUDDY_CONNECT_ON_START": "0"}))
        self.assertTrue(mcp_state.connect_on_start_wanted({"BUDDY_CONNECT_ON_START": "1"}))

    def test_the_daemon_takes_the_port_by_default(self) -> None:
        # Holding the port is what the resident daemon is for. A daemon
        # that waited to be asked would leave the device silent until
        # some session happened to call `buddy_connect`.
        self.assertTrue(mcp_state.connect_on_start_wanted({}, default=True))

    def test_the_daemon_default_can_still_be_turned_off(self) -> None:
        # `buddy_deploy.py` and `esptool` need the port free, and
        # `config.toml` is where a machine says so once.
        self.assertFalse(
            mcp_state.connect_on_start_wanted({"BUDDY_CONNECT_ON_START": "0"}, default=True)
        )

    def test_it_opens_the_port(self) -> None:
        result = mcp_state.connect_on_start()
        self.assertTrue(result["ok"])
        self.assertEqual(result["port"], mcp_state.DEFAULT_PORT)
        self.assertIs(mcp_state.live_link(), StubLink.instances[0])

    def test_it_holds_the_device_lock_while_it_opens(self) -> None:
        # It runs on its own thread beside the first tool calls of the
        # session; taking the port from under one of them would cross
        # their acks.
        mcp_state.ResidentLink = _LockWatchingLink
        mcp_state.connect_on_start()
        self.assertEqual(StubLink.instances[0].lock_held, [True])

    def test_a_port_that_will_not_open_is_recorded_rather_than_raised(self) -> None:
        # Nobody is waiting on this thread, and a server that dies
        # because the board is unplugged is worse than a silent one.
        mcp_state.ResidentLink = _RefusingLink
        result = mcp_state.connect_on_start()
        self.assertFalse(result["ok"])
        self.assertIn("no such port", result["error"])
        self.assertIsNone(mcp_state.live_link())

    def test_the_attempt_shows_up_in_the_chatter_status(self) -> None:
        # `skipped_offline` on its own cannot say whether the port was
        # never taken or taken and then lost.
        mcp_state.connect_on_start()
        self.assertTrue(mcp_chatter_tools.buddy_chatter_status()["connect_on_start"]["ok"])

    def test_nothing_is_reported_when_it_did_not_run(self) -> None:
        self.assertNotIn("connect_on_start", mcp_chatter_tools.buddy_chatter_status())

    def test_a_disconnect_afterwards_leaves_the_port_free(self) -> None:
        # `buddy_disconnect` is what frees the port for buddy_deploy.py
        # and esptool. Nothing here may take it back.
        mcp_state.connect_on_start()
        buddy_mcp.buddy_disconnect()
        self.assertIsNone(mcp_state.live_link())
        self.assertEqual(len(StubLink.instances), 1)


if __name__ == "__main__":
    unittest.main()
