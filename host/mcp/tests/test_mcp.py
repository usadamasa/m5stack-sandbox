"""Tests for the MCP wrapper around ResidentLink.

The tools themselves are thin, but the link cache underneath them is
not: it has to survive a port change, a disconnect, and a reconnect
without leaving a second reader thread on a port nobody closed. That is
what is exercised here, against a stub link rather than hardware.

Since the chatter arrived there is a second thing worth pinning down:
every tool holds the device lock for the whole of its exchange, and the
chatter's link provider never opens a port of its own.
"""

import logging
import os
import signal
import unittest
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, ClassVar, cast
from unittest import mock

import buddy_mcp
import buddy_verbs
from buddy_wire import Message


def _recording_speak(store: list[str]) -> Callable[..., Message]:
    """A stand-in for `buddy_verbs.speak` that records the text and acks.

    A plain lambda cannot carry parameter annotations, so `mock.patch.object`
    would otherwise see `side_effect` as an untyped callable.
    """

    def fake(_link: object, text: str, **_kwargs: object) -> Message:
        store.append(text)
        return {"ok": True}

    return fake


def _silent_speak(store: list[str]) -> Callable[..., None]:
    """A stand-in for `buddy_verbs.speak` that records nothing was said."""

    def fake(*_args: object, **_kwargs: object) -> None:
        store.append("")

    return fake


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
        self.interrupts = 0
        # Merged into every ack, so a test can make the device claim it
        # just entered debug mode.
        self.ack_extra: Message = {}
        StubLink.instances.append(self)

    def interrupt(self) -> None:
        self.interrupts += 1
        self.lock_held.append(buddy_mcp._device_lock.locked())

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def request(self, obj: Message, expect: str, timeout: float = 5.0) -> Message:
        self.requests.append((obj, expect))
        # Recorded at the moment the device is being talked to, which is
        # the only place the answer is meaningful.
        self.lock_held.append(buddy_mcp._device_lock.locked())
        return {"ack": expect, "ok": True, **self.ack_extra}

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


