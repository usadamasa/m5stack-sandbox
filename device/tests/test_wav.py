"""WAV framing: where the samples start, and how they get handed on.

Nothing here needs a board, a speaker or a running engine. What it
protects is the seam where a stream of bytes off a socket becomes
something `M5.Speaker.playRaw` will accept, and every failure in that
seam sounds the same from the far end of a USB cable: silence, or a
device parked inside a read.

The header numbers are not invented. `tmp/voicevox_probe.py` measured
VOICEVOX 0.25.2 answering on this LAN: `audio/wav`, a 44-byte header of
`fmt ` then `data` with nothing in between, 1ch/16-bit, and 81920 bytes
of PCM for a 2.56 s utterance at `outputSamplingRate=16000`.
"""

import struct
import unittest

from buddy.wav import PrefixedStream, WavError, open_pcm, parse_wav_header, read_exactly
from wav_fakes import FakeRaw, chunk, fmt, wav_head


class WavHeaderTest(unittest.TestCase):
    def test_reads_what_voicevox_actually_sends(self) -> None:
        # The measured case: fmt then data, PCM starting at 44.
        head = wav_head(81920)
        self.assertEqual(len(head), 44)
        got = parse_wav_header(head)
        self.assertEqual(got["offset"], 44)
        self.assertEqual(got["bytes"], 81920)
        self.assertEqual(got["rate"], 16000)
        self.assertEqual(got["channels"], 1)
        self.assertEqual(got["bits"], 16)

    def test_takes_the_rate_from_the_file_not_from_what_we_asked_for(self) -> None:
        # `outputSamplingRate` is a request, not a guarantee. Playing
        # 24 kHz samples at 16 kHz is not a subtle failure — it is the
        # wrong pitch for the whole utterance — so the header wins.
        self.assertEqual(parse_wav_header(wav_head(4096, rate=24000))["rate"], 24000)

    def test_finds_data_behind_a_chunk_we_do_not_care_about(self) -> None:
        # VOICEVOX 0.25.2 emits none, but a fixed 44-byte skip would be
        # betting the whole audio path on that staying true.
        head = wav_head(2048, before_data=chunk(b"LIST", b"INFOsoftware"))
        got = parse_wav_header(head)
        self.assertEqual(got["bytes"], 2048)
        self.assertEqual(head[got["offset"] - 8 : got["offset"] - 4], b"data")

    def test_skips_the_pad_byte_after_an_odd_chunk(self) -> None:
        # RIFF pads odd-sized chunks to even boundaries and does not
        # count the pad in the size field. Miss it and every following
        # chunk id is read one byte late, which finds nothing.
        head = wav_head(2048, before_data=chunk(b"LIST", b"odd"))
        self.assertEqual(parse_wav_header(head)["bytes"], 2048)

    def test_rejects_something_that_is_not_a_wav(self) -> None:
        # An engine that answers an error as JSON, or a captive portal
        # answering HTML, both arrive here as a stream of bytes.
        with self.assertRaises(WavError):
            parse_wav_header(b'{"detail":"speaker not found"}')

    def test_rejects_a_truncated_head(self) -> None:
        with self.assertRaises(WavError):
            parse_wav_header(b"RIFF")

    def test_rejects_data_that_arrives_before_fmt(self) -> None:
        # Without fmt there is no sample rate, and guessing one is the
        # wrong-pitch failure again.
        head = b"RIFF" + struct.pack("<I", 4) + b"WAVE" + b"data" + struct.pack("<I", 2048)
        with self.assertRaises(WavError):
            parse_wav_header(head)

    def test_rejects_a_head_with_no_data_chunk_in_it(self) -> None:
        # The caller reads a bounded prefix; a data chunk past the end
        # of it has to be an error rather than a silent zero-length read.
        head = b"RIFF" + struct.pack("<I", 4) + b"WAVE" + chunk(b"fmt ", fmt())
        with self.assertRaises(WavError):
            parse_wav_header(head)

    def test_rejects_a_format_the_speaker_cannot_play(self) -> None:
        # playRaw takes signed 16-bit mono. Handing it stereo or 8-bit
        # would play as noise at double speed rather than fail.
        for channels, bits in ((2, 16), (1, 8)):
            with self.assertRaises(WavError, msg=f"{channels}ch/{bits}-bit"):
                parse_wav_header(wav_head(2048, channels=channels, bits=bits))


