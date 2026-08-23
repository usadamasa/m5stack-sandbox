"""The daemon supervisor: one process holding the port, restartable.

The thing being pinned down here is not process management in general
but the two failures that actually bite. A second daemon started while
the first still holds the serial port fails in a way nobody reads (the
port is simply busy), and a pid file left behind by a crash makes
`start` refuse forever. Both are handled by looking at the pid rather
than by trusting the file.
"""

import os
import signal
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import buddy_mcpd


class _StateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env = {"XDG_STATE_HOME": self._tmp.name, "HOME": self._tmp.name}
        self.pid_file = Path(self._tmp.name) / "buddy" / "buddy-mcpd.pid"

    def write_pid(self, pid: int) -> None:
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.pid_file.write_text(f"{pid}\n", encoding="utf-8")


class PidFileTests(_StateTestCase):
    def test_no_file_is_no_pid(self) -> None:
        self.assertIsNone(buddy_mcpd.read_pid(self.env))

    def test_a_pid_survives_the_round_trip(self) -> None:
        self.write_pid(4321)
        self.assertEqual(buddy_mcpd.read_pid(self.env), 4321)

    def test_a_junk_file_reads_as_no_pid(self) -> None:
        # A truncated write should look like "not running", not like a
        # crash in the supervisor.
        self.write_pid(0)
        self.pid_file.write_text("not a number", encoding="utf-8")
        self.assertIsNone(buddy_mcpd.read_pid(self.env))


class StartTests(_StateTestCase):
    def _spawn(self, calls: list[list[str]], pid: int = 999) -> buddy_mcpd.Spawn:
        def spawn(argv: list[str], _log: Path) -> int:
            calls.append(argv)
            return pid

        return spawn

    def test_start_records_the_pid_it_spawned(self) -> None:
        calls: list[list[str]] = []
        result = buddy_mcpd.start(self.env, spawn=self._spawn(calls), running=lambda _: False)
        self.assertTrue(result["started"])
        self.assertEqual(result["pid"], 999)
        self.assertEqual(buddy_mcpd.read_pid(self.env), 999)

    def test_the_daemon_is_asked_for_http(self) -> None:
        calls: list[list[str]] = []
        buddy_mcpd.start(self.env, spawn=self._spawn(calls), running=lambda _: False)
        self.assertIn("--http", calls[0])

    def test_a_live_daemon_is_not_started_twice(self) -> None:
        # The serial port takes one owner. A second daemon would fail on
        # the port rather than on the pid, which is far harder to read.
        self.write_pid(4321)
        calls: list[list[str]] = []
        result = buddy_mcpd.start(self.env, spawn=self._spawn(calls), running=lambda _: True)
        self.assertFalse(result["started"])
        self.assertEqual(result["pid"], 4321)
        self.assertEqual(calls, [])

    def test_a_stale_pid_file_does_not_block_a_start(self) -> None:
        # What a crash leaves behind. Trusting the file here would mean
        # the daemon can never be started again without a manual delete.
        self.write_pid(4321)
        calls: list[list[str]] = []
        result = buddy_mcpd.start(self.env, spawn=self._spawn(calls), running=lambda _: False)
        self.assertTrue(result["started"])
        self.assertEqual(len(calls), 1)

    def test_the_state_directory_is_created(self) -> None:
        buddy_mcpd.start(self.env, spawn=self._spawn([]), running=lambda _: False)
        self.assertTrue(self.pid_file.parent.is_dir())


