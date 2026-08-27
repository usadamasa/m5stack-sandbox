"""Play speech the device fetched for itself.

There is no synthesis here and there could not be: the ESP32-S3 has
neither the flash for a Japanese voice nor the cycles to run one. What
changed is where the audio comes from. It used to arrive over the USB
cable as PCM the Mac had already synthesised; it now comes off WiFi from
a VOICEVOX engine the device calls itself (`buddy/tts.py`). This module
is the part that turns that stream into sound.

### Protocol

    host -> {"cmd":"speak.say","text":"...","url":"http://host:50021",
             "speaker":3,"rate":16000}
    dev  <- {"ack":"speak.say","ok":true,"bytes":81920,"rate":16000}
    dev  <- {"ack":"speak.end","ok":true,"blocks":40,"stalls":0}

The `speak.say` ack goes out once synthesis is done and playback has
begun, not when the request was accepted — the two POSTs to the engine
block for seconds and the app's loop stops for that time. Everything
after that ack runs a block per tick, so the UI comes back.

`speak.stop` abandons whatever is in flight.

### Timing

Measured: 16 kHz 16-bit mono consumes 32 KB/s. The brake is
`M5.Speaker`'s own queue — eight buffers, roughly a second of audio, and
`playRaw` returns False rather than blocking once it is full. So
`pump()` reads at most one block per call and stops feeding when the
queue refuses.

One block is 2048 bytes = 64 ms of audio. The app's loop runs every
40 ms. Playback stays ahead as long as `pump()` is called from that
loop, which is why this deliberately does not drain in a `while` — a
loop here would freeze the UI for the length of the utterance.

### Why the source is separate

WiFi is not the USB cable. The old path had the host declare a length
and write whole blocks, padded, and a short block would park the device
inside a blocking read. A socket has no such contract: bytes arrive when
they arrive, the last block of an utterance is almost never whole, and
the far end can simply stop. `buddy/speak_stream.py` is where all of
that is dealt with, so `pump()` below stays the same loop it always was.

### MicroPython

No `typing`, no `__future__`, no slice deletion, no exception chaining.
The speaker, the transport and the fetch are all injectable so the
sequencing is testable on the host without a board — see
`host/tests/test_speak.py`.
"""

import json

from buddy import speak_stream

# 型検査だけの import。デバイスの上では `False` なので走らない。事情と
# 使い方は `device/typings/buddy_types.pyi` の docstring にある。
_TYPE_CHECKING = False
if _TYPE_CHECKING:
    from buddy_types import AckSink, Fetch, Speaker  # noqa: F401

# Refuse anything longer than this in one utterance. At 16 kHz 16-bit
# it is 30 seconds, which is far more than a notification and far less
# than enough to wedge the device for a noticeable time. `buddy.tts`
# carries the same number for the same reason.
_MAX_BYTES = 960000

# Blocks held between `read_block` and `playRaw`. The speaker's own queue
# is the real buffer; this only covers the case where it fills between
# one tick and the next.
_MAX_HELD = 2

_DEFAULT_RATE = 16000
_DEFAULT_BLOCK = 2048
_DEFAULT_SPEAKER = 3

# `pump()` moves one block per tick and the app's loop runs every 40 ms,
# so a block has to hold more than 40 ms of audio or playback starves no
# matter how fast the link is. At 16 kHz 16-bit that is 1280 bytes;
# round up to a power of two, because the symptom — audio that stutters
# on some phrases and not others — is a miserable thing to debug from
# the other end of a cable.
_BLOCK = 2048

# M5.Speaker.playRaw(data, rate, stereo, repeat, channel, stop_current).
# Named here rather than inline so the call sites read as prose.
_MONO = False
_ONCE = 1
_ANY_CHANNEL = -1

# How much louder than the firmware's own setting the speaker is driven.
# The Cardputer-Adv's is a small one and M5Unified starts it quiet
# enough that an utterance is hard to make out across a desk.
#
# Relative, not a fixed byte: the default belongs to M5Unified and moves
# with the firmware, so reading it and multiplying keeps this saying
# what was actually wanted. Master volume scales the samples before the
# amp; measured, the default is 64 of 255, so this lands on the cap
# below — 255 is as loud as the master volume goes, and anything past
# it is the same setting under a different name.
_VOLUME_GAIN = 4

# setVolume takes a byte.
_MAX_VOLUME = 255


