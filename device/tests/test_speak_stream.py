# pyright: reportPrivateUsage=false
"""ブロックの切り出しと、見限るまで。

音声はもうケーブルからは来ない。デバイスが WiFi 越しに VOICEVOX engine
から取ってくるので、壊れ方が変わった。ホストが長さを宣言して padding 済み
のブロックを書いていた頃と違い、socket は来たときに bytes を寄越し、最後の
ブロックは短く終わり、そのまま止まることもある。

ここで押さえるのは 3 つのうちの 2 つ:

* `playRaw` に渡すブロックがブロック長ちょうどでないこと — 聞こえる
  クリックか、それ以降の全部がずれた音になる;
* 死んだ socket を永久に待つこと。アプリの 40 ms tick が止まり、
  抜けるには interrupt が要る。

残りの 1 つ (途中で切れたストリームを成功として報告する) は、`dead` を
立てるところまでがこちら、それを speak.end に写すところが
`test_speak.py` にある。

`_STALL_MS` などの private を覗く whitebox テストなので、冒頭に
`reportPrivateUsage=false` がある。
"""

import unittest

from buddy import speak_stream
from buddy.speak import _BLOCK
from buddy.speak_stream import StreamSource
from speak_fakes import FakeStream, FakeTime, TimeFrozen, blk


class _FakeRadio:
    """`network.WLAN` のうち pm だけ。読みは位置引数、書きはキーワード。"""

    def __init__(self, pm: int = 1, broken: bool = False) -> None:
        self.pm = pm
        self.broken = broken
        self.writes: list[object] = []

    def config(self, *args: str, **kwargs: object) -> object:
        if self.broken:
            raise OSError("wifi not up")
        if args:
            return self.pm
        self.pm = int(str(kwargs["pm"]))
        self.writes.append(self.pm)
        return None


class RadioTest(TimeFrozen):
    def test_keeps_wifi_awake_while_streaming(self) -> None:
        # 既定の省電力だと socket が 300 ms 止まることがあり (実測: 9.5 秒の
        # 台詞 7 回中 2 回)、枠 2 つぶんのクッションでは覆えない。
        radio = _FakeRadio(pm=1)
        src = StreamSource(FakeStream(), _BLOCK, radio=radio)
        self.assertEqual(radio.pm, speak_stream._PM_AWAKE)
        src.close()
        self.assertEqual(radio.pm, 1)

    def test_restores_only_once(self) -> None:
        radio = _FakeRadio(pm=2)
        src = StreamSource(FakeStream(), _BLOCK, radio=radio)
        src.close()
        src.close()
        self.assertEqual(radio.writes, [speak_stream._PM_AWAKE, 2])

    def test_a_radio_that_cannot_be_configured_does_not_stop_playback(self) -> None:
        radio = _FakeRadio(broken=True)
        src = StreamSource(FakeStream(blk(b"a")), _BLOCK, radio=radio)
        self.assertEqual(src.read_block(_BLOCK), blk(b"a"))
        src.close()
        self.assertEqual(radio.writes, [])

    def test_the_host_has_no_radio(self) -> None:
        # ホストには `network` が無い。触らずに動く。
        src = StreamSource(FakeStream(), _BLOCK)
        self.assertIsNone(src._radio)


