# pyright: reportPrivateUsage=false
"""発話のテストが共有する fake。`test_speak_stream.py` / `test_speak.py` /
`test_speak_volume.py` が使う。

ストリームを読む側 (`buddy.speak_stream`) と鳴らす側 (`buddy.speak`) で
テストを割ったが、時計とバイト列の口はどちらも同じものを要る。片方だけに
置くともう片方が import しに行くことになるので、ここに置く。

`TimeFrozen` は `buddy.speak_stream.time` (stall の期限) と `buddy.speak.time`
(途切れの診断) の両方を差し替える。

ブロックの大きさを `buddy.speak._BLOCK` から直接引くので、basedpyright の
private-member の検査はこのファイルごと切ってある
(冒頭の `reportPrivateUsage=false`)。
"""

import unittest
from typing import TYPE_CHECKING

from buddy import speak, speak_stream
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


class FakeSpeaker:
    """M5.Speaker のうち player が触る面。実測に合わせてある。

    1 チャンネルの枠は 2 つ (再生中 + 次) で、`isPlaying(ch)` は埋まり具合を
    0 / 1 / 2 で返す。本物の `playRaw` は満杯だと待つ (False は返さない) ので、
    fake は満杯で呼ばれたら `overfilled` を立てる — 実機なら UI が止まっている。
    渡されたチャンネルは全部記録する (-1 は並列に鳴ってしまう)。
    """

    def __init__(self, volume: int = 64) -> None:
        self.queued: list[bytes] = []
        self.handed: list[bytes] = []
        self.channels: list[int] = []
        self.overfilled = 0
        self.refuse = False
        self.stopped = 0
        self.begun = 0
        # M5Unified's own default, which is what the player multiplies.
        self.volume = volume

    def begin(self) -> bool:
        self.begun += 1
        return True

    def getVolume(self) -> int:
        return self.volume

    def setVolume(self, master_volume: int) -> None:
        self.volume = master_volume

    def isPlaying(self, _channel: int) -> int:
        return len(self.queued)

    def playRaw(
        self,
        data: bytes,
        _rate: int,
        _stereo: bool,
        _repeat: int,
        channel: int,
        _stop_current: bool,
    ) -> bool:
        self.channels.append(channel)
        if self.refuse:
            return False
        if len(self.queued) >= 2:
            self.overfilled += 1
            return False
        self.queued.append(bytes(data))
        self.handed.append(data)
        return True

    def drain(self, n: int = 1) -> None:
        """再生が進んだことにする。n ブロックぶん鳴り終わる。"""
        self.queued = self.queued[n:]

    def stop(self) -> None:
        self.stopped += 1
        self.queued = []


class RecordingTransport:
    """speak.end が流れる先。送られた行を溜めるだけ。"""

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def send_line(self, payload: bytes) -> bool:
        self.sent.append(payload)
        return True


class TimeFrozen(unittest.TestCase):
    """Base: every test here drives the device modules' clock by hand.

    `buddy.speak_stream` (stall の期限) と `buddy.speak` (途切れの診断) の
    両方を差し替える。片方だけだと、もう片方が CPython の `time` を叩いて
    `ticks_ms` が無いと落ちる。
    """

    def setUp(self) -> None:
        for mod in (speak_stream, speak):
            real = mod.time
            mod.time = FakeTime()
            self.addCleanup(setattr, mod, "time", real)
        FakeTime.now = 0
