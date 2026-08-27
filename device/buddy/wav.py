"""WAV コンテナ。バイト列のどこから samples が始まるかを言う。

`buddy/tts.py` から切り出した。engine との HTTP のやりとりはあちらに残り、
こちらは RIFF/WAVE の並びを歩いて PCM の頭を見つける係。socket から読む
以上のことはせず、ネットワークも speaker も知らない。依存は
`tts` -> `wav` の一方向。

ヘッダを見つけるには samples を少し読み過ぎるしかない。読み過ぎたぶんは
捨てずに `PrefixedStream` が抱えて、次の読み手へ先に返す。`open_pcm` は
その「読む・解く・包む」をひとまとめにしたもので、`tts.fetch_speech` は
これを 1 回呼ぶだけになる。
"""

import struct

# 型検査だけの import。デバイスの上では `False` なので走らない。事情と
# 使い方は `device/typings/buddy_types.pyi` の docstring にある。
_TYPE_CHECKING = False
if _TYPE_CHECKING:
    from buddy_types import SocketStream  # noqa: F401

_RIFF = b"RIFF"
_WAVE = b"WAVE"
_FMT = b"fmt "
_DATA = b"data"

# WAVE_FORMAT_PCM. Anything else in this field means the samples are
# companded or compressed, and playRaw would render it as noise.
_FORMAT_PCM = 1

# What M5.Speaker.playRaw takes. Not a preference: handing it stereo
# plays at double speed, and 8-bit plays as noise, and neither raises.
_CHANNELS = 1
_BITS = 16

# RIFF header floor: "RIFF" + size + "WAVE".
_RIFF_HEAD = 12
# Chunk header: four-byte id + four-byte size.
_CHUNK_HEAD = 8

# Little-endian chunk size, and the head of a PCM `fmt ` body:
# format tag, channels, sample rate, byte rate, block align, bits.
_SIZE = "<I"
_FMT_BODY = "<HHIIHH"
_FMT_MIN = struct.calcsize(_FMT_BODY)

# How much of the stream to pull before looking for `data`. VOICEVOX
# puts it at 44; the slack is for a build that inserts a chunk we do
# not care about. Anything read past the header is PCM and is kept —
# see `PrefixedStream` — so a generous probe costs nothing but a
# transient buffer.
_HEAD_BYTES = 512


class WavError(ValueError):
    """The stream is not 16-bit mono PCM, or is not a WAV at all."""


def _parse_fmt(head, body, size):
    # type: (bytes, int, int) -> tuple[int, int, int]
    """Read one fmt chunk. Returns (channels, rate, bits) or raises."""
    if size < _FMT_MIN or body + _FMT_MIN > len(head):
        raise WavError("truncated fmt chunk")
    # One unpack rather than four offset reads. `bits` in particular
    # sits at +14, which is only obvious from the field list the format
    # string spells out.
    audiofmt, channels, rate, _byte_rate, _align, bits = struct.unpack_from(_FMT_BODY, head, body)
    if audiofmt != _FORMAT_PCM:
        raise WavError("not PCM")
    if channels != _CHANNELS or bits != _BITS:
        raise WavError("need 16-bit mono, got " + str(channels) + "ch/" + str(bits) + "-bit")
    return channels, rate, bits


def parse_wav_header(head):
    # type: (bytes) -> dict[str, int]
    """Locate the samples in the head of a WAV stream.

    Returns ``{"offset", "bytes", "rate", "channels", "bits"}`` — where
    the PCM starts, how much of it there is, and the format it is in.

    `head` is a prefix of the stream, not the whole thing. The caller
    reads a bounded number of bytes and hands them here; a `data` chunk
    that starts past the end of that prefix raises rather than being
    silently reported as empty.

    The chunks are walked rather than assuming the usual 44-byte layout.
    VOICEVOX 0.25.2 does emit exactly that (measured), but a fixed skip
    would put the whole audio path on a bet about a version we do not
    control — and the walk is what lets `rate` come from the file. That
    matters more than it looks: `outputSamplingRate` is a request the
    engine may decline, and playing 24 kHz samples at 16 kHz is the
    wrong pitch for the entire utterance rather than a glitch.
    """
    if len(head) < _RIFF_HEAD or head[0:4] != _RIFF or head[8:12] != _WAVE:
        raise WavError("not a RIFF/WAVE stream")

    rate = 0
    channels = 0
    bits = 0
    pos = _RIFF_HEAD

    while pos + _CHUNK_HEAD <= len(head):
        cid = bytes(head[pos : pos + 4])
        (size,) = struct.unpack_from(_SIZE, head, pos + 4)
        body = pos + _CHUNK_HEAD

        if cid == _FMT:
            channels, rate, bits = _parse_fmt(head, body, size)
        elif cid == _DATA:
            if not rate:
                # No fmt yet means no sample rate, and the only way to
                # carry on would be to guess one.
                raise WavError("data chunk before fmt")
            return {
                "offset": body,
                "bytes": size,
                "rate": rate,
                "channels": channels,
                "bits": bits,
            }

        # Odd-sized chunks are padded to an even boundary and the pad is
        # not counted in the size field. Skipping it reads every
        # subsequent chunk id one byte late, which finds nothing.
        pos = body + size + (size & 1)

    raise WavError("no data chunk in the first " + str(len(head)) + " bytes")


def read_exactly(stream, n):
    # type: (SocketStream, int) -> bytes
    """Up to `n` bytes. Short only at end of stream."""
    buf = b""
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


class PrefixedStream:
    """A stream that yields `prefix` before anything from `rest`.

    Reading the header has to over-read — the only way to find where the
    samples start is to have some of them in hand. Those bytes are the
    first audio of the utterance, so they are held here and handed back
    before the socket is touched again. Dropping them instead would put
    an audible click at the start of every line.
    """

    def __init__(self, prefix, rest):
        # type: (bytes, SocketStream) -> None
        self._prefix = prefix
        self._rest = rest

    def read(self, n):
        # type: (int) -> bytes | None
        """`None` はまだ来ていないというだけの意味。読み手 (`StreamSource`)
        はそれをエラーではなく「次の tick で来い」として扱う。"""
        if self._prefix:
            take = self._prefix[:n]
            # Rebind rather than slice-delete: MicroPython's bytes are
            # immutable and its bytearray has no `del b[:n]`.
            self._prefix = self._prefix[len(take) :]
            return take
        return self._rest.read(n)

    def settimeout(self, seconds: float) -> None:
        """Forwarded so the player can stop the socket blocking on it.

        `buddy.speak_stream.StreamSource` sets this on whatever stream it is
        handed. Without the forward it would land here and do nothing,
        and the first read past the buffered prefix would block the UI.
        """
        self._rest.settimeout(seconds)

    def close(self) -> None:
        try:
            self._rest.close()
        except Exception as e:
            print("buddy.wav: stream close failed:", e)


def open_pcm(stream):
    # type: (SocketStream) -> tuple[dict[str, int], PrefixedStream]
    """`stream` の頭を読んで、素性と samples の読み口を返す。

    返すのは `(info, pcm)` — `parse_wav_header` が見つけたものと、最初の
    sample に位置付けた読み口。頭が WAV でなければ、あるいは 16-bit mono
    PCM でなければ `WavError`。それが何を意味するかは呼ぶ側が決める
    (`buddy.tts` は engine の答えが使えないという扱いにする)。
    """
    head = read_exactly(stream, _HEAD_BYTES)
    info = parse_wav_header(head)
    return info, PrefixedStream(head[info["offset"] :], stream)