class DebugToolTest(_McpTestCase):
    """The tools that stand in for the REPL the running app took away."""

    def test_debug_prefixes_the_verb(self) -> None:
        result = buddy_mcp.buddy_debug("mem")
        self.assertEqual(StubLink.instances[0].requests, [({"cmd": "dbg.mem"}, "dbg.mem")])
        self.assertEqual(result["ack"]["ack"], "dbg.mem")

    def test_debug_carries_source_for_eval(self) -> None:
        buddy_mcp.buddy_debug("eval", src="gc.mem_free()")
        obj, _expect = StubLink.instances[0].requests[0]
        self.assertEqual(obj["src"], "gc.mem_free()")

    def test_debug_returns_the_log_lines_too(self) -> None:
        # dbg.frag's heap map and a failed dbg.eval's traceback never
        # appear in the ack — they are printed. A tool that returned only
        # the ack would be reporting "ok: true" and nothing else.
        buddy_mcp.buddy_connect()
        StubLink.instances[0].queued_logs = [b"GC: total: 131072, used: 41328, free: 89744"]
        result = buddy_mcp.buddy_debug("frag", settle=0.0)
        self.assertEqual(result["logs"], ["GC: total: 131072, used: 41328, free: 89744"])

    def test_debug_names_the_valid_ops_on_a_typo(self) -> None:
        result = buddy_mcp.buddy_debug("memory")
        self.assertFalse(result["ok"])
        self.assertIn("mem", result["error"])
        self.assertEqual(StubLink.instances, [])

    def test_entering_debug_mode_is_announced_out_loud(self) -> None:
        # The device sets `entered` on the frame that imported its debug
        # module. Only it knows which one that was.
        spoken: list[str] = []
        with mock.patch.object(buddy_verbs, "speak", side_effect=_recording_speak(spoken)):
            buddy_mcp.buddy_connect()
            StubLink.instances[0].ack_extra = {"entered": True}
            result = buddy_mcp.buddy_debug("mem", settle=0.0)
        self.assertEqual(spoken, [buddy_verbs.DEBUG_ENTER_TEXT])
        self.assertTrue(result["announced"])

    def test_later_calls_say_nothing(self) -> None:
        spoken: list[str] = []
        with mock.patch.object(buddy_verbs, "speak", side_effect=_silent_speak(spoken)):
            result = buddy_mcp.buddy_debug("mem", settle=0.0)
        self.assertEqual(spoken, [])
        self.assertFalse(result["announced"])

    def test_announce_false_keeps_it_quiet(self) -> None:
        spoken: list[str] = []
        with mock.patch.object(buddy_verbs, "speak", side_effect=_silent_speak(spoken)):
            buddy_mcp.buddy_connect()
            StubLink.instances[0].ack_extra = {"entered": True}
            buddy_mcp.buddy_debug("mem", announce=False, settle=0.0)
        self.assertEqual(spoken, [])

    def test_a_silent_engine_does_not_fail_the_inspection(self) -> None:
        with mock.patch.object(buddy_verbs, "speak", side_effect=OSError("engine unreachable")):
            buddy_mcp.buddy_connect()
            StubLink.instances[0].ack_extra = {"entered": True}
            result = buddy_mcp.buddy_debug("mem", settle=0.0)
        self.assertTrue(result["ok"])
        self.assertFalse(result["announced"])

    def test_debug_holds_the_lock_while_it_talks(self) -> None:
        buddy_mcp.buddy_debug("mem")
        self.assertEqual(StubLink.instances[0].lock_held, [True])

    def test_interrupt_sends_one_ctrl_c(self) -> None:
        buddy_mcp.buddy_connect()
        StubLink.instances[0].queued_logs = [b"claude_buddy: at the REPL."]
        result = buddy_mcp.buddy_interrupt(settle=0.0)
        self.assertEqual(StubLink.instances[0].interrupts, 1)
        self.assertEqual(result["logs"], ["claude_buddy: at the REPL."])

    def test_interrupt_does_not_open_a_port_of_its_own(self) -> None:
        # Interrupting a device nobody is connected to would open the
        # port purely to send a byte into the dark, and then hold it.
        result = buddy_mcp.buddy_interrupt(settle=0.0)
        self.assertFalse(result["ok"])
        self.assertEqual(StubLink.instances, [])

    def test_interrupt_holds_the_lock(self) -> None:
        buddy_mcp.buddy_connect()
        buddy_mcp.buddy_interrupt(settle=0.0)
        self.assertEqual(StubLink.instances[0].lock_held, [True])


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

    def test_the_model_and_the_effort_can_be_retuned_without_a_restart(self) -> None:
        status = buddy_mcp.buddy_chatter_start(model="haiku", effort="high", batch=3)
        cfg = buddy_mcp._chatter_service().cfg
        self.assertEqual(cfg.model, "haiku")
        self.assertEqual(cfg.effort, "high")
        self.assertEqual(cfg.batch, 3)
        # Reported, or "which model is writing this" is unanswerable
        # from outside the process.
        self.assertEqual(status["model"], "haiku")
        self.assertEqual(status["effort"], "high")

    def test_an_empty_model_keeps_the_configured_one(self) -> None:
        buddy_mcp.buddy_chatter_start(model="haiku")
        buddy_mcp.buddy_chatter_start(effort="high")
        cfg = buddy_mcp._chatter_service().cfg
        self.assertEqual(cfg.model, "haiku")

    def test_the_default_model_is_sonnet(self) -> None:
        self.assertEqual(buddy_mcp.buddy_chatter_status()["model"], "sonnet")


class _LockWatchingLink(StubLink):
    """Records whether the device lock was held as the port opened."""

    def connect(self) -> None:
        self.lock_held.append(buddy_mcp._device_lock.locked())
        super().connect()


class _RefusingLink(StubLink):
    """A port that is not there — the board unplugged, or already taken."""

    def connect(self) -> None:
        raise OSError("no such port")


