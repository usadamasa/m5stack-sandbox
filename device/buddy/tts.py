"""Fetch speech from a VOICEVOX engine over WiFi.

The device used to be handed PCM over the USB cable, synthesised on the
Mac by `say`. It now asks for its own audio instead: the Mac runs
`voicevox/voicevox_engine` in Docker and this end streams the result
straight into `M5.Speaker`. The voice is Zundamon (`speaker=3`).

### Why the engine and not a hosted API

The public VOICEVOX-compatible hosts are HTTPS-only, and a TLS handshake
costs this chip about 40 KB of heap it would rather spend on audio. They
also answer with a job to poll rather than a stream, and their download
URLs 404 until synthesis finishes. A local engine is plain HTTP, answers
immediately, and — the part that matters most here — takes
`outputSamplingRate`, so the rate is the host's choice rather than the
engine's (`buddy_verbs.DEFAULT_RATE` says which and why).

### Who brings the radio up

Not this module, and not the app. `/flash/main.py` connects at boot,
before either exists, using the credentials in `/flash/wifi_event.py` —
which `host/provision_wifi.py` writes once.

It has to be that way round. Measured on hardware, `connect()` from
inside the running app is accepted and then never completes: the
ESP-IDF heap has around 12 KB free in its largest region once the
bundle is loaded, and bringing a link up wants DRAM. The app inherits a
link; it cannot make one.

So there is no `net.*` command and nothing here touches `network`. If
the radio is down, the first POST below fails with an OSError and the
utterance is lost — which is the correct and only outcome, since this
end cannot fix it.

### The two calls

    POST /audio_query?text=...&speaker=3   -> a JSON query (~2.7 KB)
    POST /synthesis?speaker=3              -> audio/wav, Content-Length set

`outputSamplingRate` is edited into the query between the two. Measured
against VOICEVOX 0.25.2: 2.56 s of speech is 122924 bytes at the default
24 kHz and 81964 at 16 kHz.

### MicroPython

No `typing`, no `__future__`, no slice deletion, no `contextlib`. The
transport is injectable so `host/tests/test_tts.py` can exercise all of
this without a board or a running engine.
"""

import json
import struct

# 型検査だけの import。デバイスの上では `False` なので走らない。事情と
# 使い方は `device/typings/buddy_types.pyi` の docstring にある。
_TYPE_CHECKING = False
if _TYPE_CHECKING:
    from buddy_types import (  # noqa: F401
        HttpClient,
        HttpResponse,
        SocketStream,
        SpeechSource,
    )

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


# Characters RFC 3986 says need no escaping. Everything else in the
# text — which is Japanese, so almost all of it — travels as %XX.
_UNRESERVED = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"

# How much of the stream to pull before looking for `data`. VOICEVOX
# puts it at 44; the slack is for a build that inserts a chunk we do
# not care about. Anything read past the header is PCM and is kept —
# see `_PrefixedStream` — so a generous probe costs nothing but a
# transient buffer.
_HEAD_BYTES = 512

# Longest utterance we will start. Matches `buddy.speak._MAX_BYTES`:
# 30 s at 16 kHz 16-bit (20 s at 24 kHz), far more than a notification
# and far less than enough to hold the speaker and the link for a
# noticeable time. Kept as
# its own constant rather than imported so neither module has to load
# the other on a device counting every kilobyte.
_MAX_BYTES = 960000

# Attempts per HTTP call, not retries on top of one. WiFi drops a
# request now and then and the engine is a laptop that may be busy;
# a second attempt costs about a second, and failing costs the whole
# utterance. Availability beyond this is explicitly out of scope.
_TRIES = 3


class WavError(ValueError):
    """The stream is not 16-bit mono PCM, or is not a WAV at all."""


class FetchError(RuntimeError):
    """The engine could not be reached, or gave us something unusable."""


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


# ----- the HTTP side


def quote(text: str) -> str:
    """Percent-encode `text` for a query parameter.

    MicroPython has no `urllib.parse`. The text is Japanese, so nearly
    every byte needs escaping — and an unescaped `&` or `=` would split
    one parameter into two and have the engine synthesise the wrong
    line rather than fail.
    """
    out = []  # type: list[str]
    for b in text.encode("utf-8"):
        if b in _UNRESERVED:
            out.append(chr(b))
        else:
            out.append("%%%02X" % b)
    return "".join(out)


