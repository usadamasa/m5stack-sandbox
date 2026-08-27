# pyright: reportPrivateUsage=false
"""The speech path: playback sequencing, volume, and the sender.

The audio no longer arrives over the cable. The device fetches it from a
VOICEVOX engine over WiFi, which changes what can go wrong: the host
used to declare a length and write padded whole blocks, and now a socket
delivers bytes when it feels like it, ends the last block short, and can
stop altogether.

ブロックを切り出す側の失敗は `test_speak_stream.py` にある。こちらは
`SpeechPlayer` がそれをどう鳴らし、どう speak.end へ写すか — 1 tick に
1 ブロックであること、speaker が断ったブロックを落とさないこと、途中で
切れたストリームを成功として報告しないこと。ホスト側から見た
`buddy_verbs.speak` の契約も、同じ経路の両端なのでここにある。

All of it runs without a board, a speaker or an engine.

This is also a whitebox test of `SpeechPlayer`'s private internals
(`_BLOCK`, `_VOLUME_GAIN`, etc.), hence the file-level
`reportPrivateUsage=false` above.
"""

import json
import os
import unittest
from typing import TYPE_CHECKING, Any
from unittest import mock

import buddy_verbs
from buddy import speak as buddy_speak
from buddy import speak_stream
from buddy.speak import _BLOCK, SpeechPlayer
from speak_fakes import FakeResponse, FakeStream, FakeTime, TimeFrozen, blk, unused_fetch

if TYPE_CHECKING:
    # 型検査だけ。`device/typings/` の stub-only モジュールで、実体は無い。
    from buddy_types import SpeechSource


