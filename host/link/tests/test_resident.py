"""ResidentLink tests against a fake serial port.

The reader thread is the part that is easy to get wrong — messages that
arrive between tool calls, an ack that lands after the request started
waiting, a port that dies mid-wait. None of that needs hardware, so it
is all covered here rather than only on the bench.
"""

import threading
import time
import unittest

import buddy_bridge
from buddy_bridge import SENTINEL, Message, ResidentLink, encode
from fake_repl import FakeRepl


def framed(payload: bytes) -> bytes:
    return SENTINEL + payload + b"\n"


class FakeSerial:
    """Minimal stand-in for serial.Serial, driven from the test.

    Structurally satisfies `buddy_bridge.SerialPort`, which is the whole
    point of that protocol existing.
    """

    def __init__(self, port: str, baud: int, timeout: float | None = None) -> None:
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.closed = False
        self.fail: OSError | None = None
        self._inbound = bytearray()
        self._written = bytearray()
        self._lock = threading.Lock()

    # ----- the surface ResidentLink uses

    @property
    def in_waiting(self) -> int:
        if self.fail:
            raise self.fail
        with self._lock:
            return len(self._inbound)

    def read(self, size: int = 1, /) -> bytes:
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

    def write(self, data: bytes, /) -> int:
        with self._lock:
            self._written.extend(data)
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    # ----- test helpers

    def feed(self, data: bytes) -> None:
        with self._lock:
            self._inbound.extend(data)

    def written(self) -> bytes:
        with self._lock:
            return bytes(self._written)


class ResidentLinkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fakes: list[FakeSerial] = []

        def factory(port: str, baud: int, timeout: float | None = None) -> FakeSerial:
            fake = FakeSerial(port, baud, timeout)
            self.fakes.append(fake)
            return fake

        self.link = ResidentLink("/dev/fake", serial_factory=factory)
        self.link.connect()
        self.fake = self.fakes[0]
        self.addCleanup(self.link.disconnect)

    def test_send_frames_with_sentinel(self) -> None:
        self.link.send({"cmd": "status"})
        self.assertEqual(self.fake.written(), encode({"cmd": "status"}))

    def test_request_returns_matching_ack(self) -> None:
        self.fake.feed(framed(b'{"ack":"status","ok":true}'))
        ack = self.link.request({"cmd": "status"}, "status", timeout=2.0)
        self.assertEqual(ack["ack"], "status")

    def test_request_waits_for_a_late_ack(self) -> None:
        # The ack lands only after request() is already blocked, which is
        # the ordering that actually happens on the wire.
        threading.Timer(0.2, lambda: self.fake.feed(framed(b'{"ack":"name","ok":true}'))).start()
        ack = self.link.request({"cmd": "name", "name": "x"}, "name", timeout=3.0)
        self.assertTrue(ack["ok"])

    def test_unrelated_traffic_survives_a_request(self) -> None:
        # The device emits `hello` on handshake; it must not be consumed
        # or discarded by a concurrent status request.
        self.fake.feed(framed(b'{"cmd":"hello","name":"Buddy"}'))
        self.fake.feed(framed(b'{"ack":"status","ok":true}'))
        self.link.request({"cmd": "status"}, "status", timeout=2.0)
        msgs, _logs = self.link.events()
        self.assertEqual([m.get("cmd") for m in msgs], ["hello"])

    def test_captures_traffic_between_calls(self) -> None:
        # Nobody is waiting: the reader thread still has to collect this.
        self.fake.feed(framed(b'{"cmd":"hello"}') + b"buddy_serial: up\n")
        # Accumulate rather than re-reading: events() drains, so a poll
        # that catches the protocol line but not yet the log line would
        # otherwise throw the protocol line away.
        msgs: list[Message] = []
        logs: list[bytes] = []
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not (msgs and logs):
            new_msgs, new_logs = self.link.events()
            msgs += new_msgs
            logs += new_logs
            time.sleep(0.01)
        self.assertEqual([m.get("cmd") for m in msgs], ["hello"])
        self.assertEqual(logs, [b"buddy_serial: up"])

    def test_events_drains(self) -> None:
        self.fake.feed(b"log one\n")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not self.link.events()[1]:
            time.sleep(0.01)
        self.assertEqual(self.link.events(), ([], []))

    def test_dropped_port_raises_connection_error(self) -> None:
        self.fake.fail = OSError(6, "Device not configured")
        with self.assertRaises(ConnectionError):
            self.link.request({"cmd": "status"}, "status", timeout=3.0)

    def test_timeout_when_nothing_answers(self) -> None:
        with self.assertRaises(TimeoutError):
            self.link.request({"cmd": "status"}, "status", timeout=0.3)

    def test_disconnect_closes_the_port(self) -> None:
        self.link.disconnect()
        self.assertTrue(self.fake.closed)
        self.assertFalse(self.link.connected)

    def test_connect_can_adopt_an_already_open_port(self) -> None:
        # How a launch is picked up. The REPL had the port to run the
        # import; opening a second one would miss whatever the device
        # says in the gap, which is where a failed import's traceback
        # lands.
        self.link.disconnect()
        adopted = FakeSerial("/dev/adopted", 115200, None)
        self.link.connect(adopt=adopted)
        self.assertTrue(self.link.connected)
        # Still one port from the factory: the one setUp opened.
        self.assertEqual(len(self.fakes), 1)
        self.link.send({"cmd": "status"})
        self.assertEqual(adopted.written(), encode({"cmd": "status"}))


