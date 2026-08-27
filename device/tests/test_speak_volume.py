# pyright: reportPrivateUsage=false
"""`SpeechPlayer` を組み立てたときに speaker へすること: 起こす、音量を上げる。

再生の並びは `test_speak.py`。こちらは 1 回のブート (= 1 回の生成) で
speaker に掛ける初期化だけを見る。
"""

import unittest

from buddy import speak as buddy_speak
from buddy.speak import SpeechPlayer
from speak_fakes import FakeSpeaker, RecordingTransport, TimeFrozen, unused_fetch


class VolumeTest(TimeFrozen):
    """Turning the speaker up, relative to whatever the firmware set —
    a fixed byte would go stale the moment M5Unified moved its default."""

    def setUp(self) -> None:
        super().setUp()
        self.t = RecordingTransport()

    def build(self, spk: FakeSpeaker) -> SpeechPlayer:
        return SpeechPlayer(self.t, speaker=spk, fetch=unused_fetch)

    def test_the_speaker_is_turned_up_when_the_player_is_built(self) -> None:
        # From a quiet start, so the multiplication is what is being
        # read here rather than the ceiling below.
        spk = FakeSpeaker(volume=10)
        player = self.build(spk)
        self.assertEqual(spk.volume, 10 * buddy_speak._VOLUME_GAIN)
        self.assertEqual(player.volume, spk.volume)

    def test_it_stops_at_the_top_of_the_byte(self) -> None:
        # setVolume takes a byte, and this board boots at 64 (measured), so
        # the shipped gain runs into the ceiling rather than handing the
        # device a value it cannot hold. Raising `_VOLUME_GAIN` changes nothing.
        spk = FakeSpeaker(volume=64)
        self.build(spk)
        self.assertEqual(spk.volume, buddy_speak._MAX_VOLUME)

    def test_a_speaker_without_volume_control_still_plays(self) -> None:
        # Losing the utterance over the setting for it would be the wrong
        # trade, and the binding is not in every firmware build.
        class _NoVolume(FakeSpeaker):
            def getVolume(self) -> int:
                raise AttributeError("getVolume")

        player = self.build(_NoVolume())
        self.assertIsNone(player.volume)


class WakeTest(TimeFrozen):
    """再起動後の最初の playRaw は無音でもポップが鳴る (実測: M5Unified が
    そこで begin() を呼び ES8311 を起こす)。起動時に起こして台詞から離す。"""

    def setUp(self) -> None:
        super().setUp()
        self.t = RecordingTransport()

    def test_the_speaker_is_woken_when_the_player_is_built(self) -> None:
        spk = FakeSpeaker()
        SpeechPlayer(self.t, speaker=spk, fetch=unused_fetch)
        self.assertEqual(spk.begun, 1)

    def test_a_speaker_without_begin_still_plays(self) -> None:
        class _NoBegin(FakeSpeaker):
            def begin(self) -> bool:
                raise AttributeError("begin")

        spk = _NoBegin()
        SpeechPlayer(self.t, speaker=spk, fetch=unused_fetch)
        self.assertEqual(spk.begun, 0)


if __name__ == "__main__":
    unittest.main()
