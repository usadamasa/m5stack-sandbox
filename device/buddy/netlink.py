"""WiFi の上の TCP transport。USB を挿さずにホストと繋ぐ経路。

`buddy.serial.BuddySerial` と同じ面を持つ (`send_line` / `poll` / `disconnect` /
`forget_bonds` / `deinit` / `advertised_name` / `connected` / `pairing_supported` /
`encrypted`) ので、`buddy.mux.TransportMux` が serial と並べて束ねられる。

### なぜ BLE ではないか

issue #29 の PoC で測った。NimBLE を `active(True)` した瞬間に ESP-IDF heap が
63 KB 減り、アプリ稼働中の余り (40 KB) には入らない。TCP の listener 1 本は
1 KB で、framing (`_SENTINEL` 付きの JSON 行) も host の `LineDemux` もそのまま
使えた。音声は元から WiFi で取っている (`buddy/tts.py`) ので、USB が運んでいた
のは JSON の行だけ。それを socket で受ける。

### 形

`PORT` で listen し、client は同時に 1 本。新しい accept は古い client を置き換える
— daemon を restart したときに前の接続が残っていても繋ぎ直せるように。lwIP の
socket は数が少ないので、抱えたままにはしない。

読みは serial と同じ規律: tick ごとに `_MAX_DRAIN` まで、行が `_MAX_LINE` を
超えたら resync、sentinel は行のどこにあってもよい。書きは non-blocking で、
詰まったら少し待って撃ち直し、それでも駄目なら client を落とす。落とすのは
`on_state("disconnected")` を伴う。

### 認証は無い

USB と同じ平文で、しかも LAN の中の誰でも繋げる。`dbg.*` (`buddy/debug.py`) も
この経路に乗るので、繋いだ相手はデバイスの上でコードを流せる。家の LAN で
使う前提で、そう決めた (README の「USB なしで使う」)。

### MicroPython

`typing` も `__future__` も無い。annotation は組み込みの名前だけ
(`device/tests/test_device_constraints.py`)。socket モジュールは注入できるので、
`device/tests/test_netlink.py` は本物の bind を一切しない。
"""

import errno
import socket
import time

# 型検査だけの import。デバイスの上では `False` なので走らない。
_TYPE_CHECKING = False
if _TYPE_CHECKING:
    from buddy_types import (  # noqa: F401
        LineCallback,
        Listener,
        Socket,
        SocketModule,
        StateCallback,
    )

# `buddy.serial._SENTINEL` と同じ。ホストの `buddy_wire.SENTINEL` とも。
_SENTINEL = b"\x1eBUDDY1 "

# ホスト側の `buddy_net.DEFAULT_PORT` と同じ値 (契約テストが突き合わせる)。
# daemon の HTTP (8787) の隣。
PORT = 8788

# serial と同じ根拠 (`buddy/serial.py`)。
_MAX_DRAIN = 512
_MAX_LINE = 4096
_RECV = 256

# non-blocking の send が詰まったときの撃ち直し。1 回 5 ms、20 回で 100 ms。
# ack は数百 byte なので、これで通らない相手は居なくなったものと見なす。
_SEND_TRIES = 20
_SEND_WAIT_MS = 5

_WOULD_BLOCK = (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINPROGRESS)


def _noop_line(_line):
    # type: (bytes) -> None
    return None


def _noop_state(_st):
    # type: (str) -> None
    return None


def _would_block(e):
    # type: (OSError) -> bool
    code = e.args[0] if e.args else None
    return code in _WOULD_BLOCK


