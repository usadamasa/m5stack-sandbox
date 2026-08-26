"""起動口のテスト — ログ、transport の選択、そして後始末。

実機もネットワークも要らない。`server.run` と uvicorn は差し替え、socket は
一時ディレクトリへ逃がしてある。
"""

import logging
import os
import signal
import unittest
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest import mock

import buddy_mcp
import buddy_mcp_serve
import mcp_state
from mcp_stubs import McpTestCase, StubLink


class ShutdownTest(McpTestCase):
    """What the daemon lets go of on the way out.

    Nothing did this before: `main` returned as soon as the server
    stopped, so a daemon that shut down cleanly still left its datagram
    socket behind. A stale socket is survivable — the next start unlinks
    it — but it also means `buddy-mcpd stop` cannot be told apart from
    a daemon that was killed.
    """

    def setUp(self) -> None:
        super().setUp()
        mcp_state.chatter = None
        self.addCleanup(setattr, mcp_state, "chatter", None)

    def _run_main(self) -> None:
        with (
            mock.patch.object(mcp_state.server, "run"),
            mock.patch.object(mcp_state, "connect_on_start_wanted", return_value=False),
            TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"BUDDY_CHATTER_SOCKET": f"{tmp}/probe.sock"}),
        ):
            self.sock = Path(tmp) / "probe.sock"
            buddy_mcp_serve.main([])

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
            mock.patch.object(mcp_state.server, "run"),
            mock.patch.object(mcp_state, "connect_on_start_wanted", return_value=False),
            mock.patch.object(buddy_mcp_serve.signal, "signal", side_effect=record),
        ):
            buddy_mcp_serve.main([])
        self.assertEqual(
            [s for s, _ in installed], [signal.SIGTERM, signal.SIGINT], "both, and only these two"
        )
        self.assertEqual({h for _, h in installed}, {buddy_mcp_serve.shutdown_on_signal})

    def test_the_handler_cleans_up_then_dies_the_way_it_was_asked(self) -> None:
        # Not `sys.exit(0)`: a supervisor reading the exit status should
        # see "terminated by SIGTERM", not a clean return.
        calls: list[str] = []

        def note(name: str) -> Callable[..., None]:
            def record(*_args: object) -> None:
                calls.append(name)

            return record

        with (
            mock.patch.object(buddy_mcp_serve, "_shutdown", side_effect=note("clean")),
            mock.patch.object(buddy_mcp_serve.signal, "signal", side_effect=note("default")),
            mock.patch.object(buddy_mcp_serve.signal, "raise_signal", side_effect=note("raise")),
        ):
            buddy_mcp_serve.shutdown_on_signal(signal.SIGTERM)
        self.assertEqual(calls, ["clean", "default", "raise"])

    def test_shutdown_still_runs_when_the_server_raises(self) -> None:
        with (
            mock.patch.object(mcp_state.server, "run", side_effect=RuntimeError("bind failed")),
            mock.patch.object(mcp_state, "connect_on_start_wanted", return_value=False),
            self.assertRaises(RuntimeError),
        ):
            buddy_mcp_serve.main([])
        self.assertFalse(mcp_state.chatter_service().running)


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
        buddy_mcp_serve.configure_logging()
        for name, spec in self.formatters.items():
            with self.subTest(formatter=name):
                self.assertTrue(spec["fmt"].startswith("%(asctime)s "))
                self.assertEqual(spec["datefmt"], buddy_mcp_serve.LOG_DATEFMT)

    def test_configuring_twice_does_not_stack_timestamps(self) -> None:
        # `buddy-mcpd restart` re-runs this in a fresh process, but a
        # test run — or any future caller — should not be able to end up
        # with two of them.
        buddy_mcp_serve.configure_logging()
        buddy_mcp_serve.configure_logging()
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
        buddy_mcp_serve.configure_logging()
        formats = [h.formatter._fmt for h in root.handlers if h.formatter is not None]
        self.assertIn(buddy_mcp_serve.LOG_FORMAT, formats)

    def test_the_run_announces_itself(self) -> None:
        # Which device and which transport, at the top of every run:
        # the log is appended to, so a line without that context cannot
        # be attributed to a run at all.
        with (
            mock.patch.object(mcp_state.server, "run"),
            mock.patch.object(mcp_state, "connect_on_start_wanted", return_value=False),
            mock.patch.object(buddy_mcp_serve, "_shutdown"),
            self.assertLogs("buddy", level="INFO") as caught,
        ):
            buddy_mcp_serve.main([])
        self.assertTrue(any("starting" in line for line in caught.output), caught.output)
        self.assertTrue(any(mcp_state.DEFAULT_PORT in line for line in caught.output))
        self.assertTrue(any("stopped" in line for line in caught.output), caught.output)


