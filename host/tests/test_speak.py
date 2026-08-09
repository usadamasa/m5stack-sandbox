"""The speech path: bulk framing, block sequencing, and synthesis guards.

Three separate things can go wrong here and only one of them is audible
in an obvious way:

* the transport's bulk mode can lose or steal bytes at the seam where
  line parsing stops — the symptom is a transfer that ends short and a
  device parked inside a blocking read, recoverable only by BtnRST;
* the device-side player can starve the speaker or run ahead of it;
* `say` can exit 0 having written no audio, which is what it does
  inside the sandbox, and silence is indistinguishable from a link
  problem once it reaches the device.

All three are covered here without a board or a speaker.
"""

import unittest
from typing import Any
from unittest import mock

import buddy_speech
from buddy_bridge import BLOCK_BYTES, Message, pad_to_blocks, speak
from buddy_serial import BuddySerial
from buddy_speak import SpeechPlayer


def _blk(ch: bytes) -> bytes:
    """One block of a single repeated byte, at the real block size."""
    return ch * BLOCK_BYTES


class _FakeTime:
    """MicroPython's ticks API, frozen. buddy_serial's bulk path uses it."""

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


class _FakeStdin:
    """A byte source that hands out whatever has been fed to it.

    `readinto` mirrors the device's real behaviour as measured: it
    fills the buffer completely, and there is deliberately no way to
    make it return short. A test that relies on a short read would be
    testing something the hardware does not do.
    """

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes) -> None:
        self.buf.extend(data)

    def readinto(self, target: bytearray) -> int:
        want = len(target)
        if len(self.buf) < want:
            return 0  # stands in for "would block"; the caller retries
        target[:want] = self.buf[:want]
        self.buf = self.buf[want:]
        return want


class _FakePoller:
    def __init__(self, stdin: _FakeStdin) -> None:
        self._stdin = stdin

    def poll(self, _timeout: int = 0) -> list:
        return [(self._stdin, 1)] if self._stdin.buf else []

    def register(self, *_a: object) -> None: ...

    def unregister(self, *_a: object) -> None: ...


def _transport() -> tuple[Any, _FakeStdin]:
    """A BuddySerial wired to fakes, bypassing __init__.

    The real one registers stdin with a poller and turns Ctrl-C off,
    neither of which belongs in a unit test. Typed `Any` on the way out
    because the injection is a deliberate contract violation — the fake
    poller and the recording `send_line` do not match the real
    signatures, and pretending otherwise would need wrappers that add
    nothing.
    """
    t: Any = BuddySerial.__new__(BuddySerial)
    stdin = _FakeStdin()
    t._stdin = stdin
    t._poller = _FakePoller(stdin)
    t._rx_buf = bytearray()
    t._shutting_down = False
    t._host_seen = True
    t._last_rx_ms = 0
    t._bulk_left = 0
    t._bulk_acc = bytearray()
    t._bulk_deadline = 0
    return t, stdin


class BulkTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        import buddy_serial

        self._real_time = buddy_serial.time
        buddy_serial.time = _FakeTime()
        self.addCleanup(setattr, buddy_serial, "time", self._real_time)
        _FakeTime.now = 0
        self.t, self.stdin = _transport()

    def test_reads_whole_blocks_in_order(self) -> None:
        self.t.bulk_begin(8)
        self.stdin.feed(b"abcdefgh")
        self.assertEqual(self.t.bulk_read(4), b"abcd")
        self.assertEqual(self.t.bulk_read(4), b"efgh")
        self.assertFalse(self.t.bulk_active)

    def test_a_block_split_across_calls_is_not_lost(self) -> None:
        # The host writes at 182 KiB/s and the device polls at 40 ms, so
        # a block genuinely can arrive in pieces. Dropping the first
        # piece would shift every following block by that many bytes.
        self.t.bulk_begin(4)
        self.stdin.feed(b"ab")
        self.assertIsNone(self.t.bulk_read(4))
        self.stdin.feed(b"cd")
        self.assertEqual(self.t.bulk_read(4), b"abcd")

    def test_bytes_the_line_drain_already_took_are_used_first(self) -> None:
        # bulk_begin runs inside the handler for the line that precedes
        # the payload, so the rx buffer can legitimately already hold
        # its first bytes.
        self.t._rx_buf = bytearray(b"ab")
        self.t.bulk_begin(4)
        self.t._rx_buf = bytearray(b"ab")
        self.stdin.feed(b"cd")
        self.assertEqual(self.t.bulk_read(4), b"abcd")

    def test_never_reads_past_the_declared_length(self) -> None:
        # Whatever follows the payload is a command, not a sample.
        self.t.bulk_begin(4)
        self.stdin.feed(b"abcd")
        self.assertEqual(self.t.bulk_read(8), b"abcd")
        self.assertFalse(self.t.bulk_active)

    def test_a_stalled_transfer_gives_the_link_back(self) -> None:
        self.t.bulk_begin(4)
        self.assertIsNone(self.t.bulk_read(4))
        self.assertTrue(self.t.bulk_active)
        _FakeTime.now = 10_000
        self.assertIsNone(self.t.bulk_read(4))
        self.assertFalse(self.t.bulk_active)

    def test_poll_does_not_touch_the_payload(self) -> None:
        # The whole seam: if the line drain kept running it would eat
        # samples and the transfer would end short.
        lines: list[bytes] = []
        self.t._on_line = lines.append
        self.t.bulk_begin(4)
        self.stdin.feed(b"ab\ncd")
        self.t.poll()
        self.assertEqual(lines, [])
        self.assertEqual(self.t.bulk_read(4), b"ab\ncd"[:4])


