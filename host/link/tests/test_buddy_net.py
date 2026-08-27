"""`buddy_net` — `tcp://` の target を socket で開き、`SerialPort` の顔で返す。

socket は fake。本物の接続は張らない。
"""

from __future__ import annotations

import unittest
from collections import deque
from typing import Any

import buddy_net
from buddy_net import DEFAULT_PORT, TcpPort, is_tcp, open_port, parse_target


class FakeSock:
    def __init__(self) -> None:
        self.rx: deque[bytes | BaseException] = deque()
        self.sent: list[bytes] = []
        self.timeouts: list[float | None] = []
        self.closed = False

    def fileno(self) -> int:
        return -1

    def recv(self, n: int) -> bytes:
        if not self.rx:
            raise TimeoutError("timed out")
        item = self.rx.popleft()
        if isinstance(item, BaseException):
            raise item
        if len(item) > n:
            self.rx.appendleft(item[n:])
        return item[:n]

    def sendall(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def settimeout(self, value: float | None) -> None:
        self.timeouts.append(value)

    def close(self) -> None:
        self.closed = True


class TargetTest(unittest.TestCase):
    def test_a_device_path_is_not_tcp(self) -> None:
        self.assertFalse(is_tcp("/dev/cu.usbmodem101"))
        self.assertTrue(is_tcp("tcp://192.168.0.227"))
        self.assertTrue(is_tcp("TCP://buddy.local:8788"))

    def test_host_alone_gets_the_agreed_port(self) -> None:
        self.assertEqual(parse_target("tcp://192.168.0.227"), ("192.168.0.227", DEFAULT_PORT))

    def test_an_explicit_port_wins(self) -> None:
        self.assertEqual(parse_target("tcp://buddy.local:9000"), ("buddy.local", 9000))

    def test_garbage_is_refused_with_a_reason(self) -> None:
        for bad in ("tcp://", "tcp://:8788", "tcp://host:port", "tcp://host:0", "/dev/cu.x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_target(bad)


class TcpPortTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sock = FakeSock()
        self.readable = False
        self.port = TcpPort(self.sock, readable=lambda: self.readable)

    def test_in_waiting_follows_readability(self) -> None:
        self.assertEqual(self.port.in_waiting, 0)
        self.readable = True
        self.assertGreater(self.port.in_waiting, 0)

    def test_read_hands_back_what_arrived(self) -> None:
        self.sock.rx.append(b"abc")
        self.assertEqual(self.port.read(2), b"ab")
        self.assertEqual(self.port.read(8), b"c")

    def test_a_quiet_socket_reads_as_nothing(self) -> None:
        # pyserial の read と同じ: timeout は失敗ではなく空。reader は
        # `in_waiting` を見て回るので、ここで投げると静かな device で落ちる。
        self.assertEqual(self.port.read(1), b"")

    def test_eof_is_an_error_like_a_pulled_cable(self) -> None:
        # ResidentLink は OSError で `dropped` を立てる。閉じた接続も同じ姿に。
        self.sock.rx.append(b"")
        with self.assertRaises(OSError):
            self.port.read(1)

    def test_write_sends_everything_and_reports_the_length(self) -> None:
        self.assertEqual(self.port.write(b"hello"), 5)
        self.assertEqual(self.sock.sent, [b"hello"])
        self.port.flush()

    def test_close_closes_the_socket(self) -> None:
        self.port.close()
        self.assertTrue(self.sock.closed)


class OpenPortTest(unittest.TestCase):
    def setUp(self) -> None:
        self.serial_port = TcpPort(FakeSock(), readable=lambda: False)
        self.opened: list[Any] = []
        self.dialled: list[Any] = []

    def serial_factory(self, port: str, baud: int, timeout: float) -> TcpPort:
        self.opened.append((port, baud, timeout))
        return self.serial_port

    def create_connection(self, addr: tuple[str, int], timeout: float) -> FakeSock:
        self.dialled.append((addr, timeout))
        return FakeSock()

    def test_tcp_dials_the_host_and_sets_the_read_timeout(self) -> None:
        sock = FakeSock()

        def create_connection(addr: tuple[str, int], timeout: float) -> FakeSock:
            self.dialled.append((addr, timeout))
            return sock

        port = open_port(
            "tcp://192.168.0.227",
            115200,
            0.05,
            create_connection=create_connection,
            serial_factory=self.serial_factory,
        )
        self.assertIsInstance(port, TcpPort)
        self.assertEqual(
            self.dialled, [(("192.168.0.227", DEFAULT_PORT), buddy_net.DEFAULT_CONNECT_TIMEOUT)]
        )
        self.assertEqual(sock.timeouts, [0.05])
        self.assertEqual(self.opened, [])

    def test_anything_else_is_a_serial_port(self) -> None:
        port = open_port(
            "/dev/cu.usbmodem101",
            115200,
            0.05,
            create_connection=self.create_connection,
            serial_factory=self.serial_factory,
        )
        self.assertIs(port, self.serial_port)
        self.assertEqual(self.opened, [("/dev/cu.usbmodem101", 115200, 0.05)])
        self.assertEqual(self.dialled, [])

    def test_the_default_dialler_is_the_socket_module_s(self) -> None:
        self.assertIs(buddy_net.DEFAULT_CREATE_CONNECTION, buddy_net.socket.create_connection)


if __name__ == "__main__":
    unittest.main()