class StopTests(_StateTestCase):
    def test_stopping_nothing_is_not_an_error(self) -> None:
        result = buddy_mcpd.stop(self.env, kill=lambda _p, _s: None, running=lambda _: False)
        self.assertFalse(result["stopped"])
        self.assertEqual(result["note"], "was not running")

    def test_a_term_is_sent_and_the_file_removed(self) -> None:
        self.write_pid(4321)
        sent: list[tuple[int, int]] = []
        alive = iter([True, False])
        result = buddy_mcpd.stop(
            self.env,
            kill=lambda p, s: sent.append((p, s)),
            running=lambda _: next(alive, False),
        )
        self.assertTrue(result["stopped"])
        self.assertEqual(sent[0], (4321, signal.SIGTERM))
        self.assertFalse(self.pid_file.exists())

    def test_the_second_signal_is_an_interrupt(self) -> None:
        # uvicorn takes the first signal as "finish serving what you
        # have" and then waits for open HTTP connections to close —
        # which a session that is still attached never does. Only a
        # second *SIGINT* sets `force_exit`; a second SIGTERM falls into
        # the else branch and just re-sets `should_exit`, so the wait
        # continues and every stop ends in SIGKILL. That is what the
        # log's "(CTRL+C to force quit)" is telling us.
        self.write_pid(4321)
        sent: list[tuple[int, int]] = []
        alive = iter([True, True, False])
        result = buddy_mcpd.stop(
            self.env,
            kill=lambda p, s: sent.append((p, s)),
            running=lambda _: next(alive, False),
            grace=0.0,
        )
        self.assertEqual([s for _, s in sent], [signal.SIGTERM, signal.SIGINT])
        self.assertFalse(result["forced"], "the interrupt is the normal path, not force")

    def test_a_process_that_ignores_both_signals_is_killed(self) -> None:
        self.write_pid(4321)
        sent: list[tuple[int, int]] = []
        result = buddy_mcpd.stop(
            self.env,
            kill=lambda p, s: sent.append((p, s)),
            running=lambda _: True,
            grace=0.0,
        )
        self.assertEqual([s for _, s in sent], [signal.SIGTERM, signal.SIGINT, signal.SIGKILL])
        self.assertTrue(result["stopped"])
        self.assertTrue(result["forced"])

    def test_a_stale_file_is_cleared_rather_than_signalled(self) -> None:
        self.write_pid(4321)
        sent: list[tuple[int, int]] = []
        result = buddy_mcpd.stop(
            self.env, kill=lambda p, s: sent.append((p, s)), running=lambda _: False
        )
        self.assertEqual(sent, [])
        self.assertFalse(self.pid_file.exists())
        self.assertEqual(result["note"], "was not running")


class StatusTests(_StateTestCase):
    def test_status_says_where_to_look(self) -> None:
        status = buddy_mcpd.status(self.env, running=lambda _: False)
        self.assertFalse(status["running"])
        self.assertTrue(status["log"].endswith("buddy/buddy-mcpd.log"))
        self.assertTrue(status["socket"].endswith("buddy/chatter.sock"))

    def test_status_prints_the_url_it_serves(self) -> None:
        # The registration in `.mcp.json` is a static URL. If the
        # configured port has drifted from it, this is where that shows.
        status = buddy_mcpd.status({**self.env, "BUDDY_HTTP_PORT": "9001"}, running=lambda _: False)
        self.assertEqual(status["url"], "http://127.0.0.1:9001/mcp")

    def test_status_names_the_serial_port_it_would_open(self) -> None:
        # "which device" is half of every question asked of this thing,
        # and an empty field would read as "none configured".
        self.assertTrue(buddy_mcpd.status(self.env, running=lambda _: False)["port"])

    def test_a_live_daemon_reports_its_pid(self) -> None:
        self.write_pid(4321)
        status = buddy_mcpd.status(self.env, running=lambda _: True)
        self.assertTrue(status["running"])
        self.assertEqual(status["pid"], 4321)


class IsRunningTests(unittest.TestCase):
    def test_this_process_is_running(self) -> None:
        self.assertTrue(buddy_mcpd.is_running(os.getpid()))

    def test_a_pid_nobody_owns_is_not(self) -> None:
        # 2^22 is above the default pid_max everywhere this runs.
        self.assertFalse(buddy_mcpd.is_running(4_194_303))


if __name__ == "__main__":
    unittest.main()
