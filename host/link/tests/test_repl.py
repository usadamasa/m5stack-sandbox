"""Getting to the REPL, which needs a human to press a button.

The transfer itself is mpremote's and is not retested here. What is ours
is the wait loop, and it exists because the Buddy app calls
`micropython.kbd_intr(-1)`: the port opens, Ctrl-C does nothing, and the
only way back is BtnRST. Failing and telling the operator to run the
command again makes them do the work twice, and in an agent-driven
session it costs a whole round trip for something the port can report.
"""

from __future__ import annotations

import unittest
from unittest import mock

from mpremote.transport import TransportError

import device_repl
from device_repl import ReplError, connect_repl
from fake_repl import FakeRepl


class _FakeClock:
    """`time` with a monotonic that only moves when sleep is called."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class ConnectReplTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _FakeClock()
        real_time = device_repl.time
        device_repl.time = self.clock
        self.addCleanup(setattr, device_repl, "time", real_time)

    def test_returns_a_transport_in_the_raw_repl(self) -> None:
        made: list[FakeRepl] = []

        def factory(_port: str, _baud: int) -> FakeRepl:
            repl = FakeRepl()
            made.append(repl)
            return repl

        repl = connect_repl("/dev/null", factory=factory)
        self.assertIs(repl, made[0])
        self.assertTrue(made[0].in_raw_repl)
        self.assertEqual(len(made), 1)
        self.assertEqual(self.clock.now, 0.0)

    def test_never_soft_resets(self) -> None:
        # A soft reset re-runs boot.py and main.py, which relaunches the
        # UIFlow launcher — and we would only have to interrupt it again
        # a moment later. mpremote defaults to doing it; we do not want
        # it, so assert the argument rather than trusting the default.
        made: list[FakeRepl] = []

        def factory(_port: str, _baud: int) -> FakeRepl:
            made.append(FakeRepl())
            return made[-1]

        connect_repl("/dev/null", factory=factory)
        self.assertEqual(made[0].soft_resets, 0)

    def test_polls_past_a_port_that_is_not_there_yet(self) -> None:
        # A reset takes the device off the bus for a second or two, so
        # opening the port raises until it re-enumerates.
        attempts = {"n": 0}

        def factory(_port: str, _baud: int) -> FakeRepl:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise OSError(2, "No such file or directory")
            return FakeRepl()

        connect_repl("/dev/null", factory=factory)
        self.assertEqual(attempts["n"], 3)
        self.assertGreater(self.clock.now, 0.0)

    def test_polls_past_an_app_that_is_still_running(self) -> None:
        # The port opens fine while the app is up — it just will not
        # hand over a raw REPL, because the app turned Ctrl-C off.
        attempts = {"n": 0}

        class _Deaf(FakeRepl):
            def enter_raw_repl(self, soft_reset: bool = True, timeout_overall: int = 10) -> None:
                raise TransportError("could not enter raw repl")

        def factory(_port: str, _baud: int) -> FakeRepl:
            attempts["n"] += 1
            return FakeRepl() if attempts["n"] >= 3 else _Deaf()

        connect_repl("/dev/null", factory=factory)
        self.assertEqual(attempts["n"], 3)

    def test_rejects_a_link_that_handshakes_and_then_dies(self) -> None:
        # macOS presents the device node before the USB interface is
        # configured. A handle opened in that window has answered once
        # and then raised ENXIO on the next ioctl — which showed up as
        # the very first block killing the port, three runs in a row,
        # and read as a WiFi problem for a long time. The handshake
        # alone is not evidence the link executes code; one round trip
        # through it is.
        attempts = {"n": 0}

        class _Flaky(FakeRepl):
            def eval(self, expression: str, parse: bool = True) -> object:
                raise OSError(6, "Device not configured")

        def factory(_port: str, _baud: int) -> FakeRepl:
            attempts["n"] += 1
            return _Flaky() if attempts["n"] <= 2 else FakeRepl()

        repl = connect_repl("/dev/null", factory=factory)
        self.assertEqual(attempts["n"], 3)
        self.assertNotIsInstance(repl, _Flaky)

    def test_closes_the_transports_it_could_not_use(self) -> None:
        # Every attempt opens a fresh handle. Leaking the failed ones
        # would eventually exhaust file descriptors, and on macOS a
        # lingering open handle on a cu.* device blocks the next open.
        made: list[FakeRepl] = []
        attempts = {"n": 0}

        class _Deaf(FakeRepl):
            def enter_raw_repl(self, soft_reset: bool = True, timeout_overall: int = 10) -> None:
                raise TransportError("nope")

        def factory(_port: str, _baud: int) -> FakeRepl:
            attempts["n"] += 1
            made.append(FakeRepl() if attempts["n"] >= 3 else _Deaf())
            return made[-1]

        connect_repl("/dev/null", factory=factory)
        self.assertEqual([r.closed for r in made], [True, True, False])

    def test_announces_the_wait_once_and_only_when_waiting(self) -> None:
        # The message says "press BtnRST". Repeating it every poll is
        # noise, and printing it when nothing was needed is a lie.
        calls = {"n": 0}
        attempts = {"n": 0}

        class _Deaf(FakeRepl):
            def enter_raw_repl(self, soft_reset: bool = True, timeout_overall: int = 10) -> None:
                raise TransportError("nope")

        def factory(_port: str, _baud: int) -> FakeRepl:
            attempts["n"] += 1
            return FakeRepl() if attempts["n"] >= 4 else _Deaf()

        connect_repl(
            "/dev/null", factory=factory, on_wait=lambda: calls.__setitem__("n", calls["n"] + 1)
        )
        self.assertEqual(calls["n"], 1)

    def test_stays_quiet_when_no_wait_was_needed(self) -> None:
        calls = {"n": 0}
        connect_repl(
            "/dev/null",
            factory=lambda _p, _b: FakeRepl(),
            on_wait=lambda: calls.__setitem__("n", calls["n"] + 1),
        )
        self.assertEqual(calls["n"], 0)

    def test_gives_up_eventually_and_says_what_it_heard(self) -> None:
        # Nobody is coming to press the button. Waiting forever would
        # hang a CI run or an unattended session with no output.
        class _Deaf(FakeRepl):
            def enter_raw_repl(self, soft_reset: bool = True, timeout_overall: int = 10) -> None:
                raise TransportError("could not enter raw repl")

        with self.assertRaises(ReplError) as caught:
            connect_repl("/dev/null", timeout=10.0, factory=lambda _p, _b: _Deaf())
        message = str(caught.exception)
        self.assertIn("BtnRST", message)
        self.assertIn("could not enter raw repl", message)

    def test_does_not_let_mpremote_print_over_the_poll(self) -> None:
        # mpremote prints whatever it read when the handshake fails.
        # That is right for a one-shot CLI and wrong inside a loop that
        # expects to fail for three minutes.
        class _Noisy(FakeRepl):
            def enter_raw_repl(self, soft_reset: bool = True, timeout_overall: int = 10) -> None:
                print(b"\x00\x00 garbage")
                raise TransportError("could not enter raw repl")

        attempts = {"n": 0}

        def factory(_port: str, _baud: int) -> FakeRepl:
            attempts["n"] += 1
            return FakeRepl() if attempts["n"] >= 3 else _Noisy()

        with mock.patch("sys.stdout") as out:
            connect_repl("/dev/null", factory=factory)
        out.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