class ReadExactlyTest(unittest.TestCase):
    def test_keeps_reading_until_it_has_what_was_asked_for(self) -> None:
        # socket は欲しいぶんをその場ではくれない。1 回の read で足りた
        # ことにすると、ヘッダの途中で解こうとすることになる。
        stream = FakeRaw(b"0123456789")
        self.assertEqual(read_exactly(stream, 10), b"0123456789")

    def test_comes_back_short_at_the_end_of_the_stream(self) -> None:
        # 打ち切られた応答は「まだ来ていない」と見分けが付かないので、
        # ここでは待たずに戻し、足りるかどうかは解く側が言う。
        self.assertEqual(read_exactly(FakeRaw(b"RIFF"), 512), b"RIFF")


class PrefixedStreamTest(unittest.TestCase):
    def test_hands_back_the_held_bytes_before_touching_the_socket(self) -> None:
        # 読み過ぎたぶんは台詞の頭の音そのもの。捨てるとクリックになる。
        rest = FakeRaw(b"tail")
        stream = PrefixedStream(b"head", rest)
        self.assertEqual(stream.read(4), b"head")
        self.assertEqual(rest.pos, 0)

    def test_crosses_from_the_held_bytes_into_the_socket(self) -> None:
        # 継ぎ目を落とした読み手は、1 回の発話につきちょうど 1 度だけ
        # 音を落とすか繰り返す。
        stream = PrefixedStream(b"head", FakeRaw(b"tail"))
        got = b""
        while len(got) < 8:
            piece = stream.read(3)
            if not piece:
                break
            got += piece
        self.assertEqual(got, b"headtail")

    def test_forwards_the_timeout_to_the_socket(self) -> None:
        # `StreamSource` はこれを渡された stream に設定する。転送しないと
        # ここで消えて、prefix を読み切った次の read が UI を止める。
        rest = FakeRaw(b"")
        PrefixedStream(b"", rest).settimeout(0.02)
        self.assertEqual(rest.timeout, 0.02)

    def test_closing_reaches_the_socket(self) -> None:
        rest = FakeRaw(b"")
        PrefixedStream(b"", rest).close()
        self.assertTrue(rest.closed)

    def test_a_socket_that_throws_on_close_does_not_take_the_app_with_it(self) -> None:
        # 閉じ損ねてもできることは無い。ここで送出すると、再生を畳む側が
        # 巻き添えになる。
        class Angry(FakeRaw):
            def close(self) -> None:
                raise OSError("already gone")

        PrefixedStream(b"", Angry(b"")).close()


class OpenPcmTest(unittest.TestCase):
    def test_reports_the_header_and_a_stream_at_the_first_sample(self) -> None:
        raw = FakeRaw(wav_head(64) + b"\x00" * 64)
        info, pcm = open_pcm(raw)
        self.assertEqual(info["bytes"], 64)
        self.assertEqual(info["rate"], 16000)
        self.assertEqual(pcm.read(4), b"\x00\x00\x00\x00")

    def test_the_stream_it_returns_crosses_the_over_read_seam(self) -> None:
        # ヘッダの探索は読み過ぎる。最初の samples は buffer から、残りは
        # 線の上から来る。
        raw = FakeRaw(wav_head(4096) + b"\x01" * 4096)
        _info, pcm = open_pcm(raw)
        got = b""
        while len(got) < 600:
            piece = pcm.read(600 - len(got))
            if not piece:
                break
            got += piece
        self.assertEqual(got, b"\x01" * 600)

    def test_a_body_that_is_not_audio_raises(self) -> None:
        # ここは WavError のまま上げる。engine の答えが使えないという
        # 言い換えは `buddy.tts` の仕事。
        with self.assertRaises(WavError):
            open_pcm(FakeRaw(b'{"detail":"speaker not found"}'))


if __name__ == "__main__":
    unittest.main()