class BuddyNet:
    """Nordic-UART-shaped protocol over one TCP connection."""

    pairing_supported = False
    encrypted = False

    def __init__(
        self,
        on_line=None,  # type: LineCallback | None
        on_state=None,  # type: StateCallback | None
        port=PORT,  # type: int
        socket_mod=None,  # type: SocketModule | None
    ) -> None:
        self._on_line = on_line or _noop_line
        self._on_state = on_state or _noop_state
        self._name = "Claude_net:%d" % port
        self._rx_buf = bytearray()
        self._client = None  # type: Socket | None
        self._shutting_down = False

        s = socket_mod if socket_mod is not None else socket  # type: SocketModule
        self._ls = s.socket()  # type: Listener
        self._ls.setsockopt(s.SOL_SOCKET, s.SO_REUSEADDR, 1)
        self._ls.bind(("0.0.0.0", port))
        self._ls.listen(1)
        self._ls.setblocking(False)
        print("buddy.netlink: listening on", port)

    # ----- transport surface

    @property
    def advertised_name(self) -> str:
        return self._name

    @property
    def connected(self) -> bool:
        return self._client is not None

    def send_line(self, payload):
        # type: (bytes | bytearray | str) -> bool
        """Push one JSON line to the client. False if nobody is there."""
        client = self._client
        if self._shutting_down or client is None:
            return False
        if not isinstance(payload, (bytes, bytearray)):
            payload = payload.encode("utf-8")
        if payload.endswith(b"\n"):
            payload = payload[:-1]
        raw = _SENTINEL + bytes(payload) + b"\n"
        sent = 0
        tries = 0
        while sent < len(raw):
            try:
                n = client.send(raw[sent:])
            except OSError as e:
                if _would_block(e) and tries < _SEND_TRIES:
                    tries += 1
                    time.sleep_ms(_SEND_WAIT_MS)
                    continue
                print("buddy.netlink: send failed:", e)
                self._drop()
                return False
            sent += n or 0
        return True

    def disconnect(self) -> None:
        """Drop the client. The listener stays up for the next one."""
        self._drop()

    def forget_bonds(self) -> None:
        # No bonding store on a socket. Parity with BuddyBLE / BuddySerial.
        pass

    def deinit(self) -> None:
        self._shutting_down = True
        # serial と同じで、畳むときは state を出さない。アプリはもう抜けて
        # いて、ここで "disconnected" を渡すと去り際の UI を描き直すことになる。
        client, self._client = self._client, None
        for sock in (client, self._ls):
            if sock is None:
                continue
            try:
                sock.close()
            except OSError:
                pass
        self._on_line = _noop_line
        self._on_state = _noop_state
        print("buddy.netlink: down")

    # ----- inbound pump

    def poll(self) -> None:
        """Accept a waiting client, then drain what it has sent."""
        if self._shutting_down:
            return
        self._accept()
        client = self._client
        if client is None:
            return
        drained = 0
        while drained < _MAX_DRAIN:
            try:
                chunk = client.recv(_RECV)
            except OSError as e:
                if not _would_block(e):
                    print("buddy.netlink: recv failed:", e)
                    self._drop()
                break
            if not chunk:
                # EOF。相手が閉じた。
                self._drop()
                break
            drained += len(chunk)
            self._rx_buf.extend(chunk)

        while True:
            nl = self._rx_buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(self._rx_buf[:nl])
            # MicroPython の bytearray はスライス削除ができないので末尾を束ね直す。
            self._rx_buf = self._rx_buf[nl + 1 :]
            self._handle_line(line)

        if len(self._rx_buf) > _MAX_LINE:
            print("buddy.netlink: rx overflow, resyncing")
            self._rx_buf = bytearray()

    # ----- internals

    def _accept(self) -> None:
        try:
            client, addr = self._ls.accept()
        except OSError:
            # 待っている client が居ない (EAGAIN)。他のエラーも同じ扱いで、
            # listener が壊れているなら次の tick も同じ答えになるだけ。
            return
        client.setblocking(False)
        old = self._client
        self._client = client
        self._rx_buf = bytearray()
        print("buddy.netlink: client", addr)
        if old is not None:
            # 置き換え。論理的には繋がったままなので state は動かさない。
            try:
                old.close()
            except OSError:
                pass
            return
        self._emit_state("connected")

    def _drop(self) -> None:
        client = self._client
        if client is None:
            return
        self._client = None
        self._rx_buf = bytearray()
        try:
            client.close()
        except OSError:
            pass
        self._emit_state("disconnected")

    def _handle_line(self, line: bytes) -> None:
        line = line.rstrip(b"\r")
        idx = line.find(_SENTINEL)
        if idx < 0:
            return
        payload = line[idx + len(_SENTINEL) :]
        if not payload:
            return
        self._on_line(payload)

    def _emit_state(self, state: str) -> None:
        try:
            self._on_state(state)
        except Exception as e:
            print("buddy.netlink: on_state error:", e)
