"""ResidentLink tests against a fake serial port.

The reader thread is the part that is easy to get wrong — messages that
arrive between tool calls, an ack that lands after the request started
waiting, a port that dies mid-wait. None of that needs hardware, so it
is all covered here rather than only on the bench.

Run with:
    python -m unittest discover -s host/tests
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from buddy_bridge import SENTINEL, ResidentLink, encode  # noqa: E402


def framed(payload: bytes) -> bytes:
    return SENTINEL + payload + b"\n"


class FakeSerial:
    """Minimal stand-in for serial.Serial, driven from the test."""

    def __init__(self, port, baud, timeout=None):
        self.port = port
        self.baud = baud
        self.closed = False
        self.fail: OSError | None = None
        self._inbound = bytearray()
        self._written = bytearray()
        self._lock = threading.Lock()

    # ----- the surface ResidentLink uses

    @property
    def in_waiting(self):
        if self.fail:
            raise self.fail
        with self._lock:
            return len(self._inbound)

    def read(self, size):
        if self.fail:
            raise self.fail
        with self._lock:
            data = bytes(self._inbound[:size])
            del self._inbound[:size]
        if not data:
            # Stand in for the blocking read's timeout so the reader
            # thread does not spin the CPU during a test.
            time.sleep(0.005)
        return data

    def write(self, data):
        with self._lock:
            self._written.extend(data)
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True

    # ----- test helpers

    def feed(self, data: bytes) -> None:
        with self._lock:
            self._inbound.extend(data)

    def written(self) -> bytes:
        with self._lock:
            return bytes(self._written)


class ResidentLinkTest(unittest.TestCase):
    def setUp(self):
        self.fakes = []

        def factory(port, baud, timeout=None):
            fake = FakeSerial(port, baud, timeout)
            self.fakes.append(fake)
            return fake

        self.link = ResidentLink("/dev/fake", serial_factory=factory)
        self.link.connect()
        self.fake = self.fakes[0]
        self.addCleanup(self.link.disconnect)

    def test_send_frames_with_sentinel(self):
        self.link.send({"cmd": "status"})
        self.assertEqual(self.fake.written(), encode({"cmd": "status"}))

    def test_request_returns_matching_ack(self):
        self.fake.feed(framed(b'{"ack":"status","ok":true}'))
        ack = self.link.request({"cmd": "status"}, "status", timeout=2.0)
        self.assertEqual(ack["ack"], "status")

    def test_request_waits_for_a_late_ack(self):
        # The ack lands only after request() is already blocked, which is
        # the ordering that actually happens on the wire.
        threading.Timer(
            0.2, lambda: self.fake.feed(framed(b'{"ack":"name","ok":true}'))
        ).start()
        ack = self.link.request({"cmd": "name", "name": "x"}, "name", timeout=3.0)
        self.assertTrue(ack["ok"])

    def test_unrelated_traffic_survives_a_request(self):
        # The device emits `hello` on handshake; it must not be consumed
        # or discarded by a concurrent status request.
        self.fake.feed(framed(b'{"cmd":"hello","name":"Buddy"}'))
        self.fake.feed(framed(b'{"ack":"status","ok":true}'))
        self.link.request({"cmd": "status"}, "status", timeout=2.0)
        msgs, _logs = self.link.events()
        self.assertEqual([m.get("cmd") for m in msgs], ["hello"])

    def test_captures_traffic_between_calls(self):
        # Nobody is waiting: the reader thread still has to collect this.
        self.fake.feed(framed(b'{"cmd":"hello"}') + b"buddy_serial: up\n")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            msgs, logs = self.link.events()
            if msgs and logs:
                break
            time.sleep(0.01)
        self.assertEqual([m.get("cmd") for m in msgs], ["hello"])
        self.assertEqual(logs, [b"buddy_serial: up"])

    def test_events_drains(self):
        self.fake.feed(b"log one\n")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not self.link.events()[1]:
            time.sleep(0.01)
        self.assertEqual(self.link.events(), ([], []))

    def test_dropped_port_raises_connection_error(self):
        self.fake.fail = OSError(6, "Device not configured")
        with self.assertRaises(ConnectionError):
            self.link.request({"cmd": "status"}, "status", timeout=3.0)

    def test_timeout_when_nothing_answers(self):
        with self.assertRaises(TimeoutError):
            self.link.request({"cmd": "status"}, "status", timeout=0.3)

    def test_disconnect_closes_the_port(self):
        self.link.disconnect()
        self.assertTrue(self.fake.closed)
        self.assertFalse(self.link.connected)

    def test_start_app_terminates_its_last_line(self):
        # The paste-mode terminator (0x04) carries no newline of its own.
        # Left unterminated it sits in the device's rx buffer and gets
        # prepended to the next frame, whose sentinel then no longer
        # starts the line — the device drops it silently and the first
        # request after a launch times out.
        self.link.start_app(settle=0.0)
        self.assertTrue(
            self.fake.written().endswith(b"\n"),
            "start_app must leave the device's line buffer empty",
        )

    def test_first_frame_after_start_app_starts_the_line(self):
        # The property the device actually checks: once start_app is
        # done, a subsequent frame must be the first thing on its line.
        self.link.start_app(settle=0.0)
        self.link.send({"cmd": "status"})
        last_line = self.fake.written().split(b"\n")[-2] + b"\n"
        self.assertEqual(last_line, encode({"cmd": "status"}))


if __name__ == "__main__":
    unittest.main()