class ConnectOnStartTest(_McpTestCase):
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
        buddy_mcp._startup_connect = None
        self.addCleanup(setattr, buddy_mcp, "_startup_connect", None)
        buddy_mcp._chatter = None
        self.addCleanup(setattr, buddy_mcp, "_chatter", None)

    def test_a_spawned_server_leaves_the_port_alone_unless_asked(self) -> None:
        self.assertFalse(buddy_mcp._connect_on_start_wanted({}))
        self.assertFalse(buddy_mcp._connect_on_start_wanted({"BUDDY_CONNECT_ON_START": "0"}))
        self.assertTrue(buddy_mcp._connect_on_start_wanted({"BUDDY_CONNECT_ON_START": "1"}))

    def test_the_daemon_takes_the_port_by_default(self) -> None:
        # Holding the port is what the resident daemon is for. A daemon
        # that waited to be asked would leave the device silent until
        # some session happened to call `buddy_connect`.
        self.assertTrue(buddy_mcp._connect_on_start_wanted({}, default=True))

    def test_the_daemon_default_can_still_be_turned_off(self) -> None:
        # `buddy_deploy.py` and `esptool` need the port free, and
        # `config.toml` is where a machine says so once.
        self.assertFalse(
            buddy_mcp._connect_on_start_wanted({"BUDDY_CONNECT_ON_START": "0"}, default=True)
        )

    def test_it_opens_the_port(self) -> None:
        result = buddy_mcp._connect_on_start()
        self.assertTrue(result["ok"])
        self.assertEqual(result["port"], buddy_mcp.DEFAULT_PORT)
        self.assertIs(buddy_mcp._live_link(), StubLink.instances[0])

    def test_it_holds_the_device_lock_while_it_opens(self) -> None:
        # It runs on its own thread beside the first tool calls of the
        # session; taking the port from under one of them would cross
        # their acks.
        buddy_mcp.ResidentLink = _LockWatchingLink  # pyright: ignore[reportAttributeAccessIssue]
        buddy_mcp._connect_on_start()
        self.assertEqual(StubLink.instances[0].lock_held, [True])

    def test_a_port_that_will_not_open_is_recorded_rather_than_raised(self) -> None:
        # Nobody is waiting on this thread, and a server that dies
        # because the board is unplugged is worse than a silent one.
        buddy_mcp.ResidentLink = _RefusingLink  # pyright: ignore[reportAttributeAccessIssue]
        result = buddy_mcp._connect_on_start()
        self.assertFalse(result["ok"])
        self.assertIn("no such port", result["error"])
        self.assertIsNone(buddy_mcp._live_link())

    def test_the_attempt_shows_up_in_the_chatter_status(self) -> None:
        # `skipped_offline` on its own cannot say whether the port was
        # never taken or taken and then lost.
        buddy_mcp._connect_on_start()
        self.assertTrue(buddy_mcp.buddy_chatter_status()["connect_on_start"]["ok"])

    def test_nothing_is_reported_when_it_did_not_run(self) -> None:
        self.assertNotIn("connect_on_start", buddy_mcp.buddy_chatter_status())

    def test_a_disconnect_afterwards_leaves_the_port_free(self) -> None:
        # `buddy_disconnect` is what frees the port for buddy_deploy.py
        # and esptool. Nothing here may take it back.
        buddy_mcp._connect_on_start()
        buddy_mcp.buddy_disconnect()
        self.assertIsNone(buddy_mcp._live_link())
        self.assertEqual(len(StubLink.instances), 1)