class StreamSourceTest(TimeFrozen):
    def test_stops_the_socket_blocking_the_ui(self) -> None:
        # A socket left at its default waits for the bytes it was asked
        # for, and the app's 40 ms tick waits with it.
        stream = FakeStream()
        StreamSource(stream, 4096)
        self.assertEqual(stream.timeout, speak_stream._READ_TIMEOUT_S)

    def test_hands_back_whole_blocks_in_order(self) -> None:
        stream = FakeStream(blk(b"a") + blk(b"b"))
        src = StreamSource(stream, 2 * _BLOCK)
        self.assertEqual(src.read_block(_BLOCK), blk(b"a"))
        self.assertEqual(src.read_block(_BLOCK), blk(b"b"))
        self.assertEqual(src.left, 0)

    def test_a_block_split_across_calls_is_not_lost(self) -> None:
        # WiFi delivers a block in pieces routinely. Dropping the first
        # piece would shift every sample after it.
        stream = FakeStream(b"a" * 100)
        src = StreamSource(stream, _BLOCK)
        self.assertIsNone(src.read_block(_BLOCK))
        stream.feed(b"a" * (_BLOCK - 100))
        self.assertEqual(src.read_block(_BLOCK), blk(b"a"))

    def test_pads_the_final_short_block_with_silence(self) -> None:
        # playRaw is handed exactly one block. The last one almost never
        # divides evenly, and this is the job the host's pad_to_blocks
        # used to do before the audio came off a socket.
        stream = FakeStream(b"x" * 500)
        src = StreamSource(stream, 500)
        block = src.read_block(_BLOCK)
        assert block is not None
        self.assertEqual(len(block), _BLOCK)
        self.assertEqual(block[:500], b"x" * 500)
        self.assertEqual(block[500:], b"\x00" * (_BLOCK - 500))

    def test_the_pad_is_not_counted_as_audio(self) -> None:
        # `left` is what tells the player the utterance is over. Counting
        # the padding would end it a block early on every short tail.
        src = StreamSource(FakeStream(b"x" * 500), 500)
        src.read_block(_BLOCK)
        self.assertEqual(src.left, 0)

    def test_never_reads_past_the_declared_length(self) -> None:
        # Whatever follows the payload on a keep-alive socket belongs to
        # the next response, not to this utterance.
        stream = FakeStream(b"x" * 500 + b"NEXT")
        src = StreamSource(stream, 500)
        src.read_block(_BLOCK)
        self.assertEqual(bytes(stream.buf), b"NEXT")

    def test_waiting_is_not_failing(self) -> None:
        # An empty read is the normal case on a 40 ms tick. Treating it
        # as an error would kill an utterance on the first jitter.
        src = StreamSource(FakeStream(), _BLOCK)
        self.assertIsNone(src.read_block(_BLOCK))
        self.assertFalse(src.dead)

    def test_gives_up_once_nothing_has_arrived_for_the_stall_window(self) -> None:
        src = StreamSource(FakeStream(), _BLOCK)
        FakeTime.now = speak_stream._STALL_MS + 1
        self.assertIsNone(src.read_block(_BLOCK))
        self.assertTrue(src.dead)

    def test_progress_resets_the_stall_deadline(self) -> None:
        # A slow stream that is still alive must not be killed by the
        # clock. Any byte at all counts as progress.
        stream = FakeStream(b"a" * 100)
        src = StreamSource(stream, _BLOCK)
        FakeTime.now = speak_stream._STALL_MS - 1
        self.assertIsNone(src.read_block(_BLOCK))
        FakeTime.now = speak_stream._STALL_MS + 1
        self.assertIsNone(src.read_block(_BLOCK))
        self.assertFalse(src.dead)

    def test_a_stream_that_ends_short_is_a_failure_not_a_short_utterance(self) -> None:
        # Content-Length declared the length up front, so a stream that
        # stops early was cut off. Padding the gap and reporting success
        # would hide it.
        stream = FakeStream(b"x" * 100)
        stream.end()
        src = StreamSource(stream, _BLOCK)
        self.assertIsNone(src.read_block(_BLOCK))
        self.assertTrue(src.dead)

    def test_close_releases_the_socket_and_the_response(self) -> None:
        stream = FakeStream()
        response = FakeStream()
        src = StreamSource(stream, _BLOCK, response)
        src.close()
        self.assertTrue(stream.closed)
        self.assertTrue(response.closed)
        src.close()  # idempotent


if __name__ == "__main__":
    unittest.main()
