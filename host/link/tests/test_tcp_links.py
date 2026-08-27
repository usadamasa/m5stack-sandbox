"""`tcp://` の target を `ResidentLink` / `BuddyLink` に渡したとき。

開くのは `buddy_net.open_port` 経由で、REPL が要るもの (`launch_app` /
`start_app` / `interrupt`) は断る。実際の socket は張らない。
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from test_resident_link import FakeSerial

import buddy_link
import resident_link
from buddy_link import BuddyLink
from device_repl import ReplError
from resident_link import ResidentLink


class ResidentOverTcpTest(unittest.TestCase):
    def test_connect_opens_the_target_through_open_port(self) -> None:
        opened: list[Any] = []

        def open_port(target: str, baud: int, timeout: float) -> FakeSerial:
            opened.append((target, baud, timeout))
            return FakeSerial(target, baud, timeout)

        link = ResidentLink("tcp://192.168.0.227", read_timeout=0.05)
        with mock.patch.object(resident_link, "open_port", open_port):
            link.connect()
        self.addCleanup(link.disconnect)
        self.assertEqual(opened, [("tcp://192.168.0.227", 115200, 0.05)])
        self.assertTrue(link.connected)

    def test_start_app_is_refused_without_touching_the_target(self) -> None:
        link = ResidentLink("tcp://192.168.0.227")
        with (
            mock.patch.object(resident_link, "launch_app") as launch,
            self.assertRaises(ReplError),
        ):
            link.start_app()
        launch.assert_not_called()

    def test_interrupt_is_refused_because_there_is_no_console(self) -> None:
        ser = FakeSerial("tcp://x", 0, None)
        link = ResidentLink("tcp://x", serial_factory=lambda *_a, **_k: ser)
        link.connect()
        self.addCleanup(link.disconnect)
        with self.assertRaises(ReplError):
            link.interrupt()
        self.assertEqual(ser.written(), b"")


class BuddyLinkOverTcpTest(unittest.TestCase):
    def test_open_goes_through_open_port(self) -> None:
        ser = FakeSerial("tcp://x", 0, None)
        with mock.patch.object(buddy_link, "open_port", return_value=ser) as open_port:
            with BuddyLink("tcp://192.168.0.227:9000") as link:
                self.assertIsInstance(link, BuddyLink)
        open_port.assert_called_once_with("tcp://192.168.0.227:9000", 115200, timeout=mock.ANY)
        self.assertTrue(ser.closed)

    def test_launch_app_is_refused(self) -> None:
        with self.assertRaises(ReplError):
            buddy_link.launch_app("tcp://192.168.0.227")

    def test_interrupt_is_refused(self) -> None:
        ser = FakeSerial("tcp://x", 0, None)
        with mock.patch.object(buddy_link, "open_port", return_value=ser):
            with BuddyLink("tcp://x") as link, self.assertRaises(ReplError):
                link.interrupt()
        self.assertEqual(ser.written(), b"")


if __name__ == "__main__":
    unittest.main()