class ShutdownTest(_McpTestCase):
    """What the daemon lets go of on the way out.

    Nothing did this before: `main` returned as soon as the server
    stopped, so a daemon that shut down cleanly still left its datagram
    socket behind. A stale socket is survivable — the next start unlinks
    it — but it also means `buddy-mcpd stop` cannot be told apart from
    a daemon that was killed.
    """

    def setUp(self) -> None:
        super().setUp()
        buddy_mcp._chatter = None
        self.addCleanup(setattr, buddy_mcp, "_chatter", None)

    def _run_main(self) -> None:
        with (
            mock.patch.object(buddy_mcp.server, "run"),
            mock.patch.object(buddy_mcp, "_connect_on_start_wanted", return_value=False),
            TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"BUDDY_CHATTER_SOCKET": f"{tmp}/probe.sock"}),
        ):
            self.sock = Path(tmp) / "probe.sock"
            buddy_mcp.main([])

    def test_the_chatter_socket_does_not_outlive_the_daemon(self) -> None:
        self._run_main()
        self.assertFalse(self.sock.exists(), "a clean shutdown must unlink its own socket")

    def test_the_port_is_released_on_the_way_out(self) -> None:
        # The next thing to want the port is usually `buddy_deploy.py`,
        # and it should not have to wait for the kernel to notice.
        buddy_mcp.buddy_connect()
        self._run_main()
        self.assertFalse(StubLink.instances[0].connected)

    def test_the_signal_handlers_are_installed_before_the_server_runs(self) -> None:
        # uvicorn borrows both signals for the length of `serve()` and
        # restores whatever was there beforehand on the way out — so
        # these have to be in place before it starts, not after.
        installed: list[tuple[int, object]] = []

        def record(sig: int, handler: object) -> None:
            installed.append((sig, handler))

        with (
            mock.patch.object(buddy_mcp.server, "run"),
            mock.patch.object(buddy_mcp, "_connect_on_start_wanted", return_value=False),
            mock.patch.object(buddy_mcp.signal, "signal", side_effect=record),
        ):
            buddy_mcp.main([])
        self.assertEqual(
            [s for s, _ in installed], [signal.SIGTERM, signal.SIGINT], "both, and only these two"
        )
        self.assertEqual({h for _, h in installed}, {buddy_mcp.shutdown_on_signal})

    def test_the_handler_cleans_up_then_dies_the_way_it_was_asked(self) -> None:
        # Not `sys.exit(0)`: a supervisor reading the exit status should
        # see "terminated by SIGTERM", not a clean return.
        calls: list[str] = []

        def note(name: str) -> Callable[..., None]:
            def record(*_args: object) -> None:
                calls.append(name)

            return record

        with (
            mock.patch.object(buddy_mcp, "_shutdown", side_effect=note("clean")),
            mock.patch.object(buddy_mcp.signal, "signal", side_effect=note("default")),
            mock.patch.object(buddy_mcp.signal, "raise_signal", side_effect=note("raise")),
        ):
            buddy_mcp.shutdown_on_signal(signal.SIGTERM)
        self.assertEqual(calls, ["clean", "default", "raise"])

    def test_shutdown_still_runs_when_the_server_raises(self) -> None:
        with (
            mock.patch.object(buddy_mcp.server, "run", side_effect=RuntimeError("bind failed")),
            mock.patch.object(buddy_mcp, "_connect_on_start_wanted", return_value=False),
            self.assertRaises(RuntimeError),
        ):
            buddy_mcp.main([])
        self.assertFalse(buddy_mcp._chatter_service().running)


