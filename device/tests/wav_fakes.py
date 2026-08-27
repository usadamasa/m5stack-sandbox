"""WAV を読むテストが共有する組み立て道具と fake。

`test_wav.py` (コンテナを解くところ) と `test_tts.py` (engine とのやりとり)
に割れたが、engine が返すバイト列を作る側はどちらも同じものが要る。片方に
置くともう片方が import しに行くことになるので、ここに置く。

`FakeRaw` は socket の形をした読み口。`PrefixedStream` が `settimeout` を
無条件で転送するので、本物の socket と同じくそれを持っている必要がある。
"""

import struct


def chunk(cid: bytes, body: bytes) -> bytes:
    """One RIFF chunk, padded to an even length as the spec requires."""
    return cid + struct.pack("<I", len(body)) + body + (b"\x00" if len(body) & 1 else b"")


def fmt(rate: int = 16000, channels: int = 1, bits: int = 16) -> bytes:
    return struct.pack(
        "<HHIIHH",
        1,  # PCM
        channels,
        rate,
        rate * channels * bits // 8,
        channels * bits // 8,
        bits,
    )


def wav_head(
    pcm_bytes: int,
    before_data: bytes = b"",
    rate: int = 16000,
    channels: int = 1,
    bits: int = 16,
) -> bytes:
    """A header describing `pcm_bytes` of samples. The samples themselves
    are not appended: the parser only ever sees the head of the stream."""
    body = b"WAVE" + chunk(b"fmt ", fmt(rate, channels, bits)) + before_data
    body += b"data" + struct.pack("<I", pcm_bytes)
    return b"RIFF" + struct.pack("<I", len(body) + pcm_bytes) + body


class FakeRaw:
    """A socket-shaped object holding a fixed stream."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.closed = False
        self.timeout: float | None = None

    def read(self, n: int) -> bytes | None:
        # 名前が `chunk` でないのは、このモジュールの `chunk()` を隠すため。
        taken = self.data[self.pos : self.pos + n]
        self.pos += len(taken)
        return taken

    def settimeout(self, seconds: float) -> None:
        self.timeout = seconds

    def close(self) -> None:
        self.closed = True
