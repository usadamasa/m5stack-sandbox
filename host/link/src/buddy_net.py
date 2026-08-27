"""デバイスへ WiFi (TCP) で繋ぐ側。`buddy_wire.SerialPort` の顔をした socket。

`tcp://host[:port]` という target を `ResidentLink` / `BuddyLink` が受け取ると、
`serial.Serial` の代わりにここが開く。framing は USB と同じなので、上に載る
`LineDemux` も verb も変わらない。デバイス側の受け口は `device/buddy/netlink.py`。

### 何が同じで何が違うか

`SerialPort` の 5 つ (`in_waiting` / `read` / `write` / `flush` / `close`) を
socket で写す。`in_waiting` は `select` で「読めるか」を訊いて、読めるなら
ひと塊ぶんの数を返す — pyserial のように正確なバイト数は要らず、reader は
0 かどうかしか見ない。`read` の timeout は pyserial と同じく空 (`b""`) で、
相手が閉じた EOF は `OSError` にする。`ResidentLink` はケーブルが抜けたときと
同じ経路 (`dropped`) でそれを拾う。

REPL は無い。`launch_app` / `start_app` / `interrupt` は raw REPL か Ctrl-C を
USB の console へ送るもので、TCP の向こうにあるのはアプリの listener だけ。
それらは `tcp://` を明示的に断る (`buddy_link` / `resident_link`)。
"""

from __future__ import annotations

import select
import socket
from collections.abc import Callable
from typing import Protocol

import serial

from buddy_wire import SerialFactory, SerialPort

# デバイス側の `buddy.netlink.PORT` と同じ値 (`device/tests/test_netlink.py` が
# 突き合わせる)。
DEFAULT_PORT = 8788

SCHEME = "tcp://"

# `in_waiting` が「読める」ときに返す数。reader はこれを read の size に使う
# ので、1 度の recv で取る上限でもある。
CHUNK = 4096

# 接続を張るときの上限。daemon の `connect_on_start` は起動と並んで走るので、
# 居ないデバイスをいつまでも待つと status が止まる。
DEFAULT_CONNECT_TIMEOUT = 5.0


class Sock(Protocol):
    """`socket.socket` のうちここが触るぶん。テストの fake も同じ面。"""

    def fileno(self) -> int: ...
    def recv(self, bufsize: int, /) -> bytes: ...
    def sendall(self, data: bytes, /) -> None: ...
    def settimeout(self, value: float | None, /) -> None: ...
    def close(self) -> None: ...


CreateConnection = Callable[[tuple[str, int], float], Sock]
DEFAULT_CREATE_CONNECTION: CreateConnection = socket.create_connection


def is_tcp(target: str) -> bool:
    return target.lower().startswith(SCHEME)


def parse_target(target: str) -> tuple[str, int]:
    """`tcp://host[:port]` を (host, port) に。それ以外は ValueError。"""
    if not is_tcp(target):
        raise ValueError(f"{target!r} is not a tcp:// target")
    rest = target[len(SCHEME) :]
    host, sep, port_text = rest.rpartition(":")
    if not sep:
        host, port_text = rest, str(DEFAULT_PORT)
    if not host:
        raise ValueError(f"{target!r} has no host")
    try:
        port = int(port_text)
    except ValueError:
        raise ValueError(f"{target!r} has a port that is not a number") from None
    if not 0 < port < 65536:
        raise ValueError(f"{target!r} has a port out of range")
    return host, port


def _selectable(sock: Sock) -> Callable[[], bool]:
    def readable() -> bool:
        ready, _, _ = select.select([sock], [], [], 0)
        return bool(ready)

    return readable


class TcpPort:
    """One TCP connection wearing `buddy_wire.SerialPort`."""

    def __init__(self, sock: Sock, readable: Callable[[], bool] | None = None) -> None:
        self._sock = sock
        self._readable = readable if readable is not None else _selectable(sock)

    @property
    def in_waiting(self) -> int:
        return CHUNK if self._readable() else 0

    def read(self, size: int = 1, /) -> bytes:
        try:
            data = self._sock.recv(size)
        except TimeoutError:
            return b""
        if not data:
            raise OSError("connection closed by the device")
        return data

    def write(self, data: bytes, /) -> int | None:
        self._sock.sendall(data)
        return len(data)

    def flush(self) -> None:
        # socket に flush は無い。sendall が返った時点でカーネルに渡っている。
        return None

    def close(self) -> None:
        self._sock.close()


def open_port(
    target: str,
    baud: int,
    timeout: float,
    *,
    create_connection: CreateConnection = DEFAULT_CREATE_CONNECTION,
    serial_factory: SerialFactory = serial.Serial,
) -> SerialPort:
    """target に応じて TCP かシリアルを開く。どちらも `SerialPort`。

    `baud` は TCP では意味を持たないが、呼び手 (`ResidentLink` / `BuddyLink`) が
    どちらを開くか知らずに済むよう、同じ引数を受ける。接続を張るまでの上限は
    `DEFAULT_CONNECT_TIMEOUT` で固定 — 変えたい理由がまだ無い。
    """
    if not is_tcp(target):
        return serial_factory(target, baud, timeout=timeout)
    host, port = parse_target(target)
    sock = create_connection((host, port), DEFAULT_CONNECT_TIMEOUT)
    # 張るまでは connect_timeout、張ってからは reader の刻み。
    sock.settimeout(timeout)
    return TcpPort(sock)