def tune_query(query, rate):
    # type: (dict[str, object], int) -> dict[str, object]
    """Edit the engine's audio_query in place, and hand it back.

    Only the two output settings are touched. `accent_phrases` carries
    the engine's own reading of the text — rebuilding any of it here
    would change how the line is pronounced.

    In place rather than copied: the query is a few kilobytes of nested
    dicts and this board has 61 KB of heap.
    """
    query["outputSamplingRate"] = rate
    query["outputStereo"] = False
    return query


class _PrefixedStream:
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
            print("buddy.tts: stream close failed:", e)


def _default_requests():
    """The firmware's HTTP client.

    Frozen into this build as `requests` — the name `urequests` was
    retired upstream, which is why the reference implementations that
    predate MicroPython 1.20 import the other one.
    """
    import requests

    return requests


def _post(req, url, data, headers):
    # type: (HttpClient, str, bytes | None, dict[str, str] | None) -> HttpResponse
    """POST with retries. Raises FetchError once the attempts run out.

    `req` is the firmware's `requests` module or a test double — no
    `typing.Protocol` on MicroPython to name what the two have in common,
    so the calls through it below are ignored per-line, and the response
    it hands back stays equally duck-typed for the same reason.
    """
    last = None
    for _ in range(_TRIES):
        try:
            res = req.post(url, data=data, headers=headers)
        except Exception as e:  # OSError, and whatever the TLS-less stack raises
            last = e
            continue
        status = getattr(res, "status_code", 200)
        if status != 200:
            last = "HTTP " + str(status)
            try:
                res.close()
            except Exception:
                pass
            continue
        return res
    raise FetchError("POST failed after " + str(_TRIES) + " tries: " + str(last))


def _read_exactly(stream, n):
    # type: (SocketStream, int) -> bytes
    """Up to `n` bytes. Short only at end of stream."""
    buf = b""
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def fetch_speech(url, text, speaker=3, rate=24000, requests_mod=None):
    # type: (str, str, int, int, HttpClient | None) -> SpeechSource
    """Ask VOICEVOX for `text` and return a stream of its samples.

    Returns ``{"stream", "bytes", "rate", "response"}``: a reader
    already positioned at the first sample, how many bytes of PCM to
    expect, the rate the engine actually used, and the response to close
    when playback ends.

    Blocks for as long as synthesis takes — seconds. The caller's UI
    loop stops for that time; there is no way around it without a
    non-blocking HTTP client, and the alternative to blocking is a
    second thread, which on this build shares a GIL and would not help.
    """
    if not text.strip():
        raise FetchError("nothing to say")

    req = requests_mod if requests_mod is not None else _default_requests()
    base = url.rstrip("/")
    speaker_arg = "speaker=" + str(int(speaker))

    # `res` is whatever `req.post()` handed back — equally duck-typed, so
    # every member access on it below is ignored per-line for the same
    # reason as `_post()`'s own body.
    res = _post(req, base + "/audio_query?text=" + quote(text) + "&" + speaker_arg, None, None)
    try:
        query = res.json()
    finally:
        try:
            res.close()
        except Exception:
            pass

    body = json.dumps(tune_query(query, rate)).encode("utf-8")
    # The query is the largest thing we hold at once. Let it go before
    # the audio starts arriving.
    query = None

    res = _post(
        req,
        base + "/synthesis?" + speaker_arg,
        body,
        {"Content-Type": "application/json"},
    )
    body = None

    raw = res.raw
    head = _read_exactly(raw, _HEAD_BYTES)
    try:
        info = parse_wav_header(head)
    except WavError as e:
        try:
            res.close()
        except Exception:
            pass
        raise FetchError("engine did not answer with playable audio: " + str(e))

    if info["bytes"] < 1 or info["bytes"] > _MAX_BYTES:
        try:
            res.close()
        except Exception:
            pass
        raise FetchError("utterance is " + str(info["bytes"]) + " bytes, out of range")

    return {
        "stream": _PrefixedStream(head[info["offset"] :], raw),
        "bytes": info["bytes"],
        "rate": info["rate"],
        "response": res,
    }
