# pyright: reportPrivateUsage=false
"""The speech path: playback sequencing, volume, and the sender.

The audio no longer arrives over the cable. The device fetches it from a
VOICEVOX engine over WiFi, which changes what can go wrong: the host
used to declare a length and write padded whole blocks, and now a socket
delivers bytes when it feels like it, ends the last block short, and can
stop altogether.

ブロックを切り出す側の失敗は `test_speak_stream.py` にある。こちらは
`SpeechPlayer` がそれをどう鳴らし、どう speak.end へ写すか — 1 本の
チャンネルに枠が空いたぶんだけ渡すこと、渡したブロックを手放さないこと、
speaker が断ったブロックを落とさないこと、途中で切れたストリームを成功
として報告しないこと。ホスト側から見た `buddy_verbs.speak` の契約は
`test_speak_host.py`。

All of it runs without a board, a speaker or an engine.

This is also a whitebox test of `SpeechPlayer`'s private internals
(`_BLOCK`, `_VOLUME_GAIN`, etc.), hence the file-level
`reportPrivateUsage=false` above.
"""

import json
import unittest
from typing import TYPE_CHECKING, Any

from buddy import speak as buddy_speak
from buddy import speak_stream
from buddy.speak import _BLOCK, SpeechPlayer
from speak_fakes import FakeResponse, FakeStream, FakeTime, TimeFrozen, blk, unused_fetch

if TYPE_CHECKING:
    # 型検査だけ。`device/typings/` の stub-only モジュールで、実体は無い。
    from buddy_types import SpeechSource


class _FakeSpeaker:
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
        # M5Unified's own default, which is what the player multiplies.
        self.volume = volume

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


class _RecordingTransport:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def send_line(self, payload: bytes) -> bool:
        self.sent.append(payload)
        return True


