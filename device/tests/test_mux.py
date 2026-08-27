"""`buddy/mux.py` — serial と net を 1 つのトランスポートに見せる。

Router も protocol も `ble` を 1 つしか持たないので、2 本を束ねるのはここ。
poll は全員、send は繋がっている全員、state は「誰か 1 人でも」で集約する。
"""

import unittest

from buddy.mux import TransportMux


class FakeLeg:
    """束ねられる側。`connected` と `send_line` の成否をテストが決める。"""

    def __init__(self, name: str, mux: TransportMux) -> None:
        self.advertised_name = name
        self.connected = False
        self.accepts = True
        self.sent: list[bytes] = []
        self.polls = 0
        self.deinits = 0
        self.disconnects = 0
        self.forgets = 0
        self._on_state = mux.child_state

    def poll(self) -> None:
        self.polls += 1

    def send_line(self, payload: bytes) -> bool:
        if not self.connected or not self.accepts:
            return False
        self.sent.append(payload)
        return True

    def disconnect(self) -> None:
        self.disconnects += 1
        self.go(False)

    def forget_bonds(self) -> None:
        self.forgets += 1

    def deinit(self) -> None:
        self.deinits += 1

    def go(self, up: bool) -> None:
        """本物と同じ順: 自分の状態を変えてから知らせる。"""
        if self.connected == up:
            return
        self.connected = up
        self._on_state("connected" if up else "disconnected")


class TransportMuxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.states: list[str] = []
        self.mux = TransportMux(on_state=self.states.append)
        self.usb = FakeLeg("Claude_serial", self.mux)
        self.net = FakeLeg("Claude_net:8788", self.mux)
        self.mux.add(self.usb)
        self.mux.add(self.net)

    def test_looks_like_one_transport(self) -> None:
        self.assertFalse(self.mux.pairing_supported)
        self.assertFalse(self.mux.encrypted)
        self.assertEqual(self.mux.advertised_name, "Claude_serial+Claude_net:8788")
        self.assertFalse(self.mux.connected)

    def test_poll_reaches_every_leg(self) -> None:
        self.mux.poll()
        self.assertEqual((self.usb.polls, self.net.polls), (1, 1))

    def test_connected_when_any_leg_is(self) -> None:
        self.net.go(True)
        self.assertTrue(self.mux.connected)
        self.assertEqual(self.states, ["connected"])

    def test_a_second_leg_coming_up_is_not_a_second_connection(self) -> None:
        self.usb.go(True)
        self.net.go(True)
        self.assertEqual(self.states, ["connected"])

    def test_disconnected_only_when_the_last_leg_goes(self) -> None:
        self.usb.go(True)
        self.net.go(True)
        self.usb.go(False)
        self.assertEqual(self.states, ["connected"])
        self.assertTrue(self.mux.connected)
        self.net.go(False)
        self.assertEqual(self.states, ["connected", "disconnected"])

    def test_other_states_pass_straight_through(self) -> None:
        self.mux.child_state("encrypted")
        self.assertEqual(self.states, ["encrypted"])

    def test_send_goes_to_everyone_who_is_connected(self) -> None:
        self.net.go(True)
        self.assertTrue(self.mux.send_line(b'{"ack":"a"}'))
        self.assertEqual(self.usb.sent, [])
        self.assertEqual(self.net.sent, [b'{"ack":"a"}'])
        self.usb.go(True)
        self.assertTrue(self.mux.send_line(b'{"ack":"b"}'))
        self.assertEqual(self.usb.sent, [b'{"ack":"b"}'])
        self.assertEqual(self.net.sent, [b'{"ack":"a"}', b'{"ack":"b"}'])

    def test_send_is_false_only_when_nobody_took_it(self) -> None:
        self.assertFalse(self.mux.send_line(b"x"))
        self.usb.go(True)
        self.net.go(True)
        self.usb.accepts = False
        self.assertTrue(self.mux.send_line(b"x"))
        # 先頭が断っても後ろへ届く: 短絡しない。
        self.assertEqual(self.net.sent, [b"x"])

    def test_disconnect_and_forget_bonds_fan_out(self) -> None:
        self.usb.go(True)
        self.net.go(True)
        self.mux.disconnect()
        self.assertEqual((self.usb.disconnects, self.net.disconnects), (1, 1))
        self.assertEqual(self.states, ["connected", "disconnected"])
        self.mux.forget_bonds()
        self.assertEqual((self.usb.forgets, self.net.forgets), (1, 1))

    def test_deinit_reaches_every_leg_even_if_one_throws(self) -> None:
        def boom() -> None:
            raise OSError("already closed")

        self.usb.deinit = boom
        self.mux.deinit()
        self.assertEqual(self.net.deinits, 1)


if __name__ == "__main__":
    unittest.main()
