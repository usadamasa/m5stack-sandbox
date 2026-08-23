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
the far end can simply stop. `_StreamSource` is where all of that is
dealt with, so `pump()` below stays the same loop it always was.

### MicroPython

No `typing`, no `__future__`, no slice deletion, no exception chaining.
The speaker, the transport and the fetch are all injectable so the
sequencing is testable on the host without a board — see
`host/tests/test_speak.py`.
"""

import json
import time

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

# Silence, for padding a final block that came up short.
_PAD = b"\x00"

# How long one `read_block` call may block waiting on the socket. Half a
# tick: long enough to ride out ordinary WiFi jitter inside a single
# call, short enough that a tick which spends its whole budget here
# still lands before the next one is due. A socket left at its default
# blocks until it has the bytes, which would freeze the UI for as long
# as the network felt like it.
_READ_TIMEOUT_S = 0.02

# How long a stream may make no progress at all before we stop waiting.
# Matches the patience the old bulk transport had for a stalled USB
# transfer. Past this the AP is gone, the engine has died, or the laptop
# went to sleep, and none of those get better by waiting.
_STALL_MS = 3000

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
    # type: (object) -> int | None
    """Turn `spk` up by `_VOLUME_GAIN`. Returns the volume now set.

    Called once, when the player is built. That is once per boot, which
    is what makes multiplying safe: the firmware sets the volume back to
    its own default on every reset, and re-importing the app inside a
    live session fails on memory long before it gets here.

    Never raises. A board whose build has no volume control still plays
    audio, and losing the utterance over the setting for it would be the
    wrong trade.

    `spk` is duck-typed — the real M5.Speaker or a test double, and
    MicroPython has no `typing.Protocol` to name what the two have in
    common — so every call through it below is ignored per-line rather
    than left to cascade.
    """
    try:
        before = spk.getVolume()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    except Exception as e:
        print("buddy.speak: getVolume failed:", e)
        return None
    after = before * _VOLUME_GAIN  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    if after > _MAX_VOLUME:
        after = _MAX_VOLUME
    try:
        spk.setVolume(after)  # pyright: ignore[reportUnknownMemberType]
    except Exception as e:
        print("buddy.speak: setVolume failed:", e)
        return before  # pyright: ignore[reportUnknownVariableType]
    print("buddy.speak: volume", before, "->", after)  # pyright: ignore[reportUnknownArgumentType]
    return after  # pyright: ignore[reportUnknownVariableType]


def _default_fetch():
    """The real fetch. Lazy for the same reason as the speaker."""
    from buddy import tts as buddy_tts

    return buddy_tts.fetch_speech


class _StreamSource:
    """Turns a byte stream into whole blocks for the player.

    The player wants blocks of exactly `size` and wants them without
    waiting. A socket offers neither, so the difference is absorbed
    here: partial reads accumulate across calls, the last block of the
    utterance is padded with silence, and a stream that has stopped
    producing sets `dead` rather than leaving the player to spin.

    `left` counts bytes of PCM still owed by the stream — it reaches
    zero when the last of the declared payload has been handed over.
    """

    def __init__(self, stream, total, response=None):
        # type: (object, int, object | None) -> None
        # `stream`/`response` are duck-typed sockets or test doubles — no
        # `typing.Protocol` on MicroPython to name what they have in
        # common, so their member accesses below are ignored per-line.
        self._stream = stream
        self._response = response
        self._acc = b""  # type: bytes
        self.left = total
        self.dead = False
        self._last_progress = time.ticks_ms()

        # Without this the socket blocks until it has what was asked
        # for, and the UI loop stops with it. Absent on the test
        # doubles, and on any stream that is already a buffer.
        setter = getattr(stream, "settimeout", None)
        if setter is not None:
            try:
                setter(_READ_TIMEOUT_S)
            except Exception as e:
                print("buddy.speak: settimeout failed:", e)

    def read_block(self, size):
        # type: (int) -> bytes | None
        """One complete block of `size` bytes, or None if not here yet.

        Never short: `pump()` hands the result straight to `playRaw`,
        and a short block there is an audible click. The final block of
        an utterance is padded with silence to make that true — the
        measured 81920 bytes happens to divide by 2048, but that is
        luck, and the padding used to be the host's job before the audio
        started coming off a socket.

        Returns None when the bytes are not here yet. That is not an
        error; the next tick tries again. It becomes one after
        `_STALL_MS` without a single byte of progress, at which point
        `dead` is set and `pump()` ends the utterance as not-ok. An
        early end of stream is the same kind of failure and is treated
        the same way: the length was declared up front by
        Content-Length, so a stream that stops short has been cut off,
        and padding the gap with silence would report success for an
        utterance the listener never heard the end of.
        """
        if self.left <= 0:
            return None

        stream = self._stream
        if stream is None:
            # close() ran while an utterance was in flight. Nothing more
            # is coming, and left > 0 means it ended early.
            self.dead = True
            return None

        # The last block is short by definition. Ask only for what is
        # still owed, then pad — asking for a full block would read
        # into whatever the server sends next, or hang waiting for
        # bytes that are not coming.
        want = size if size < self.left else self.left

        had = len(self._acc)
        ended = self._fill(stream, want)

        if len(self._acc) > had:  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            self._last_progress = time.ticks_ms()

        if len(self._acc) >= want:  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            return self._take(want, size)

        self._give_up(ended)
        return None

    def _fill(self, stream, want):
        # type: (object, int) -> bool
        """Read until `want` bytes are buffered. True if the stream ended.

        Stopping without reaching `want` is the normal case, not a
        failure: the caller comes back next tick.
        """
        while len(self._acc) < want:  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            try:
                # `stream` is duck-typed (see __init__), so `.read()`'s
                # result is unavoidably Unknown, and folding it into `_acc`
                # below taints every read of `_acc` for the rest of this
                # method — ignored per-line rather than left to cascade.
                chunk = stream.read(want - len(self._acc))  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType, reportUnknownArgumentType, reportAttributeAccessIssue]
            except OSError:
                # Timed out, or would block. Indistinguishable from a
                # slow AP at this layer, and both want the same answer:
                # come back next tick. A genuine connection error looks
                # the same here and is caught by the stall deadline.
                chunk = None
            if chunk is None:
                return False
            if not chunk:
                return True
            self._acc += chunk  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType, reportUnknownArgumentType]
        return False

    def _take(self, want, size):
        # type: (int, int) -> bytes
        """Hand over one block, padded with silence if it is the last one."""
        block = self._acc[:want]  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        # Rebind rather than slice-delete: MicroPython's bytes are
        # immutable and its bytearray has no `del b[:n]`.
        self._acc = self._acc[want:]  # pyright: ignore[reportUnknownMemberType]
        # By the real bytes, not the padding — this is what tells
        # `pump()` the utterance is over.
        self.left -= want
        if len(block) < size:  # pyright: ignore[reportUnknownArgumentType]
            block = block + _PAD * (size - len(block))  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        return block  # pyright: ignore[reportUnknownVariableType]

    def _give_up(self, ended):
        # type: (bool) -> None
        """Decide whether a block that did not arrive is a failure yet."""
        if ended:
            print("buddy.speak: stream ended", self.left, "bytes short")
            self.dead = True
        elif time.ticks_diff(time.ticks_ms(), self._last_progress) > _STALL_MS:
            print("buddy.speak: stream stalled with", self.left, "bytes left")
            self.dead = True

    def close(self) -> None:
        """Let go of the socket. Safe to call twice."""
        self.left = 0
        self._acc = b""
        for obj in (self._stream, self._response):
            if obj is None:
                continue
            try:
                obj.close()  # pyright: ignore[reportUnknownMemberType]
            except Exception as e:
                print("buddy.speak: close failed:", e)
        self._stream = None
        self._response = None


class SpeechPlayer:
    """Fetches one utterance at a time and streams it into the speaker."""

    # `transport`/`speaker`/`fetch` are duck-typed dependencies (real
    # objects or test doubles) — no `typing.Protocol` on MicroPython to
    # name what the two sides of each have in common, so `transport`,
    # `speaker` and `fetch` stay unannotated and every use of them, or of
    # a field built from them, is ignored per-line below rather than left
    # to cascade silently.
    def __init__(
        self,
        transport,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        speaker=None,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        fetch=None,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    ) -> None:
        self._t = transport
        self._spk = speaker if speaker is not None else _default_speaker()  # pyright: ignore[reportUnknownMemberType]
        self._fetch = fetch if fetch is not None else None  # pyright: ignore[reportUnknownMemberType]
        self.volume = _boost_volume(self._spk)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]

        self.active = False
        self.text = ""
        # Declared plainly (rather than left to infer) so that a later
        # Unknown-tainted store — `self._rate = got["rate"]` in `_say()`,
        # where `got` is the duck-typed fetch's result — cannot widen what
        # every other method sees this field as.
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
        text = msg.get("text", "")
        url = msg.get("url", "")
        if not url:
            return {"ack": "speak.say", "ok": False, "err": "no engine url"}

        fetch = self._fetch if self._fetch is not None else _default_fetch()  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]

        # Whatever was playing loses; the host asked for something new.
        # Done before the fetch so the speaker is not still working
        # through a second of the last line while this one synthesises.
        self.stop()

        try:
            # `fetch` is duck-typed (see the class docstring note), so its
            # result and everything pulled out of it below is ignored
            # per-line.
            # msg's values are `object` (see the class docstring's dict[str,
            # object] note); `fetch`'s real signature wants str/str/int/int,
            # which they always are on the wire — ignored per-line rather
            # than narrowed with a `typing.cast` MicroPython does not have.
            got = fetch(  # pyright: ignore[reportUnknownVariableType]
                url,  # pyright: ignore[reportArgumentType]
                text,  # pyright: ignore[reportArgumentType]
                msg.get("speaker", _DEFAULT_SPEAKER),  # pyright: ignore[reportArgumentType]
                int(msg.get("rate", _DEFAULT_RATE)),  # pyright: ignore[reportArgumentType]
            )
        except Exception as e:
            return {"ack": "speak.say", "ok": False, "err": str(e)}

        total = got["bytes"]  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        if total < 1 or total > _MAX_BYTES:  # pyright: ignore[reportOperatorIssue]
            try:
                got["response"].close()  # pyright: ignore[reportUnknownMemberType]
            except Exception:
                pass
            return {"ack": "speak.say", "ok": False, "err": "length out of range"}

        self._rate = got["rate"]  # pyright: ignore[reportUnknownMemberType]
        self._block = _BLOCK
        self._source = _StreamSource(got["stream"], total, got.get("response"))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType, reportArgumentType]
        # Rounded up: the last block is padded rather than dropped.
        self._blocks_total = (total + self._block - 1) // self._block  # pyright: ignore[reportUnknownVariableType, reportOperatorIssue]
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
            self._spk.stop()  # pyright: ignore[reportUnknownMemberType]
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
            return bool(self._spk.playRaw(block, self._rate, _MONO, _ONCE, _ANY_CHANNEL, False))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
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
        self._t.send_line(  # pyright: ignore[reportUnknownMemberType]
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