class SpeechPlayerTest(TimeFrozen):
    def setUp(self) -> None:
        super().setUp()
        self.t = _RecordingTransport()
        self.spk = _FakeSpeaker()
        self.stream = FakeStream()
        self.response = FakeResponse()
        self.fetched: list[tuple[str, str, int, int]] = []
        self.player = SpeechPlayer(self.t, speaker=self.spk, fetch=self._fetch)
        self._rate = 16000
        self._bytes = 2 * _BLOCK
        self._error: Exception | None = None

    def _fetch(self, url: str, text: str, speaker: int, rate: int) -> "SpeechSource":
        self.fetched.append((url, text, speaker, rate))
        if self._error is not None:
            raise self._error
        return {
            "stream": self.stream,
            "bytes": self._bytes,
            "rate": self._rate,
            "response": self.response,
        }

    def say(self, **over: object) -> dict[str, Any]:
        msg: dict[str, object] = {
            "cmd": "speak.say",
            "text": "ずんだもんなのだ",
            "url": "http://192.168.0.156:50021",
            "speaker": 3,
            "rate": 16000,
        }
        msg.update(over)
        ack = self.player.handle(msg)
        assert ack is not None
        return ack

    def test_say_fetches_and_starts_playing(self) -> None:
        ack = self.say()
        self.assertTrue(ack["ok"])
        self.assertEqual(ack["bytes"], 2 * _BLOCK)
        self.assertTrue(self.player.active)
        self.assertEqual(
            self.fetched, [("http://192.168.0.156:50021", "ずんだもんなのだ", 3, 16000)]
        )

    def test_reports_the_rate_the_engine_used(self) -> None:
        # The engine may decline outputSamplingRate, and playRaw has to
        # be told the truth or the whole line comes out at the wrong
        # pitch.
        self._rate = 24000
        self.assertEqual(self.say()["rate"], 24000)

    def test_every_block_goes_to_the_same_fixed_channel(self) -> None:
        # channel=-1 は「空いているチャンネルを探す」で、64 ms のブロックを
        # 45 ms おきに渡すと重なって鳴る (実測: 8 ブロックが 133 ms で終わった)。
        self._bytes = 3 * _BLOCK
        self.say()
        self.stream.feed(blk(b"a") + blk(b"b") + blk(b"c"))
        for _ in range(3):
            self.player.pump()
            self.spk.drain()
        self.assertEqual(len(self.spk.channels), 3)
        self.assertEqual(set(self.spk.channels), {buddy_speak._CHANNEL})
        self.assertGreaterEqual(buddy_speak._CHANNEL, 0)

    def test_fills_the_free_slots_and_no_more_in_one_tick(self) -> None:
        # 枠は 2 つ。最初の tick で両方埋める — 頭のクッションはこれしか
        # 無い。3 つ目は枠が空くまで socket に置いたままにする。
        self._bytes = 3 * _BLOCK
        self.say()
        self.stream.feed(blk(b"a") + blk(b"b") + blk(b"c"))
        self.player.pump()
        self.assertEqual(self.spk.queued, [blk(b"a"), blk(b"b")])
        self.player.pump()
        self.assertEqual(self.spk.queued, [blk(b"a"), blk(b"b")])
        self.assertEqual(self.spk.overfilled, 0)
        self.spk.drain()
        self.player.pump()
        self.assertEqual(self.spk.queued, [blk(b"b"), blk(b"c")])

    def test_never_hands_a_block_to_a_full_queue(self) -> None:
        # 本物の playRaw は満杯だと待つ。待たされた tick は UI が止まる。
        self.say()
        self.stream.feed(blk(b"a") * 6)
        for _ in range(6):
            self.player.pump()
        self.assertEqual(self.spk.overfilled, 0)
        self.assertEqual(len(self.spk.handed), 2)

    def test_keeps_handed_blocks_referenced_until_played(self) -> None:
        # binding は buffer のポインタを渡すだけ (複製しない)。参照を落とすと
        # GC がその領域を次の bytes に回し、鳴っている途中で中身が変わる。
        self._bytes = 4 * _BLOCK
        self.say()
        self.stream.feed(blk(b"a") + blk(b"b") + blk(b"c") + blk(b"d"))
        for _ in range(4):
            self.player.pump()
            self.spk.drain()
        kept = self.player._recent
        self.assertTrue(kept)
        self.assertLessEqual(len(kept), buddy_speak._KEEP)
        for handed, held in zip(self.spk.handed[-len(kept) :], kept, strict=True):
            self.assertIs(handed, held)

    def test_finishes_and_acks_when_the_payload_runs_out(self) -> None:
        self.say()
        self.stream.feed(blk(b"a") + blk(b"b"))
        for _ in range(4):
            self.player.pump()
            self.spk.drain()
        self.assertFalse(self.player.active)
        self.assertEqual(len(self.t.sent), 1)
        sent = json.loads(self.t.sent[0])
        self.assertEqual(sent["ack"], "speak.end")
        self.assertTrue(sent["ok"])
        self.assertEqual(sent["blocks"], 2)

    def test_speak_end_waits_for_the_speaker_to_drain(self) -> None:
        # 最後のブロックを渡した時点ではまだ 2 枠ぶん鳴り残っている。
        # ホストは speak.end を「鳴り終わった」として扱うので、そこまで待つ。
        self.say()
        self.stream.feed(blk(b"a") + blk(b"b"))
        self.player.pump()
        self.player.pump()
        self.assertTrue(self.player.active)
        self.assertEqual(self.t.sent, [])
        self.spk.drain(2)
        self.player.pump()
        self.assertFalse(self.player.active)
        self.assertEqual(json.loads(self.t.sent[0])["ack"], "speak.end")

    def test_holds_a_block_the_speaker_refused(self) -> None:
        # 断られたブロックは次の tick でもう一度渡す。落とすと音が抜ける。
        self.say()
        self.stream.feed(blk(b"a") + blk(b"b"))
        self.spk.refuse = True
        self.player.pump()
        self.assertEqual(self.spk.queued, [])
        self.spk.refuse = False
        self.player.pump()
        self.assertEqual(self.spk.queued, [blk(b"a"), blk(b"b")])

    def test_a_stalled_stream_ends_not_ok(self) -> None:
        self.say()
        self.player.pump()
        FakeTime.now = speak_stream._STALL_MS + 1
        self.player.pump()
        self.assertFalse(self.player.active)
        sent = json.loads(self.t.sent[0])
        self.assertFalse(sent["ok"])

    def test_a_stalled_stream_still_plays_out_what_it_has(self) -> None:
        # 途中で切れても、渡し終えたぶんは鳴らし切ってから not-ok を返す。
        self.say()
        self.stream.feed(blk(b"a"))
        self.player.pump()
        FakeTime.now = speak_stream._STALL_MS + 1
        self.player.pump()
        self.assertTrue(self.player.active)
        self.spk.drain()
        self.player.pump()
        self.assertFalse(self.player.active)
        self.assertFalse(json.loads(self.t.sent[0])["ok"])

    def test_stalls_count_the_times_the_speaker_ran_dry(self) -> None:
        # stalls はホストが「音が途切れた」を知る唯一の値。鳴り始めた後に
        # speaker が空になった回数で、続けて空いていた tick は 1 回。
        self.say()
        self.stream.feed(blk(b"a"))
        self.player.pump()
        self.assertEqual(self.player._stalls, 0)
        self.spk.drain()
        for _ in range(3):
            self.player.pump()
        self.assertEqual(self.player._stalls, 1)
        self.stream.feed(blk(b"b"))
        self.player.pump()
        self.spk.drain()
        self.player.pump()
        self.assertFalse(self.player.active)
        self.assertEqual(json.loads(self.t.sent[0])["stalls"], 1)

    def test_waiting_before_the_first_block_is_not_a_stall(self) -> None:
        # 鳴り始める前に socket を待つのは普通のこと。音は途切れていない。
        self.say()
        self.player.pump()
        self.player.pump()
        self.stream.feed(blk(b"a") + blk(b"b"))
        for _ in range(4):
            self.player.pump()
            self.spk.drain()
        self.assertEqual(json.loads(self.t.sent[0])["stalls"], 0)

    def test_block_size_follows_the_rate(self) -> None:
        # 枠は 2 つで tick に 1〜2 ブロックしか渡せない。tick (40 ms + 読み
        # 取り) より短いブロックだと再生が追い越す。64 ms 以上の最小の 2 の冪。
        self.assertEqual(buddy_speak._block_for(16000), 2048)
        self.assertEqual(buddy_speak._block_for(24000), 4096)
        self.assertEqual(buddy_speak._block_for(48000), 8192)
        self._rate = 24000
        self.say()
        self.assertEqual(self.player._block, 4096)

    def test_a_failed_fetch_is_answered_not_raised(self) -> None:
        # The app's on_line calls this synchronously. An exception here
        # would take the transport down with it.
        self._error = OSError("ECONNREFUSED")
        ack = self.say()
        self.assertFalse(ack["ok"])
        self.assertIn("ECONNREFUSED", ack["err"])
        self.assertFalse(self.player.active)

    def test_refuses_to_start_without_an_engine_url(self) -> None:
        ack = self.say(url="")
        self.assertFalse(ack["ok"])
        self.assertEqual(self.fetched, [])

    def test_rejects_a_length_that_would_wedge_the_device(self) -> None:
        for bad in (0, 99_000_000):
            self._bytes = bad
            ack = self.say()
            self.assertFalse(ack["ok"], bad)
            self.assertFalse(self.player.active, bad)

    def test_stop_silences_and_releases(self) -> None:
        self.say()
        before = self.spk.stopped
        ack = self.player.handle({"cmd": "speak.stop"})
        assert ack is not None
        self.assertTrue(ack["ok"])
        self.assertFalse(self.player.active)
        self.assertEqual(self.spk.stopped, before + 1)
        self.assertTrue(self.stream.closed)

    def test_say_silences_whatever_was_already_playing(self) -> None:
        # 枠 2 つぶんの尻尾が残る。止めないと、合成の数秒の後に前の台詞の
        # 尻尾が鳴ってから次が始まる。
        self.assertEqual(self.spk.stopped, 0)
        self.say()
        self.assertEqual(self.spk.stopped, 1)

    def test_other_commands_fall_through(self) -> None:
        self.assertIsNone(self.player.handle({"cmd": "status"}))
        self.assertIsNone(self.player.handle_raw(b"not json"))

    def test_handles_a_raw_wire_line(self) -> None:
        ack = self.player.handle_raw(
            json.dumps({"cmd": "speak.say", "text": "あ", "url": "http://h:50021"}).encode("utf-8")
        )
        assert ack is not None
        self.assertTrue(ack["ok"])
        # Defaults matter: the host may send only text and url.
        self.assertEqual(self.fetched[0][2], 3)