class LoggingTest(unittest.TestCase):
    """The daemon's log is the only account of what happened while
    nobody was attached, and it is appended to across restarts. Without
    a timestamp on every line there is no way to tell which run a line
    belongs to — and the first question asked of it is always "when did
    it stop".
    """

    def setUp(self) -> None:
        from uvicorn.config import LOGGING_CONFIG

        self.formatters = cast("dict[str, dict[str, Any]]", LOGGING_CONFIG["formatters"])
        original = {name: dict(spec) for name, spec in self.formatters.items()}
        self.addCleanup(self.formatters.update, original)

    def test_every_uvicorn_formatter_gets_a_timestamp(self) -> None:
        buddy_mcp.configure_logging()
        for name, spec in self.formatters.items():
            with self.subTest(formatter=name):
                self.assertTrue(spec["fmt"].startswith("%(asctime)s "))
                self.assertEqual(spec["datefmt"], buddy_mcp.LOG_DATEFMT)

    def test_configuring_twice_does_not_stack_timestamps(self) -> None:
        # `buddy-mcpd restart` re-runs this in a fresh process, but a
        # test run — or any future caller — should not be able to end up
        # with two of them.
        buddy_mcp.configure_logging()
        buddy_mcp.configure_logging()
        for name, spec in self.formatters.items():
            with self.subTest(formatter=name):
                self.assertEqual(spec["fmt"].count("%(asctime)s"), 1)

    def test_the_root_handler_is_replaced_not_skipped(self) -> None:
        # `basicConfig` is a no-op when the root logger already has a
        # handler, and by the time this runs something has usually
        # installed one. Without `force` the daemon's own lines and the
        # MCP SDK's session-lifecycle lines come out bare while
        # uvicorn's are timestamped — which is exactly what shipped.
        root = logging.getLogger()
        original = list(root.handlers)

        def restore() -> None:
            root.handlers[:] = original

        self.addCleanup(restore)
        root.handlers[:] = [logging.NullHandler()]
        buddy_mcp.configure_logging()
        formats = [
            h.formatter._fmt  # pyright: ignore[reportPrivateUsage]
            for h in root.handlers
            if h.formatter is not None
        ]
        self.assertIn(buddy_mcp.LOG_FORMAT, formats)

    def test_the_run_announces_itself(self) -> None:
        # Which device and which transport, at the top of every run:
        # the log is appended to, so a line without that context cannot
        # be attributed to a run at all.
        with (
            mock.patch.object(buddy_mcp.server, "run"),
            mock.patch.object(buddy_mcp, "_connect_on_start_wanted", return_value=False),
            mock.patch.object(buddy_mcp, "_shutdown"),
            self.assertLogs("buddy", level="INFO") as caught,
        ):
            buddy_mcp.main([])
        self.assertTrue(any("starting" in line for line in caught.output), caught.output)
        self.assertTrue(any(buddy_mcp.DEFAULT_PORT in line for line in caught.output))
        self.assertTrue(any("stopped" in line for line in caught.output), caught.output)


class TransportTest(unittest.TestCase):
    """How the process decides to listen. The daemon's half of it.

    `stdio` is still the default: one client, spawned and owned by it.
    The resident daemon asks for HTTP instead, which is what lets more
    than one session share one serial port.
    """

    def test_stdio_is_the_default(self) -> None:
        self.assertEqual(buddy_mcp.transport_options([], {}), ("stdio", {}))

    def test_http_binds_to_the_loopback_on_the_agreed_port(self) -> None:
        name, opts = buddy_mcp.transport_options(["--http"], {})
        self.assertEqual(name, "streamable-http")
        # Not 0.0.0.0: a USB device on this desk has no reason to be
        # reachable from the network.
        self.assertEqual(opts["host"], "127.0.0.1")
        self.assertEqual(opts["port"], buddy_mcp.DEFAULT_HTTP_PORT)

    def test_the_session_is_not_kept_on_the_server(self) -> None:
        # The whole point of the daemon is that it can be restarted
        # mid-session. A server-side session id would 404 every client
        # that was connected before the restart.
        _, opts = buddy_mcp.transport_options(["--http"], {})
        self.assertTrue(opts["stateless_http"])

    def test_the_port_is_configurable(self) -> None:
        _, opts = buddy_mcp.transport_options(["--http"], {"BUDDY_HTTP_PORT": "9001"})
        self.assertEqual(opts["port"], 9001)

    def test_the_flag_beats_the_environment(self) -> None:
        _, opts = buddy_mcp.transport_options(
            ["--http", "--port", "9002"], {"BUDDY_HTTP_PORT": "9001"}
        )
        self.assertEqual(opts["port"], 9002)

    def test_an_unreadable_port_falls_back_rather_than_refusing_to_start(self) -> None:
        # The registration in `.mcp.json` is a static URL on the default
        # port; a typo in the config file that stopped the daemon
        # starting would be worse than one it ignores and logs.
        _, opts = buddy_mcp.transport_options(["--http"], {"BUDDY_HTTP_PORT": "nonsense"})
        self.assertEqual(opts["port"], buddy_mcp.DEFAULT_HTTP_PORT)


if __name__ == "__main__":
    unittest.main()