class StartAppTest(unittest.TestCase):
    """Relaunching hands the port from the REPL to the reader thread."""

    def setUp(self) -> None:
        self.repl = FakeRepl()
        self.opened: list[str] = []

        def connect_repl(port: str, baud: int, timeout: float = 180.0, **_kw: object) -> FakeRepl:
            self.opened.append(port)
            return self.repl

        real = buddy_bridge.connect_repl
        buddy_bridge.connect_repl = connect_repl
        self.addCleanup(setattr, buddy_bridge, "connect_repl", real)

    def test_imports_the_app_without_waiting_for_it_to_end(self) -> None:
        # `exec` would block until the command returns, and the app's
        # whole job is never to return.
        buddy_bridge.launch_app("/dev/fake")
        self.assertEqual(self.repl.launched, [buddy_bridge.LAUNCH_SOURCE])
        self.assertEqual(self.repl.execs, [buddy_bridge.LAUNCH_SOURCE])

    def test_hands_back_a_port_the_reader_can_poll(self) -> None:
        # mpremote opens blocking with a one second inter-byte timeout.
        # A reader that polls in_waiting would stall on both.
        port = buddy_bridge.launch_app("/dev/fake", read_timeout=0.05)
        self.assertIs(port, self.repl.serial)
        self.assertEqual(self.repl.serial.timeout, 0.05)
        self.assertIsNone(self.repl.serial.inter_byte_timeout)

    def test_writes_nothing_after_the_launch(self) -> None:
        # The paste-mode launch had to send a trailing newline: Ctrl-D
        # carries none, so the stray byte was prepended to the next
        # protocol frame and the device dropped it, timing out exactly
        # one request. Raw-paste acknowledges its own terminator, so
        # there is nothing to clean up — and anything written here would
        # land in the app's input.
        buddy_bridge.launch_app("/dev/fake")
        self.assertEqual(bytes(self.repl.serial.written), b"")

    def test_a_resident_link_comes_back_on_the_launched_port(self) -> None:
        link = ResidentLink("/dev/fake", serial_factory=lambda *_a, **_k: FakeSerial("x", 0, None))
        link.connect()
        self.addCleanup(link.disconnect)
        link.start_app(settle=0.0)
        self.assertTrue(link.connected)
        link.send({"cmd": "status"})
        self.assertEqual(bytes(self.repl.serial.written), encode({"cmd": "status"}))

    def test_the_repl_wait_is_short_enough_for_a_tool_call(self) -> None:
        # The interrupt normally gets us there, but when it does not the
        # fallback is a BtnRST press. An MCP call that blocks for three
        # minutes waiting for one is worse than one that says so.
        captured: list[float] = []

        def connect_repl(_port: str, _baud: int, timeout: float = 180.0, **_kw: object) -> FakeRepl:
            captured.append(timeout)
            return self.repl

        buddy_bridge.connect_repl = connect_repl
        link = ResidentLink("/dev/fake", serial_factory=lambda *_a, **_k: FakeSerial("x", 0, None))
        link.connect()
        self.addCleanup(link.disconnect)
        link.start_app(settle=0.0)
        self.assertLessEqual(captured[0], 30.0)


if __name__ == "__main__":
    unittest.main()