def _default_speaker():
    """The real speaker. Imported lazily so the host can import this."""
    import M5

    return M5.Speaker


def _boost_volume(spk):
    # type: (Speaker) -> int | None
    """Turn `spk` up by `_VOLUME_GAIN`. Returns the volume now set.

    Called once, when the player is built. That is once per boot, which
    is what makes multiplying safe: the firmware sets the volume back to
    its own default on every reset, and re-importing the app inside a
    live session fails on memory long before it gets here.

    Never raises. A board whose build has no volume control still plays
    audio, and losing the utterance over the setting for it would be the
    wrong trade.

    `spk` は本物の M5.Speaker かテストの double。両者に共通の base は無いので、
    面は `.pyi` 側の `Speaker` Protocol で押さえる。
    """
    try:
        before = spk.getVolume()
    except Exception as e:
        print("buddy.speak: getVolume failed:", e)
        return None
    after = before * _VOLUME_GAIN
    if after > _MAX_VOLUME:
        after = _MAX_VOLUME
    try:
        spk.setVolume(after)
    except Exception as e:
        print("buddy.speak: setVolume failed:", e)
        return before
    print("buddy.speak: volume", before, "->", after)
    return after


def _default_fetch():
    """The real fetch. Lazy for the same reason as the speaker."""
    from buddy import tts as buddy_tts

    return buddy_tts.fetch_speech


# ----- ワイヤから来た値の絞り込み
#
# 届いた命令は `dict[str, object]` で、値の型は名乗らない。`fetch` は
# str/str/int/int を要る。実行時はいつもそうなっているが、そう言い直す手
# (`typing.cast`) が MicroPython には無いので、実際に絞る。
#
# 変換の意味は元のまま。以前は `str` へは何もせず素通しし、`rate` だけ
# `int()` に通していた。壊れた値がここを抜けても、その先の `fetch` が
# 例外を投げて `_say` の try が ack の err に載せる。


def _str_of(msg, key):
    # type: (dict[str, object], str) -> str
    """本文と URL。str でない値は `str()` に通す — `ChatPanel.say` が
    transcript へ積むときと同じ扱い。"""
    value = msg.get(key, "")
    return value if isinstance(value, str) else str(value)


def _int_of(msg, key, default):
    # type: (dict[str, object], str, int) -> int
    """`int(msg[key])`。

    数にならない値では投げる。黙って既定へ落とすと、ホストは頼んだ
    rate と違う音を聞くまで間違いに気付けない。
    """
    value = msg.get(key, default)
    if isinstance(value, (int, float, str)):
        # str も通すのは元のままで、`int("16000")` は成立する。数にならない
        # str は ValueError になり、それも呼び出し側の try が受ける。
        return int(value)
    raise TypeError("not a number: " + repr(value))