class ServeHttpTest(unittest.TestCase):
    """Why the HTTP transport is built here instead of via `server.run`.

    `run_streamable_http_async` builds its own `uvicorn.Config` and
    passes only host, port and log level — there is no way to reach
    `timeout_graceful_shutdown` through it. Without that timeout,
    `Server._wait_tasks_to_complete` ends in `await server.wait_closed()`
    with no escape for `force_exit`, so a stop that lands while a
    request is in flight hangs until the client goes away. The daemon is
    then SIGKILLed, and neither `main`'s `finally` nor the signal
    handler runs — the serial port and the socket are left behind.
    """

    def _run(self) -> tuple[mock.MagicMock, mock.MagicMock]:
        with (
            mock.patch.object(mcp_state.server, "streamable_http_app") as build,
            mock.patch("uvicorn.Config") as config,
            mock.patch("uvicorn.Server"),
        ):
            buddy_mcp_serve.serve_http({"host": "127.0.0.1", "port": 8787, "stateless_http": True})
        return build, config

    def test_the_graceful_shutdown_is_bounded(self) -> None:
        _, config = self._run()
        self.assertEqual(
            config.call_args.kwargs["timeout_graceful_shutdown"],
            mcp_state.SHUTDOWN_TIMEOUT,
        )

    def test_the_app_keeps_the_settings_the_transport_asked_for(self) -> None:
        build, config = self._run()
        self.assertTrue(build.call_args.kwargs["stateless_http"])
        self.assertEqual(build.call_args.kwargs["streamable_http_path"], mcp_state.HTTP_PATH)
        self.assertEqual(config.call_args.kwargs["port"], 8787)
        self.assertEqual(config.call_args.kwargs["host"], "127.0.0.1")

    def test_the_bounded_wait_leaves_room_for_the_cleanup(self) -> None:
        # `buddy-mcpd` escalates to SIGKILL after TERM_GRACE. If uvicorn
        # can still be inside its bounded wait by then, the timeout buys
        # nothing — the daemon dies before its own cleanup runs.
        import buddy_mcpd

        self.assertLess(
            mcp_state.SHUTDOWN_TIMEOUT + 1.0,
            buddy_mcpd.TERM_GRACE,
            "the wait plus the ~1s cleanup must finish inside the supervisor's patience",
        )


class TransportTest(unittest.TestCase):
    """How the process decides to listen. The daemon's half of it.

    `stdio` is still the default: one client, spawned and owned by it.
    The resident daemon asks for HTTP instead, which is what lets more
    than one session share one serial port.
    """

    def test_stdio_is_the_default(self) -> None:
        self.assertEqual(buddy_mcp_serve.transport_options([], {}), ("stdio", {}))

    def test_http_binds_to_the_loopback_on_the_agreed_port(self) -> None:
        name, opts = buddy_mcp_serve.transport_options(["--http"], {})
        self.assertEqual(name, "streamable-http")
        # Not 0.0.0.0: a USB device on this desk has no reason to be
        # reachable from the network.
        self.assertEqual(opts["host"], "127.0.0.1")
        self.assertEqual(opts["port"], mcp_state.DEFAULT_HTTP_PORT)

    def test_the_session_is_not_kept_on_the_server(self) -> None:
        # The whole point of the daemon is that it can be restarted
        # mid-session. A server-side session id would 404 every client
        # that was connected before the restart.
        _, opts = buddy_mcp_serve.transport_options(["--http"], {})
        self.assertTrue(opts["stateless_http"])

    def test_the_port_is_configurable(self) -> None:
        _, opts = buddy_mcp_serve.transport_options(["--http"], {"BUDDY_HTTP_PORT": "9001"})
        self.assertEqual(opts["port"], 9001)

    def test_the_flag_beats_the_environment(self) -> None:
        _, opts = buddy_mcp_serve.transport_options(
            ["--http", "--port", "9002"], {"BUDDY_HTTP_PORT": "9001"}
        )
        self.assertEqual(opts["port"], 9002)

    def test_an_unreadable_port_falls_back_rather_than_refusing_to_start(self) -> None:
        # The registration in `.mcp.json` is a static URL on the default
        # port; a typo in the config file that stopped the daemon
        # starting would be worse than one it ignores and logs.
        _, opts = buddy_mcp_serve.transport_options(["--http"], {"BUDDY_HTTP_PORT": "nonsense"})
        self.assertEqual(opts["port"], mcp_state.DEFAULT_HTTP_PORT)


if __name__ == "__main__":
    unittest.main()
