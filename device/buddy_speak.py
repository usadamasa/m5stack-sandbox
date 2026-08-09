"""Play PCM the host streams over the serial link.

There is no speech synthesis here and there could not be: the ESP32-S3
has neither the flash for a Japanese voice nor the cycles to run one.
The host synthesises (`host/buddy_speech.py`) and this end is a pipe
into `M5.Speaker`.

### Protocol

    host -> {"cmd":"speak.begin","rate":16000,"block":2048,"blocks":29,
             "text":"..."}
    dev  <- {"ack":"speak.begin","ok":true,"bytes":59392}
    host -> 59392 raw bytes, no framing, in whole blocks
    dev  <- {"ack":"speak.end","ok":true,"blocks":29,"stalls":0}

The payload carries no sentinel and is not JSON. That is the point:
base64 inside a JSON line would cost a third of the bandwidth and a
parse per block, and the transport has a mode for exactly this — see
the bulk-mode note in `buddy_serial.py`. The host declares the length
first so the blocking read is safe, and pads to a whole block so the
device is never left waiting inside one.

`speak.stop` cannot arrive mid-transfer, because line parsing is
suspended while the payload is in flight. A transfer that dies takes
`_BULK_STALL_MS` to give up and then releases the link on its own.

### Timing

Measured: the link carries 182 KiB/s and 16 kHz 16-bit mono consumes
32 KB/s, so the host is about five times faster than playback. The
brake is `M5.Speaker`'s own queue — eight buffers, roughly a second of
audio, and `playRaw` returns False rather than blocking once it is
full. So `pump()` reads at most one block per call and stops feeding
when the queue refuses; the host's write() blocks on a full USB buffer
in turn, which is the whole of the flow control.

One block is 2048 bytes = 64 ms of audio and takes ~11 ms to read. The
app's loop runs every 40 ms. Playback stays ahead as long as `pump()`
is called from that loop, which is why this deliberately does not drain
in a `while` — a loop here would freeze the UI for the length of the
utterance.

### MicroPython

No `typing`, no `__future__`, no slice deletion. The speaker and the
transport are both injectable so the sequencing is testable on the host
without a board — see `host/tests/test_speak.py`.
"""

import json

# Refuse anything longer than this in one utterance. At 16 kHz 16-bit
# it is 30 seconds, which is far more than a notification and far less
# than enough to wedge the device for a noticeable time.
_MAX_BYTES = 960000

# Blocks held between `bulk_read` and `playRaw`. The speaker's own queue
# is the real buffer; this only covers the case where it fills between
# one tick and the next.
_MAX_HELD = 2

_DEFAULT_RATE = 16000
_DEFAULT_BLOCK = 2048

# `pump()` moves one block per tick and the app's loop runs every 40 ms,
# so a block has to hold more than 40 ms of audio or playback starves no
# matter how fast the link is. At 16 kHz 16-bit that is 1280 bytes;
# round up to a power of two and reject anything smaller, because the
# symptom — audio that stutters on some phrases and not others — is a
# miserable thing to debug from the other end of a cable.
_MIN_BLOCK = 2048

# M5.Speaker.playRaw(data, rate, stereo, repeat, channel, stop_current).
# Named here rather than inline so the call sites read as prose.
_MONO = False
_ONCE = 1
_ANY_CHANNEL = -1


def _default_speaker():
    """The real speaker. Imported lazily so the host can import this."""
    import M5

    return M5.Speaker


class SpeechPlayer:
    """Streams one utterance at a time from the link into the speaker."""

    def __init__(self, transport, speaker=None) -> None:
        self._t = transport
        self._spk = speaker if speaker is not None else _default_speaker()

        self.active = False
        self.text = ""
        self._rate = _DEFAULT_RATE
        self._block = _DEFAULT_BLOCK
        self._blocks_left = 0
        self._blocks_total = 0
        self._held = []  # type: list[bytes]
        self._stalls = 0

    # ----- command dispatch

    def handle_raw(self, raw):
        """Parse one wire line and dispatch it. None if it is not ours."""
        try:
            msg = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
        except (ValueError, UnicodeError):
            return None
        if not isinstance(msg, dict):
            return None
        return self.handle(msg)

    def handle(self, msg):
        """Dispatch one parsed command. None if the cmd is not ours."""
        cmd = msg.get("cmd")
        if cmd == "speak.begin":
            return self._begin(msg)
        if cmd == "speak.stop":
            self.stop()
            return {"ack": "speak.stop", "ok": True}
        return None

    def _begin(self, msg) -> dict:
        rate = int(msg.get("rate", _DEFAULT_RATE))
        block = int(msg.get("block", _DEFAULT_BLOCK))
        blocks = int(msg.get("blocks", 0))
        total = block * blocks

        if rate < 4000 or rate > 48000:
            return {"ack": "speak.begin", "ok": False, "err": "rate out of range"}
        if block < _MIN_BLOCK or block % 2:
            # Odd sizes would split a 16-bit sample across two blocks;
            # small ones starve playback. See _MIN_BLOCK.
            return {"ack": "speak.begin", "ok": False, "err": "bad block size"}
        if blocks < 1 or total > _MAX_BYTES:
            return {"ack": "speak.begin", "ok": False, "err": "length out of range"}

        # Whatever was playing loses; the host asked for something new.
        self.stop()

        self._rate = rate
        self._block = block
        self._blocks_left = blocks
        self._blocks_total = blocks
        self._stalls = 0
        self.text = msg.get("text", "")
        self.active = True
        # Ordered last: the ack is what releases the host to start
        # writing, and it must not go out before we can receive.
        self._t.bulk_begin(total)
        return {"ack": "speak.begin", "ok": True, "bytes": total}

    def stop(self) -> None:
        """Silence the speaker and abandon any transfer in flight."""
        if self.active:
            self._t.bulk_end()
        self.active = False
        self._held = []
        self._blocks_left = 0
        self.text = ""
        try:
            self._spk.stop()
        except Exception as e:
            print("buddy_speak: stop failed:", e)

    # ----- main-loop pump

    def pump(self) -> None:
        """Move at most one block from the link into the speaker.

        Called every tick. One block per call on purpose: see the timing
        note in the module docstring.
        """
        if not self.active:
            return

        self._drain_held()

        if len(self._held) >= _MAX_HELD:
            # Speaker is behind. Leave the bytes on the wire — the
            # host's write blocks once the USB buffer fills, which is
            # the backpressure we want.
            self._stalls += 1
            return

        if self._blocks_left > 0:
            block = self._t.bulk_read(self._block)
            if block is None:
                if not self._t.bulk_active:
                    # The transport gave up on a stalled transfer.
                    self._finish(False)
                return
            self._blocks_left -= 1
            self._held.append(block)
            self._drain_held()

        if self._blocks_left <= 0 and not self._held:
            self._finish(True)

    def _drain_held(self) -> None:
        while self._held:
            if not self._play(self._held[0]):
                return
            # Rebind rather than pop(0): MicroPython's list has pop, but
            # the transcript-length lists elsewhere in this bundle use
            # the same idiom and consistency is worth more than the
            # allocation here.
            self._held = self._held[1:]

    def _play(self, block) -> bool:
        try:
            return bool(self._spk.playRaw(block, self._rate, _MONO, _ONCE, _ANY_CHANNEL, False))
        except Exception as e:
            print("buddy_speak: playRaw failed:", e)
            return True  # drop it rather than wedge the queue

    def _finish(self, ok) -> None:
        blocks = self._blocks_total
        stalls = self._stalls
        self.active = False
        self._held = []
        self._blocks_left = 0
        self._t.bulk_end()
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
