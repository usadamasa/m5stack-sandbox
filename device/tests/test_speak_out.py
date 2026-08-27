# pyright: reportPrivateUsage=false
"""speaker への出口: 起こす、音量を上げる、渡す、持ち続ける、止める。

`test_speak.py` / `test_speak_volume.py` から切り出した。あちらは
`SpeechPlayer` が発話をどう進めて `speak.end` へ写すかで、こちらは
`SpeakerOut` が `M5.Speaker` に対して何をするか。fake の speaker は
`device/tests/speak_fakes.py` の `FakeSpeaker` を使い回す。

`_CHANNEL` や `_KEEP` といった private を覗く whitebox テストなので、冒頭に
`reportPrivateUsage=false` がある。
"""

import unittest

from buddy import speak_out
from buddy.speak_out import SpeakerOut
from speak_fakes import FakeSpeaker, blk


class WakeTest(unittest.TestCase):
    """再起動後の最初の playRaw は無音でもポップが鳴る (実測: M5Unified が
    そこで begin() を呼び ES8311 を起こす)。起動時に起こして台詞から離す。"""

    def test_the_speaker_is_woken_when_the_out_is_built(self) -> None:
        spk = FakeSpeaker()
        SpeakerOut(spk)
        self.assertEqual(spk.begun, 1)

    def test_a_speaker_without_begin_still_plays(self) -> None:
        class _NoBegin(FakeSpeaker):
            def begin(self) -> bool:
                raise AttributeError("begin")

        spk = _NoBegin()
        SpeakerOut(spk)
        self.assertEqual(spk.begun, 0)


class VolumeTest(unittest.TestCase):
    """Turning the speaker up, relative to whatever the firmware set —
    a fixed byte would go stale the moment M5Unified moved its default."""

    def test_the_speaker_is_turned_up_when_the_out_is_built(self) -> None:
        # From a quiet start, so the multiplication is what is being
        # read here rather than the ceiling below.
        spk = FakeSpeaker(volume=10)
        out = SpeakerOut(spk)
        self.assertEqual(spk.volume, 10 * speak_out._VOLUME_GAIN)
        self.assertEqual(out.volume, spk.volume)

    def test_it_stops_at_the_top_of_the_byte(self) -> None:
        # setVolume takes a byte, and this board boots at 64 (measured), so
        # the shipped gain runs into the ceiling rather than handing the
        # device a value it cannot hold. Raising `_VOLUME_GAIN` changes nothing.
        spk = FakeSpeaker(volume=64)
        SpeakerOut(spk)
        self.assertEqual(spk.volume, speak_out._MAX_VOLUME)

    def test_a_speaker_without_volume_control_still_plays(self) -> None:
        # Losing the utterance over the setting for it would be the wrong
        # trade, and the binding is not in every firmware build.
        class _NoVolume(FakeSpeaker):
            def getVolume(self) -> int:
                raise AttributeError("getVolume")

        out = SpeakerOut(_NoVolume())
        self.assertIsNone(out.volume)