class VolumeTest(TimeFrozen):
    """Turning the speaker up, relative to whatever the firmware set —
    a fixed byte would go stale the moment M5Unified moved its default."""

    def setUp(self) -> None:
        super().setUp()
        self.t = _RecordingTransport()

    def build(self, spk: _FakeSpeaker) -> SpeechPlayer:
        return SpeechPlayer(self.t, speaker=spk, fetch=unused_fetch)

    def test_the_speaker_is_turned_up_when_the_player_is_built(self) -> None:
        # From a quiet start, so the multiplication is what is being
        # read here rather than the ceiling below.
        spk = _FakeSpeaker(volume=10)
        player = self.build(spk)
        self.assertEqual(spk.volume, 10 * buddy_speak._VOLUME_GAIN)
        self.assertEqual(player.volume, spk.volume)

    def test_it_stops_at_the_top_of_the_byte(self) -> None:
        # setVolume takes a byte, and this board boots at 64 (measured), so
        # the shipped gain runs into the ceiling rather than handing the
        # device a value it cannot hold. Raising `_VOLUME_GAIN` changes nothing.
        spk = _FakeSpeaker(volume=64)
        self.build(spk)
        self.assertEqual(spk.volume, buddy_speak._MAX_VOLUME)

    def test_a_speaker_without_volume_control_still_plays(self) -> None:
        # Losing the utterance over the setting for it would be the wrong
        # trade, and the binding is not in every firmware build.
        class _NoVolume(_FakeSpeaker):
            def getVolume(self) -> int:
                raise AttributeError("getVolume")

        player = self.build(_NoVolume())
        self.assertIsNone(player.volume)


if __name__ == "__main__":
    unittest.main()
