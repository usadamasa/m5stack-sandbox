# pyright: reportPrivateUsage=false
"""`buddy/netlink.py` — WiFi の上の TCP transport。

socket は注入した fake で、本物の bind は一切しない (sandbox が拒むし、単体
テストがポートを開く理由も無い)。`BuddySerial` と同じ面を持つことと、
`buddy_net` (ホスト側) と既定ポートが一致することをここで押さえる。
"""

import errno
import unittest
from collections import deque

import buddy_net

from buddy import netlink
from buddy.netlink import PORT, BuddyNet

SENTINEL = netlink._SENTINEL


class _FakeTime:
    @staticmethod
    def ticks_ms() -> int:
        return 0

    @staticmethod
    def sleep_ms(_ms: int) -> None:
        return None


class FakeSock:
    """1 本の TCP 接続。`rx` に積んだものを recv が順に返す。"""

    def __init__(self) -> None:
        self.rx: deque[bytes | OSError] = deque()
        self.sent: list[bytes] = []
        self.closed = False
        self.blocking: bool | None = None
        self.send_error: OSError | None = None
        # 0 より大きければ、send はこのバイト数までしか受け取らない。
        self.send_limit = 0
        self.recvs = 0

    def readable(self) -> bool:
        """poll が「読める」と答える条件。EOF (b"") も読めるうちに入る。"""
        return bool(self.rx)

    def recv(self, n: int) -> bytes:
        self.recvs += 1
        if not self.rx:
            raise OSError(errno.EAGAIN)
        item = self.rx.popleft()
        if isinstance(item, OSError):
            raise item
        if len(item) > n:
            # 本物と同じで、読み切れなかったぶんは次の recv に残る。
            self.rx.appendleft(item[n:])
        return item[:n]

    def send(self, data: bytes) -> int:
        if self.send_error is not None:
            raise self.send_error
        if self.send_limit and len(data) > self.send_limit:
            data = data[: self.send_limit]
        self.sent.append(bytes(data))
        return len(data)

    def setblocking(self, flag: bool) -> None:
        self.blocking = flag

    def close(self) -> None:
        self.closed = True

    def wire(self) -> bytes:
        return b"".join(self.sent)


class FakeListener(FakeSock):
    def __init__(self) -> None:
        super().__init__()
        self.pending: deque[FakeSock] = deque()
        self.bound: tuple[str, int] | None = None
        self.backlog: int | None = None
        self.options: list[tuple[int, int, int]] = []
        self.accepts = 0

    def readable(self) -> bool:
        return bool(self.pending)

    def setsockopt(self, level: int, opt: int, value: int) -> None:
        self.options.append((level, opt, value))

    def bind(self, addr: tuple[str, int]) -> None:
        self.bound = addr

    def listen(self, backlog: int) -> None:
        self.backlog = backlog

    def accept(self) -> tuple[FakeSock, tuple[str, int]]:
        self.accepts += 1
        if not self.pending:
            raise OSError(errno.EAGAIN)
        return self.pending.popleft(), ("192.168.0.155", 50000)


class FakeSocketModule:
    SOL_SOCKET = 0xFFF
    SO_REUSEADDR = 4

    def __init__(self) -> None:
        self.listener = FakeListener()
        self.made = 0

    def socket(self) -> FakeListener:
        self.made += 1
        return self.listener


class FakePoller:
    """登録した socket のうち、読めるものだけを返す。本物の select.poll と同じ形。"""

    def __init__(self) -> None:
        self.registered: list[FakeSock] = []

    def register(self, obj: FakeSock, _events: int) -> None:
        self.registered.append(obj)

    def unregister(self, obj: FakeSock) -> None:
        if obj in self.registered:
            self.registered.remove(obj)

    def poll(self, _timeout: int = 0) -> list[tuple[FakeSock, int]]:
        return [(o, 1) for o in self.registered if o.readable()]


class FakeSelectModule:
    POLLIN = 1

    def __init__(self) -> None:
        self.pollers: list[FakePoller] = []

    def poll(self) -> FakePoller:
        made = FakePoller()
        self.pollers.append(made)
        return made