class _FakeSpeaker:
    """M5.Speaker with a bounded queue, as measured: eight and no more."""

    def __init__(self, depth: int = 8) -> None:
        self.depth = depth
        self.queued: list[bytes] = []
        self.stopped = 0

    def playRaw(
        self,
        data: bytes,
        _rate: int,
        _stereo: bool,
        _repeat: int,
        _channel: int,
        _stop_current: bool,
    ) -> bool:
        if len(self.queued) >= self.depth:
            return False
        self.queued.append(bytes(data))
        return True

    def drain(self, n: int = 1) -> None:
        self.queued = self.queued[n:]

    def stop(self) -> None:
        self.stopped += 1
        self.queued = []


class SpeechPlayerTest(unittest.TestCase):
    def setUp(self) -> None:
        import buddy_serial

        self._real_time = buddy_serial.time
        buddy_serial.time = _FakeTime()
        self.addCleanup(setattr, buddy_serial, "time", self._real_time)
        _FakeTime.now = 0

        self.t, self.stdin = _transport()
        self.sent: list[bytes] = []
        self.t.send_line = self.sent.append
        self.spk = _FakeSpeaker()
        self.player = SpeechPlayer(self.t, speaker=self.spk)

    def begin(self, blocks: int = 3, block: int = BLOCK_BYTES) -> Message:
        ack = self.player.handle(
            {"cmd": "speak.begin", "rate": 16000, "block": block, "blocks": blocks}
        )
        assert ack is not None
        return ack

    def test_begin_declares_the_length_and_arms_the_transport(self) -> None:
        ack = self.begin(blocks=3)
        self.assertTrue(ack["ok"])
        self.assertEqual(ack["bytes"], 3 * BLOCK_BYTES)
        self.assertTrue(self.t.bulk_active)

    def test_one_block_per_pump(self) -> None:
        # Draining in a loop here would freeze the UI for the length of
        # the utterance, so the pump deliberately takes one bite a tick.
        self.begin(blocks=3)
        self.stdin.feed(_blk(b"a") + _blk(b"b") + _blk(b"c"))
        self.player.pump()
        self.assertEqual(self.spk.queued, [_blk(b"a")])
        self.player.pump()
        self.assertEqual(self.spk.queued, [_blk(b"a"), _blk(b"b")])

    def test_finishes_and_acks_when_the_payload_runs_out(self) -> None:
        self.begin(blocks=2)
        self.stdin.feed(_blk(b"a") + _blk(b"b"))
        for _ in range(4):
            self.player.pump()
        self.assertFalse(self.player.active)
        self.assertEqual(len(self.sent), 1)
        self.assertIn(b'"ack":"speak.end"', self.sent[0])
        self.assertIn(b'"ok":true', self.sent[0])

    def test_holds_a_block_the_speaker_refused(self) -> None:
        # playRaw returns False rather than blocking once its queue is
        # full. A block dropped here is a gap in the audio.
        self.spk.depth = 1
        self.begin(blocks=2)
        self.stdin.feed(_blk(b"a") + _blk(b"b"))
        self.player.pump()
        self.player.pump()
        self.assertEqual(self.spk.queued, [_blk(b"a")])
        self.spk.drain()
        self.player.pump()
        self.assertEqual(self.spk.queued, [_blk(b"b")])

    def test_a_stalled_transfer_ends_not_ok(self) -> None:
        self.begin(blocks=2)
        self.player.pump()
        _FakeTime.now = 10_000
        self.player.pump()
        self.assertFalse(self.player.active)
        self.assertIn(b'"ok":false', self.sent[0])

    def test_stop_silences_and_releases(self) -> None:
        self.begin(blocks=2)
        before = self.spk.stopped
        ack = self.player.handle({"cmd": "speak.stop"})
        assert ack is not None
        self.assertTrue(ack["ok"])
        self.assertFalse(self.player.active)
        self.assertFalse(self.t.bulk_active)
        self.assertEqual(self.spk.stopped, before + 1)

    def test_begin_silences_whatever_was_already_playing(self) -> None:
        # The speaker queue holds about a second. Without this, a new
        # utterance would be heard behind the tail of the last one.
        self.assertEqual(self.spk.stopped, 0)
        self.begin(blocks=2)
        self.assertEqual(self.spk.stopped, 1)

    def test_rejects_a_length_that_would_wedge_the_device(self) -> None:
        for bad in (
            {"blocks": 0},
            {"blocks": 10_000_000},
            {"block": 2049},
            {"rate": 100},
        ):
            payload = {"cmd": "speak.begin", "rate": 16000, "block": 2048, "blocks": 4}
            payload.update(bad)
            ack = self.player.handle(payload)
            assert ack is not None
            self.assertFalse(ack["ok"], bad)
            self.assertFalse(self.t.bulk_active, bad)

    def test_other_commands_fall_through(self) -> None:
        self.assertIsNone(self.player.handle({"cmd": "status"}))
        self.assertIsNone(self.player.handle_raw(b"not json"))