class SpeechPlayer:
    """Fetches one utterance at a time and streams it into the speaker."""

    # `transport` / `speaker` / `fetch` は注入で受け取る相手 (本物か double)。
    # どれも共通の base を持たないので、面は `.pyi` 側の Protocol で押さえ、
    # 名前は `# type:` コメントから引く — ここの注釈は組み込みの名前 1 つで
    # なければならない (`device/tests/test_device_constraints.py`)。
    def __init__(
        self,
        transport,  # type: AckSink
        speaker=None,  # type: Speaker | None
        fetch=None,  # type: Fetch | None
    ) -> None:
        self._t = transport
        self._spk = speaker if speaker is not None else _default_speaker()
        self._fetch = fetch
        self.volume = _boost_volume(self._spk)

        self.active = False
        self.text = ""
        self._rate = _DEFAULT_RATE  # type: int
        self._block = _DEFAULT_BLOCK  # type: int
        self._source = None
        self._blocks_total = 0  # type: int
        self._blocks_done = 0  # type: int
        self._held = []  # type: list[bytes]
        self._stalls = 0  # type: int

    # ----- command dispatch

    def handle_raw(self, raw):
        # type: (bytes | bytearray | str) -> dict[str, object] | None
        """Parse one wire line and dispatch it. None if it is not ours."""
        try:
            msg = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
        except (ValueError, UnicodeError):
            return None
        if not isinstance(msg, dict):
            return None
        # json.loads() is untyped, so isinstance() only narrows `msg` to
        # dict[Unknown, Unknown] rather than the dict[str, object] handle()
        # declares. Runtime-safe regardless: the isinstance check above
        # already guarantees this is a dict.
        return self.handle(msg)  # pyright: ignore[reportUnknownArgumentType]

    def handle(self, msg):
        # type: (dict[str, object]) -> dict[str, object] | None
        """Dispatch one parsed command. None if the cmd is not ours."""
        cmd = msg.get("cmd")
        if cmd == "speak.say":
            return self._say(msg)
        if cmd == "speak.stop":
            self.stop()
            return {"ack": "speak.stop", "ok": True}
        return None

    def _say(self, msg):
        # type: (dict[str, object]) -> dict[str, object]
        text = _str_of(msg, "text")
        url = _str_of(msg, "url")
        if not url:
            return {"ack": "speak.say", "ok": False, "err": "no engine url"}

        fetch = self._fetch if self._fetch is not None else _default_fetch()

        # Whatever was playing loses; the host asked for something new.
        # Done before the fetch so the speaker is not still working
        # through a second of the last line while this one synthesises.
        self.stop()

        try:
            got = fetch(
                url,
                text,
                _int_of(msg, "speaker", _DEFAULT_SPEAKER),
                _int_of(msg, "rate", _DEFAULT_RATE),
            )
        except Exception as e:
            return {"ack": "speak.say", "ok": False, "err": str(e)}

        total = got["bytes"]
        if total < 1 or total > _MAX_BYTES:
            try:
                got["response"].close()
            except Exception:
                pass
            return {"ack": "speak.say", "ok": False, "err": "length out of range"}

        self._rate = got["rate"]
        self._block = _BLOCK
        self._source = speak_stream.StreamSource(got["stream"], total, got["response"])
        # Rounded up: the last block is padded rather than dropped.
        self._blocks_total = (total + self._block - 1) // self._block
        self._blocks_done = 0
        self._stalls = 0
        self.text = text
        self.active = True

        return {"ack": "speak.say", "ok": True, "bytes": total, "rate": self._rate}

    def stop(self) -> None:
        """Silence the speaker and abandon any transfer in flight."""
        if self._source is not None:
            self._source.close()
            self._source = None
        self.active = False
        self._held = []
        self.text = ""
        try:
            self._spk.stop()
        except Exception as e:
            print("buddy.speak: stop failed:", e)

    # ----- main-loop pump

    def pump(self) -> None:
        """Move at most one block from the stream into the speaker.

        Called every tick. One block per call on purpose: see the timing
        note in the module docstring.
        """
        if not self.active or self._source is None:
            return

        self._drain_held()

        if len(self._held) >= _MAX_HELD:
            # Speaker is behind. Leave the bytes on the socket — TCP's
            # window closes in turn, which is the backpressure we want.
            self._stalls += 1
            return

        if self._source.left > 0:
            block = self._source.read_block(self._block)
            if block is None:
                if self._source.dead:
                    self._finish(False)
                else:
                    self._stalls += 1
                return
            self._blocks_done += 1
            self._held.append(block)
            self._drain_held()

        # `self._source is None` already returned at the top of this
        # method, and nothing between there and here can reset it back —
        # basedpyright agrees, which is how this simplified from an
        # `is not None and ...` guard that had become redundant.
        if self._source.left <= 0 and not self._held:
            self._finish(True)

    def _drain_held(self) -> None:
        while self._held:
            if not self._play(self._held[0]):
                return
            # Rebind rather than pop(0): consistency with the bytearray
            # idiom this bundle uses everywhere, since MicroPython has
            # no slice deletion and mixing the two reads badly.
            self._held = self._held[1:]

    def _play(self, block: bytes) -> bool:
        try:
            return bool(self._spk.playRaw(block, self._rate, _MONO, _ONCE, _ANY_CHANNEL, False))
        except Exception as e:
            print("buddy.speak: playRaw failed:", e)
            return True  # drop it rather than wedge the queue

    def _finish(self, ok: bool) -> None:
        blocks = self._blocks_done
        stalls = self._stalls
        self.active = False
        self._held = []
        if self._source is not None:
            self._source.close()
            self._source = None
        self._t.send_line(
            json.dumps(
                {
                    "ack": "speak.end",
                    "ok": bool(ok),
                    "blocks": blocks,
                    "stalls": stalls,
                },
                separators=(",", ":"),
            ).encode("utf-8")
        )