class BuddyNetTest(unittest.TestCase):
    def setUp(self) -> None:
        self._real_time = netlink.time
        netlink.time = _FakeTime()
        self.addCleanup(setattr, netlink, "time", self._real_time)

        self._real_select = netlink.select
        netlink.select = FakeSelectModule()
        self.addCleanup(setattr, netlink, "select", self._real_select)

        self.sock = FakeSocketModule()
        self.lines: list[bytes] = []
        self.states: list[str] = []
        self.t = BuddyNet(
            on_line=self.lines.append, on_state=self.states.append, socket_mod=self.sock
        )
        self.listener = self.sock.listener

    def _connect(self) -> FakeSock:
        client = FakeSock()
        self.listener.pending.append(client)
        self.t.poll()
        return client

    # ----- listen

    def test_listens_on_the_agreed_port_without_blocking(self) -> None:
        self.assertEqual(self.listener.bound, ("0.0.0.0", PORT))
        self.assertEqual(self.listener.backlog, 1)
        self.assertFalse(self.listener.blocking)
        self.assertIn(
            (FakeSocketModule.SOL_SOCKET, FakeSocketModule.SO_REUSEADDR, 1), self.listener.options
        )

    def test_the_port_is_the_one_the_host_dials(self) -> None:
        self.assertEqual(PORT, buddy_net.DEFAULT_PORT)

    def test_looks_like_the_serial_transport(self) -> None:
        self.assertFalse(self.t.pairing_supported)
        self.assertFalse(self.t.encrypted)
        self.assertIn("net", self.t.advertised_name)
        self.t.forget_bonds()

    # ----- nobody there

    def test_nothing_happens_without_a_client(self) -> None:
        self.t.poll()
        self.assertEqual(self.states, [])
        self.assertFalse(self.t.connected)
        self.assertFalse(self.t.send_line(b'{"ack":"x"}'))

    def test_an_idle_tick_does_not_call_accept(self) -> None:
        # accept を素で呼ぶと、繋いでいる相手が居ない間ずっと 40ms ごとに
        # OSError(EAGAIN) を確保することになる。実機ではその churn が発話を
        # 詰まらせ、最後にアプリを落とした。読めるときだけ呼ぶ。
        for _ in range(10):
            self.t.poll()
        self.assertEqual(self.listener.accepts, 0)

    def test_an_idle_client_does_not_call_recv(self) -> None:
        client = self._connect()
        for _ in range(10):
            self.t.poll()
        self.assertEqual(client.recvs, 0)

    # ----- a client

    def test_accepting_a_client_is_a_connection(self) -> None:
        client = self._connect()
        self.assertEqual(self.states, ["connected"])
        self.assertTrue(self.t.connected)
        self.assertFalse(client.blocking)

    def test_a_frame_reaches_on_line_and_the_ack_goes_back_framed(self) -> None:
        client = self._connect()
        client.rx.append(SENTINEL + b'{"cmd":"status"}\n')
        self.t.poll()
        self.assertEqual(self.lines, [b'{"cmd":"status"}'])
        self.assertTrue(self.t.send_line(b'{"ack":"status"}'))
        self.assertEqual(client.wire(), SENTINEL + b'{"ack":"status"}\n')

    def test_send_line_accepts_str_and_a_trailing_newline(self) -> None:
        client = self._connect()
        self.assertTrue(self.t.send_line('{"ack":"a"}\n'))
        self.assertEqual(client.wire(), SENTINEL + b'{"ack":"a"}\n')

    def test_send_line_keeps_going_after_a_short_write(self) -> None:
        client = self._connect()
        client.send_limit = 5
        self.assertTrue(self.t.send_line(b'{"ack":"status"}'))
        self.assertEqual(client.wire(), SENTINEL + b'{"ack":"status"}\n')

    def test_a_line_split_across_reads_is_reassembled(self) -> None:
        client = self._connect()
        client.rx.append(SENTINEL + b'{"cmd":')
        self.t.poll()
        self.assertEqual(self.lines, [])
        client.rx.append(b'"status"}\n')
        self.t.poll()
        self.assertEqual(self.lines, [b'{"cmd":"status"}'])

    def test_two_lines_in_one_read_are_both_delivered(self) -> None:
        client = self._connect()
        client.rx.append(SENTINEL + b'{"a":1}\r\n' + SENTINEL + b'{"b":2}\n')
        self.t.poll()
        self.assertEqual(self.lines, [b'{"a":1}', b'{"b":2}'])

    def test_the_sentinel_may_sit_anywhere_on_the_line(self) -> None:
        client = self._connect()
        client.rx.append(b"noise" + SENTINEL + b'{"a":1}\n')
        self.t.poll()
        self.assertEqual(self.lines, [b'{"a":1}'])

    def test_a_line_without_the_sentinel_is_dropped(self) -> None:
        client = self._connect()
        client.rx.append(b'{"a":1}\n')
        self.t.poll()
        self.assertEqual(self.lines, [])

    def test_a_runaway_line_resyncs_the_buffer(self) -> None:
        client = self._connect()
        client.rx.append(b"x" * (netlink._MAX_LINE + 1))
        # 1 tick に汲むのは `_MAX_DRAIN` まで。溢れるまで何 tick か要る。
        for _ in range(netlink._MAX_LINE // netlink._MAX_DRAIN + 2):
            self.t.poll()
        self.assertEqual(len(self.t._rx_buf), 0)
        client.rx.append(SENTINEL + b'{"a":1}\n')
        self.t.poll()
        self.assertEqual(self.lines, [b'{"a":1}'])

    # ----- losing the client

    def test_eof_drops_the_client(self) -> None:
        client = self._connect()
        client.rx.append(b"")
        self.t.poll()
        self.assertEqual(self.states, ["connected", "disconnected"])
        self.assertFalse(self.t.connected)
        self.assertTrue(client.closed)
        self.assertFalse(self.t.send_line(b'{"ack":"x"}'))

    def test_a_read_error_other_than_would_block_drops_the_client(self) -> None:
        client = self._connect()
        client.rx.append(OSError(errno.ECONNRESET))
        self.t.poll()
        self.assertEqual(self.states, ["connected", "disconnected"])
        self.assertTrue(client.closed)

    def test_a_failed_send_drops_the_client(self) -> None:
        client = self._connect()
        client.send_error = OSError(errno.EPIPE)
        self.assertFalse(self.t.send_line(b'{"ack":"x"}'))
        self.assertEqual(self.states, ["connected", "disconnected"])
        self.assertTrue(client.closed)

    def test_a_new_client_replaces_the_old_one(self) -> None:
        old = self._connect()
        old.rx.append(SENTINEL + b'{"half":')
        self.t.poll()
        new = self._connect()
        self.assertTrue(old.closed)
        self.assertEqual(self.states, ["connected"])
        new.rx.append(SENTINEL + b'{"a":1}\n')
        self.t.poll()
        # 前の client の書きかけは持ち越さない。
        self.assertEqual(self.lines, [b'{"a":1}'])
        self.assertTrue(self.t.send_line(b'{"ack":"a"}'))
        self.assertEqual(old.sent, [])
        self.assertEqual(new.wire(), SENTINEL + b'{"ack":"a"}\n')

    def test_disconnect_closes_the_client_and_says_so(self) -> None:
        client = self._connect()
        self.t.disconnect()
        self.assertTrue(client.closed)
        self.assertEqual(self.states, ["connected", "disconnected"])
        self.t.disconnect()
        self.assertEqual(self.states, ["connected", "disconnected"])

    # ----- teardown

    def test_deinit_closes_everything_and_goes_quiet(self) -> None:
        client = self._connect()
        self.t.deinit()
        self.assertTrue(client.closed)
        self.assertTrue(self.listener.closed)
        self.assertFalse(self.t.send_line(b'{"ack":"x"}'))
        self.listener.pending.append(FakeSock())
        self.t.poll()
        self.assertEqual(self.states, ["connected"])


if __name__ == "__main__":
    unittest.main()