class PadTest(unittest.TestCase):
    def test_pads_up_to_a_whole_block(self) -> None:
        self.assertEqual(len(pad_to_blocks(b"x" * 10, block=4)), 12)

    def test_leaves_an_exact_multiple_alone(self) -> None:
        self.assertEqual(pad_to_blocks(b"x" * 8, block=4), b"x" * 8)

    def test_pads_with_silence_not_noise(self) -> None:
        self.assertEqual(pad_to_blocks(b"ab", block=4), b"ab\x00\x00")

    def test_rejects_an_odd_block(self) -> None:
        # A 16-bit sample split across two blocks would be played as two
        # unrelated samples, which is a click.
        with self.assertRaises(ValueError):
            pad_to_blocks(b"ab", block=3)


class _FakeBulkLink:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.requests: list[Message] = []
        self.raw: list[bytes] = []
        self.waited: list[str] = []

    def request(self, obj: Message, expect: str, timeout: float = 5.0) -> Message:
        self.requests.append(obj)
        return {"ack": expect, "ok": self.ok, "err": "nope", "timeout": timeout}

    def write_raw(self, data: bytes) -> None:
        self.raw.append(data)

    def await_ack(self, expect: str, timeout: float = 5.0) -> Message:
        self.waited.append(expect)
        return {"ack": expect, "ok": True, "timeout": timeout}


class SpeakSenderTest(unittest.TestCase):
    def test_declares_then_writes_then_waits(self) -> None:
        link = _FakeBulkLink()
        speak(link, b"\x01\x02" * 3000, rate=16000)
        begin = link.requests[0]
        self.assertEqual(begin["cmd"], "speak.begin")
        self.assertEqual(begin["block"], BLOCK_BYTES)
        self.assertEqual(begin["blocks"] * BLOCK_BYTES, len(link.raw[0]))
        self.assertEqual(link.waited, ["speak.end"])

    def test_payload_is_padded_to_whole_blocks(self) -> None:
        link = _FakeBulkLink()
        speak(link, b"\x01" * (BLOCK_BYTES + 10))
        self.assertEqual(len(link.raw[0]) % BLOCK_BYTES, 0)

    def test_nothing_is_written_when_the_device_refuses(self) -> None:
        # Writing anyway would push a payload at a device that never
        # entered bulk mode, and every byte of it would be parsed as a
        # malformed line.
        link = _FakeBulkLink(ok=False)
        with self.assertRaises(RuntimeError):
            speak(link, b"\x01" * 4096)
        self.assertEqual(link.raw, [])

    def test_empty_audio_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            speak(_FakeBulkLink(), b"")


class SynthesisTest(unittest.TestCase):
    def test_blank_text_is_rejected(self) -> None:
        with self.assertRaises(buddy_speech.SynthesisError):
            buddy_speech.synthesize("   ")

    def test_a_header_only_file_is_reported_as_a_sandbox_problem(self) -> None:
        # This is exactly what `say` does inside the sandbox: exit 0,
        # 4096 bytes, no audio. Passing that on would put silence on the
        # wire and look like a link failure from the device end.
        with (
            mock.patch.object(buddy_speech, "available", return_value=True),
            mock.patch.object(buddy_speech.subprocess, "run") as run,
            mock.patch.object(buddy_speech.Path, "exists", return_value=True),
            mock.patch.object(buddy_speech.Path, "stat") as stat,
        ):
            run.return_value = mock.Mock(returncode=0, stderr="")
            stat.return_value = mock.Mock(st_size=4096)
            with self.assertRaises(buddy_speech.SynthesisError) as caught:
                buddy_speech.synthesize("テスト")
        self.assertIn("uv run", str(caught.exception))

    def test_a_failing_say_is_reported_with_its_stderr(self) -> None:
        with (
            mock.patch.object(buddy_speech, "available", return_value=True),
            mock.patch.object(buddy_speech.subprocess, "run") as run,
        ):
            run.return_value = mock.Mock(returncode=1, stderr="Voice not found")
            with self.assertRaises(buddy_speech.SynthesisError) as caught:
                buddy_speech.synthesize("テスト", voice="Nonexistent")
        self.assertIn("Voice not found", str(caught.exception))

    def test_duration_matches_the_sample_count(self) -> None:
        self.assertAlmostEqual(buddy_speech.duration_s(b"\x00" * 32000, 16000), 1.0)


if __name__ == "__main__":
    unittest.main()
