# pyright: reportPrivateUsage=false
"""発話のテストが共有する fake。`test_speak_stream.py` と `test_speak.py` が使う。

ストリームを読む側 (`buddy.speak_stream`) と鳴らす側 (`buddy.speak`) で
テストを割ったが、時計とバイト列の口はどちらも同じものを要る。片方だけに
置くともう片方が import しに行くことになるので、ここに置く。

`TimeFrozen` が差し替えるのは `buddy.speak_stream.time` — stall の期限を
持っているのはあちらで、`buddy.speak` はもう時計を見ない。

ブロックの大きさを `buddy.speak._BLOCK` から直接引くので、basedpyright の
private-member の検査はこのファイルごと切ってある
(冒頭の `reportPrivateUsage=false`)。
"""

import unittest
from typing import TYPE_CHECKING

from buddy import speak_stream
from buddy.speak import _BLOCK

if TYPE_CHECKING:
    # 型検査だけ。`device/typings/` の stub-only モジュールで、実体は無い。
    from buddy_types import SpeechSource


class FakeTime:
    """MicroPython's ticks API, frozen and driven by the test."""

    now = 0

    @classmethod
    def ticks_ms(cls) -> int:
        return cls.now

    @classmethod
    def ticks_add(cls, base: int, delta: int) -> int:
        return base + delta

    @classmethod
    def ticks_diff(cls, a: int, b: int) -> int:
        return a - b


class FakeStream:
    """A byte source that hands out only what has been fed to it.

    `read` returns None rather than blocking when it is empty, which is
    what a socket with a timeout does from this layer's point of view.
    """

    def __init__(self, data: bytes = b"") -> None:
        self.buf = bytearray(data)
        self.timeout: float | None = None
        self.closed = False
        self.ended = False

    def feed(self, data: bytes) -> None:
        self.buf.extend(data)

    def end(self) -> None:
        """Mark end of stream: further reads return b"" once drained."""
        self.ended = True

    def read(self, n: int) -> bytes | None:
        if not self.buf:
            return b"" if self.ended else None
        take = bytes(self.buf[:n])
        del self.buf[:n]
        return take

    def settimeout(self, seconds: float) -> None:
        self.timeout = seconds

    def close(self) -> None:
        self.closed = True


class FakeResponse:
    """HTTP の response のうち、player が触る口だけ。"""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def unused_fetch(*_args: object) -> "SpeechSource":
    """A `fetch` double for tests that never call it (see VolumeTest)."""
    msg = "fetch should not have been called"
    raise AssertionError(msg)


def blk(ch: bytes) -> bytes:
    return ch * _BLOCK


class TimeFrozen(unittest.TestCase):
    """Base: every test here drives buddy.speak_stream's clock by hand."""

    def setUp(self) -> None:
        self._real_time = speak_stream.time
        speak_stream.time = FakeTime()
        self.addCleanup(setattr, speak_stream, "time", self._real_time)
        FakeTime.now = 0
