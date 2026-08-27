# pyright: reportPrivateUsage=false
"""The speech path: playback sequencing and the sender.

The audio no longer arrives over the cable. The device fetches it from a
VOICEVOX engine over WiFi, which changes what can go wrong: the host
used to declare a length and write padded whole blocks, and now a socket
delivers bytes when it feels like it, ends the last block short, and can
stop altogether.

ブロックを切り出す側の失敗は `test_speak_stream.py`、speaker へ渡す側は
`test_speak_out.py` にある。こちらは `SpeechPlayer` がそれをどう鳴らし、どう
speak.end へ写すか — 枠が空いたぶんだけ渡すこと、speaker が断ったブロックを
落とさないこと、途中で切れたストリームを成功として報告しないこと。ホスト側
から見た `buddy_verbs.speak` の契約は `test_speak_host.py`。

All of it runs without a board, a speaker or an engine.

This is also a whitebox test of `SpeechPlayer`'s private internals
(`_stalls`, `_block`, etc.), hence the file-level
`reportPrivateUsage=false` above.
"""

import json
import unittest
from typing import TYPE_CHECKING, Any

from buddy import speak_stream
from buddy.speak import SpeechPlayer
from buddy.speak_out import BLOCK
from speak_fakes import (
    FakeResponse,
    FakeSpeaker,
    FakeStream,
    FakeTime,
    RecordingTransport,
    TimeFrozen,
    blk,
)

if TYPE_CHECKING:
    # 型検査だけ。`device/typings/` の stub-only モジュールで、実体は無い。
    from buddy_types import SpeechSource


class SpeechPlayerTest(TimeFrozen):
    def setUp(self) -> None:
        super().setUp()
        self.t = RecordingTransport()
        self.spk = FakeSpeaker()
        self.stream = FakeStream()
        self.response = FakeResponse()
        self.fetched: list[tuple[str, str, int, int]] = []
        self.player = SpeechPlayer(self.t, speaker=self.spk, fetch=self._fetch)
        self._rate = 16000
        self._bytes = 2 * BLOCK
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
        self.assertEqual(ack["bytes"], 2 * BLOCK)
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

    def test_the_speaker_is_turned_up_when_the_player_is_built(self) -> None:
        # 上げるのは `SpeakerOut` の仕事 (`test_speak_out.py`)。ここで見るのは
        # その結果が player の `volume` に写ること — ホストが読む値。
        self.assertEqual(self.player.volume, self.spk.volume)

    def test_fills_the_free_slots_and_no_more_in_one_tick(self) -> None:
        # 枠は 2 つ。最初の tick で両方埋める — 頭のクッションはこれしか
        # 無い。3 つ目は枠が空くまで socket に置いたままにする。
        self._bytes = 3 * BLOCK
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
        # engine が declined したレートで鳴らすので、ブロック長も engine の
        # 言い値から引く。長さの決め方そのものは `test_speak_out.py`。
        self._rate = 48000
        self.say()
        self.assertEqual(self.player._block, 8192)

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

    def test_only_text_and_url_is_enough(self) -> None:
        ack = self.player.handle({"cmd": "speak.say", "text": "あ", "url": "http://h:50021"})
        assert ack is not None
        self.assertTrue(ack["ok"])
        # Defaults matter: the host may send only text and url.
        self.assertEqual(self.fetched[0][2], 3)


if __name__ == "__main__":
    unittest.main()