class PushTest(unittest.TestCase):
    """枠の見方とブロックの渡し方。実測の根拠は `buddy/speak_out.py` の
    docstring の Timing 節にある。"""

    def setUp(self) -> None:
        self.spk = FakeSpeaker()
        self.out = SpeakerOut(self.spk)

    def push(self, ch: bytes) -> bool:
        return self.out.push(blk(ch), 16000)

    def test_every_block_goes_to_the_same_fixed_channel(self) -> None:
        # channel=-1 は「空いているチャンネルを探す」で、64 ms のブロックを
        # 45 ms おきに渡すと重なって鳴る (実測: 8 ブロックが 133 ms で終わった)。
        for ch in (b"a", b"b", b"c"):
            self.assertTrue(self.push(ch))
            self.spk.drain()
        self.assertEqual(len(self.spk.channels), 3)
        self.assertEqual(set(self.spk.channels), {speak_out._CHANNEL})
        self.assertGreaterEqual(speak_out._CHANNEL, 0)

    def test_depth_counts_the_slots_in_use(self) -> None:
        # 呼び手はこれを見て渡すかどうかを決める。本物の playRaw は満杯だと
        # 待ち、待たされた tick は UI が止まる。
        self.assertEqual(self.out.depth(), 0)
        self.push(b"a")
        self.assertEqual(self.out.depth(), 1)
        self.push(b"b")
        self.assertEqual(self.out.depth(), speak_out.QUEUE_FULL)

    def test_a_speaker_without_is_playing_reads_as_empty(self) -> None:
        # 分からないなら渡す。満杯なら playRaw が待つだけで、音は落ちない。
        class _NoIsPlaying(FakeSpeaker):
            def isPlaying(self, _channel: int) -> int:
                raise AttributeError("isPlaying")

        self.assertEqual(SpeakerOut(_NoIsPlaying()).depth(), 0)

    def test_a_refused_block_is_not_kept(self) -> None:
        # 呼び手が同じブロックを渡し直すので、こちらが持つ意味は無い。
        self.spk.refuse = True
        self.assertFalse(self.push(b"a"))
        self.assertEqual(self.out._recent, [])

    def test_a_block_that_playraw_threw_on_is_dropped(self) -> None:
        # 断られたことにすると呼び手が延々と渡し直し、キューが詰まったまま
        # になる。1 ブロック落とす方を選ぶ。
        class _Broken(FakeSpeaker):
            def playRaw(
                self,
                data: bytes,
                _rate: int,
                _stereo: bool,
                _repeat: int,
                channel: int,
                _stop_current: bool,
            ) -> bool:
                raise OSError("i2s")

        out = SpeakerOut(_Broken())
        self.assertTrue(out.push(blk(b"a"), 16000))

    def test_keeps_handed_blocks_referenced_until_played(self) -> None:
        # binding は buffer のポインタを渡すだけ (複製しない)。参照を落とすと
        # GC がその領域を次の bytes に回し、鳴っている途中で中身が変わる。
        for ch in (b"a", b"b", b"c", b"d"):
            self.push(ch)
            self.spk.drain()
        kept = self.out._recent
        self.assertTrue(kept)
        self.assertLessEqual(len(kept), speak_out._KEEP)
        for handed, held in zip(self.spk.handed[-len(kept) :], kept, strict=True):
            self.assertIs(handed, held)

    def test_stop_silences_but_holds_on_to_the_blocks(self) -> None:
        # `spk.stop()` の直後も task は数 ms 読む。手放すのは release() だけ。
        self.push(b"a")
        self.out.stop()
        self.assertEqual(self.spk.stopped, 1)
        self.assertTrue(self.out._recent)

    def test_release_lets_the_blocks_go(self) -> None:
        self.push(b"a")
        self.out.release()
        self.assertEqual(self.out._recent, [])

    def test_a_speaker_that_cannot_stop_does_not_raise(self) -> None:
        class _NoStop(FakeSpeaker):
            def stop(self) -> None:
                raise AttributeError("stop")

        SpeakerOut(_NoStop()).stop()


class BlockSizeTest(unittest.TestCase):
    def test_block_size_follows_the_rate(self) -> None:
        # 枠は 2 つで tick に 1〜2 ブロックしか渡せない。tick (40 ms + 読み
        # 取り) より短いブロックだと再生が追い越す。80 ms 以上の最小の 2 の冪。
        self.assertEqual(speak_out.block_for(16000), 4096)
        self.assertEqual(speak_out.block_for(24000), 4096)
        self.assertEqual(speak_out.block_for(48000), 8192)

    def test_the_default_block_is_the_one_tests_count_in(self) -> None:
        self.assertEqual(speak_out.BLOCK, speak_out.block_for(24000))


if __name__ == "__main__":
    unittest.main()