class _FakeSpeaker:
    """M5.Speaker のうち player が触る面。実測に合わせてある。

    1 チャンネルの枠は 2 つ (再生中 + 次)。`isPlaying(ch)` はその埋まり具合を
    0 / 1 / 2 で返す。本物の `playRaw` は満杯だと**待つ** (False は返さない)
    ので、fake は満杯で呼ばれたら `overfilled` を立てて False を返す —
    player がそこへ渡した時点で、実機なら UI が止まっている。

    `channel=-1` は「空いているチャンネルを探す」で、ブロックが並列に鳴る。
    fake は渡されたチャンネルを全部記録して、テストがそれを検査する。
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
        # channel=-1 は「空いているチャンネルを探す」。64 ms のブロックを
        # 45 ms おきに渡すと別チャンネルで重なって鳴る (実測: 8 ブロックが
        # 133 ms で終わった)。1 本に固定して初めて順に鳴る。
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
        # binding は buffer のポインタをそのまま渡す (複製しない)。参照を
        # 落とすと GC がその領域を次の bytes に回し、鳴っている途中で中身が
        # 変わる。渡した最後の数ブロックは player が持ち続ける。
        self.say()
        self.stream.feed(blk(b"a") + blk(b"b") + blk(b"c") + blk(b"d"))
        for _ in range(4):
            self.player.pump()
            self.spk.drain()
        kept = self.player._recent
        self.assertTrue(kept)
        self.assertLessEqual(len(kept), buddy_speak._KEEP)
        for handed, held in zip(self.spk.handed[-len(kept) :], kept):
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
        # stalls はホストが「音が途切れた」を知る唯一の値。socket を待った
        # tick ではなく、鳴り始めた後に speaker が空になった回数で数える —
        # 続けて空いていた tick は 1 回。
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
        # 1 tick (40 ms + 読み取り) に 1〜2 ブロックしか渡せず、枠は 2 つ。
        # ブロックが tick より短いと再生が追い越す。64 ms 以上になる最小の
        # 2 の冪。
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
    """Turning the speaker up, relative to whatever the firmware set.

    A fixed byte would go stale the moment M5Unified moved its default,
    and the thing actually asked for was "twice as loud".
    """

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
        # setVolume takes a byte, and this board boots at 64 — measured
        # — so the shipped gain runs into the ceiling rather than
        # handing the device a value it cannot hold. Worth knowing: at
        # this gain the speaker is already at its loudest, and raising
        # `_VOLUME_GAIN` further changes nothing.
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


class _FakeLink:
    """A link that records requests and answers with canned acks."""

    def __init__(
        self, ack: dict[str, Any] | None = None, end: dict[str, Any] | None = None
    ) -> None:
        self.ack: dict[str, Any] = (
            ack if ack is not None else {"ok": True, "bytes": 81920, "rate": 16000}
        )
        self.end: dict[str, Any] = (
            end if end is not None else {"ok": True, "blocks": 40, "stalls": 0}
        )
        self.requests: list[tuple[dict[str, Any], str, float]] = []
        self.waited: list[tuple[str, float]] = []

    def request(self, obj: dict[str, Any], expect: str, timeout: float = 5.0) -> dict[str, Any]:
        self.requests.append((obj, expect, timeout))
        out = dict(self.ack)
        out["ack"] = expect
        return out

    def await_ack(self, expect: str, timeout: float = 5.0) -> dict[str, Any]:
        self.waited.append((expect, timeout))
        out = dict(self.end)
        out["ack"] = expect
        return out


class SpeakSenderTest(unittest.TestCase):
    def test_asks_the_device_to_fetch_and_then_waits_for_the_end(self) -> None:
        link = _FakeLink()
        end = buddy_verbs.speak(link, "ずんだもんなのだ", url="http://h:50021")
        sent, expect, _timeout = link.requests[0]
        self.assertEqual(sent["cmd"], "speak.say")
        self.assertEqual(sent["text"], "ずんだもんなのだ")
        self.assertEqual(sent["url"], "http://h:50021")
        self.assertEqual(expect, "speak.say")
        self.assertEqual([w[0] for w in link.waited], ["speak.end"])
        self.assertEqual(end["blocks"], 40)

    def test_defaults_to_zundamon(self) -> None:
        link = _FakeLink()
        buddy_verbs.speak(link, "あ", url="http://h:50021")
        self.assertEqual(link.requests[0][0]["speaker"], buddy_verbs.ZUNDAMON)

    def test_allows_time_for_synthesis_before_the_first_ack(self) -> None:
        # The device does not answer speak.say until the engine has
        # produced the whole WAV. A chat-sized timeout would give up
        # while synthesis was still running and leave the link out of
        # step with a device that is about to start playing.
        link = _FakeLink()
        buddy_verbs.speak(link, "あ", url="http://h:50021")
        self.assertGreaterEqual(link.requests[0][2], 30.0)

    def test_waits_out_the_playback_before_giving_up_on_the_end_ack(self) -> None:
        # speak.end arrives when the last block has been played, which
        # is 5.12 s after the start for this payload.
        link = _FakeLink(ack={"ok": True, "bytes": 163840, "rate": 16000})
        buddy_verbs.speak(link, "あ", url="http://h:50021", timeout=10.0)
        self.assertGreaterEqual(link.waited[0][1], 5.12 + 10.0)

    def test_a_refusal_is_raised_not_returned(self) -> None:
        # Waiting for speak.end after a refusal would block until the
        # timeout for an utterance that never started.
        link = _FakeLink(ack={"ok": False, "err": "no engine url"})
        with self.assertRaises(RuntimeError) as caught:
            buddy_verbs.speak(link, "あ", url="http://h:50021")
        self.assertIn("no engine url", str(caught.exception))
        self.assertEqual(link.waited, [])

    def test_empty_text_never_reaches_the_device(self) -> None:
        link = _FakeLink()
        with self.assertRaises(ValueError):
            buddy_verbs.speak(link, "   ", url="http://h:50021")
        self.assertEqual(link.requests, [])


class VoicevoxUrlTest(unittest.TestCase):
    def test_an_explicit_url_wins(self) -> None:
        self.assertEqual(buddy_verbs.voicevox_url("http://10.0.0.5:50021"), "http://10.0.0.5:50021")

    def test_falls_back_to_the_environment(self) -> None:
        with mock.patch.dict(os.environ, {"VOICEVOX_URL": "http://env:50021"}):
            self.assertEqual(buddy_verbs.voicevox_url(), "http://env:50021")

    def test_a_bare_address_is_given_a_scheme_and_a_port(self) -> None:
        # The device does no URL parsing; it concatenates paths onto
        # whatever it is handed. A bare host would produce
        # "192.168.0.156/audio_query" and fail at the socket.
        self.assertEqual(buddy_verbs.voicevox_url("192.168.0.156"), "http://192.168.0.156:50021")

    def test_a_trailing_slash_is_dropped(self) -> None:
        self.assertEqual(buddy_verbs.voicevox_url("http://h:50021/"), "http://h:50021")

    def test_localhost_is_refused(self) -> None:
        # The engine runs on this Mac, but the device is not on this
        # Mac. A loopback address is the single most likely mistake
        # here and it fails as a connection timeout on the device,
        # seconds later and nowhere near the cause.
        for bad in ("http://127.0.0.1:50021", "http://localhost:50021"):
            with self.assertRaises(ValueError, msg=bad):
                buddy_verbs.voicevox_url(bad)


if __name__ == "__main__":
    unittest.main()
