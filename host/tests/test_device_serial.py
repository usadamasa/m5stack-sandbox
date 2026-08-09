"""Inbound framing tests for the device-side transport.

`device/buddy_serial.py` runs on MicroPython, but its line classifier is
plain Python and is the piece most likely to be wrong in a way that only
shows up as "the device ignored me". Exercising it here means a framing
regression fails in CI instead of on the bench.

Only `_handle_line` is covered: `poll()` reads the real stdin, which a
unit test has no business touching.
"""

import unittest

import buddy_serial
from buddy_serial import _SENTINEL, BuddySerial


class _FakeTime:
    """Stand in for MicroPython's time module.

    `time.ticks_ms` does not exist on CPython, and `_handle_line` calls
    it on every accepted frame.
    """

    @staticmethod
    def ticks_ms() -> int:
        return 0


class HandleLineTest(unittest.TestCase):
    def setUp(self) -> None:
        self._real_time = buddy_serial.time
        buddy_serial.time = _FakeTime()
        self.addCleanup(setattr, buddy_serial, "time", self._real_time)

        # Bypass __init__: it registers the real stdin with a poller and
        # disables Ctrl-C, neither of which belongs in a unit test.
        self.t = BuddySerial.__new__(BuddySerial)
        self.lines: list[bytes] = []
        self.states: list[str] = []
        self.t._on_line = self.lines.append
        self.t._on_state = self.states.append
        self.t._host_seen = False
        self.t._last_rx_ms = 0

    def test_accepts_a_clean_frame(self) -> None:
        self.t._handle_line(_SENTINEL + b'{"cmd":"status"}')
        self.assertEqual(self.lines, [b'{"cmd":"status"}'])
        self.assertEqual(self.states, ["connected"])

    def test_ignores_a_line_without_the_sentinel(self) -> None:
        self.t._handle_line(b"just some REPL noise")
        self.assertEqual(self.lines, [])
        self.assertFalse(self.t._host_seen)

    def test_tolerates_a_partial_line_prefix(self) -> None:
        # A control byte left over from whatever ran before, sitting in
        # the rx buffer ahead of the first frame. The host no longer
        # produces one — raw-paste acknowledges its own terminator,
        # unlike the paste mode this used to use — but a strict
        # startswith() drops such a frame silently, and the device is
        # the side that cannot be debugged when it does.
        self.t._handle_line(b"\x04" + _SENTINEL + b'{"cmd":"status"}')
        self.assertEqual(self.lines, [b'{"cmd":"status"}'])

    def test_tolerates_repl_echo_before_the_frame(self) -> None:
        self.t._handle_line(b">>> " + _SENTINEL + b'{"cmd":"status"}')
        self.assertEqual(self.lines, [b'{"cmd":"status"}'])

    def test_strips_trailing_cr(self) -> None:
        self.t._handle_line(_SENTINEL + b'{"cmd":"status"}\r')
        self.assertEqual(self.lines, [b'{"cmd":"status"}'])

    def test_ignores_a_sentinel_with_no_payload(self) -> None:
        self.t._handle_line(_SENTINEL)
        self.assertEqual(self.lines, [])
        self.assertFalse(self.t._host_seen)

    def test_connected_state_fires_once(self) -> None:
        self.t._handle_line(_SENTINEL + b'{"cmd":"status"}')
        self.t._handle_line(_SENTINEL + b'{"cmd":"status"}')
        self.assertEqual(self.states, ["connected"])
        self.assertEqual(len(self.lines), 2)


if __name__ == "__main__":
    unittest.main()
