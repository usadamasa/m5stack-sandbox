# pyright: reportPrivateUsage=false
"""The device's own fetch path: the VOICEVOX call.

Nothing here needs a board, a speaker or a running engine. What it
protects is the round trip to the engine — the two POSTs, what goes in
the query, and what happens when the engine is up but answering
something other than audio.

WAV そのものを解くところは `buddy/wav.py` へ割れていて、テストも
`test_wav.py` にある。engine が返すバイト列の組み立ては両方が要るので
`wav_fakes.py` にある。
"""

import json
import unittest
from typing import Any, cast

from buddy import tts
from buddy.tts import FetchError, fetch_speech, quote, tune_query
from wav_fakes import FakeRaw, wav_head


class QuoteTest(unittest.TestCase):
    """MicroPython has no `urllib.parse`, so the encoder is ours."""

    def test_leaves_unreserved_characters_alone(self) -> None:
        self.assertEqual(quote("abcXYZ019-_.~"), "abcXYZ019-_.~")

    def test_percent_encodes_japanese_as_utf8(self) -> None:
        # The whole point of the exercise: the text is Japanese and it
        # travels as a query parameter.
        self.assertEqual(quote("あ"), "%E3%81%82")

    def test_encodes_the_characters_that_would_break_the_query(self) -> None:
        # An unescaped & or = would split one parameter into two, and
        # the engine would synthesise the wrong text or reject the call.
        self.assertEqual(quote("a&b=c"), "a%26b%3Dc")
        self.assertEqual(quote("a b"), "a%20b")

    def test_uses_uppercase_hex(self) -> None:
        # Not cosmetic: %e3 and %E3 are equivalent per RFC 3986, but a
        # lowercase pair is easy to mistake for text in a capture.
        self.assertNotIn("%e3", quote("あ"))


class TuneQueryTest(unittest.TestCase):
    def test_asks_for_the_rate_we_want(self) -> None:
        # The rate is the host's call (`buddy_verbs.DEFAULT_RATE`), not the
        # engine's default — whatever the query came back with is replaced.
        self.assertEqual(
            tune_query({"outputSamplingRate": 24000}, 16000)["outputSamplingRate"], 16000
        )

    def test_forces_mono(self) -> None:
        # playRaw is handed mono. Stereo would play at double speed.
        self.assertIs(tune_query({"outputStereo": True}, 16000)["outputStereo"], False)

    def test_leaves_the_rest_of_the_query_untouched(self) -> None:
        # accent_phrases carries the engine's own reading of the text.
        # Rebuilding it here would change how the line is pronounced.
        query: dict[str, object] = {
            "accent_phrases": [{"moras": ["a"]}],
            "speedScale": 1.0,
            "outputSamplingRate": 24000,
        }
        tuned = tune_query(query, 16000)
        self.assertEqual(tuned["accent_phrases"], [{"moras": ["a"]}])
        self.assertEqual(tuned["speedScale"], 1.0)


class _FakeResponse:
    """`buddy_types.HttpResponse` の面。

    `raw` を省いた呼び出しには空のストリームが入る。audio_query の応答は
    `.raw` を読まれないが、Protocol の側は response に必ず読み口があると
    言っている — 本物の `requests.Response` がそうだから。
    """

    def __init__(
        self,
        payload: dict[str, object] | None = None,
        raw: FakeRaw | None = None,
        status: int = 200,
    ) -> None:
        self.status_code = status
        self._payload: dict[str, object] = payload if payload is not None else {}
        self.raw = raw if raw is not None else FakeRaw(b"")
        self.closed = False

    def json(self) -> dict[str, object]:
        return self._payload

    def close(self) -> None:
        self.closed = True


class _FakeRequests:
    """Stands in for the firmware's frozen `requests` module."""

    def __init__(self, *responses: _FakeResponse | Exception, takes_timeout: bool = True) -> None:
        self._queue = list(responses)
        self.calls: list[tuple[str, bytes | None, dict[str, str] | None]] = []
        self.timeouts: list[object] = []
        # 古いファームウェアの `requests.post` は `timeout` を取らない。
        self._takes_timeout = takes_timeout

    def post(
        self,
        url: str,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        if timeout is not None and not self._takes_timeout:
            # 古いファームウェアはこう答える。
            raise TypeError("post() got an unexpected keyword argument 'timeout'")
        self.calls.append((url, data, headers))
        self.timeouts.append(timeout)
        nxt = self._queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class FetchSpeechTest(unittest.TestCase):
    def setUp(self) -> None:
        # モジュールに残る「この firmware は timeout を取らない」の記憶を、
        # テストごとにまっさらへ戻す。
        tts._post_takes_timeout = True
        self.addCleanup(setattr, tts, "_post_takes_timeout", True)

    def _ok(
        self, rate: int = 16000, pcm_bytes: int = 81920, takes_timeout: bool = True
    ) -> _FakeRequests:
        # The body carries real samples up to a cap, so a test can read
        # across the seam between the over-read header buffer and the
        # socket without materialising 80 KB per case.
        body = b"\x00" * min(pcm_bytes, 8192)
        return _FakeRequests(
            _FakeResponse(payload={"outputSamplingRate": 24000, "outputStereo": True}),
            _FakeResponse(raw=FakeRaw(wav_head(pcm_bytes, rate=rate) + body)),
            takes_timeout=takes_timeout,
        )

    def test_every_post_carries_a_timeout(self) -> None:
        # timeout の無い POST は main loop を人質に取る。WiFi が詰まると
        # 実機は ack を返さなくなり、Ctrl-C も REPL も効かなくなった
        # (issue #92)。止まる場所が C の中なので、socket 側で切るしかない。
        req = self._ok()
        fetch_speech("http://host:50021", "こんにちは", speaker=3, rate=16000, requests_mod=req)
        self.assertEqual(req.timeouts, [tts.HTTP_TIMEOUT, tts.HTTP_TIMEOUT])

    def test_a_firmware_that_will_not_take_a_timeout_still_speaks(self) -> None:
        # `requests.post` の signature はファームウェアのもので、こちらの
        # 持ち物ではない。取らない相手なら timeout 無しへ落ちる — 喋れなく
        # なるよりはブロックしうる方がまし。
        req = self._ok(takes_timeout=False)
        got = fetch_speech("http://host:50021", "やあ", speaker=3, rate=16000, requests_mod=req)
        self.assertEqual(got["rate"], 16000)
        self.assertEqual(len(req.calls), 2)
        self.assertEqual(req.timeouts, [None, None])

    def test_calls_audio_query_then_synthesis(self) -> None:
        req = self._ok()
        fetch_speech("http://host:50021", "こんにちは", speaker=3, rate=16000, requests_mod=req)
        self.assertIn("/audio_query?", req.calls[0][0])
        self.assertIn("/synthesis?", req.calls[1][0])

    def test_sends_the_text_url_encoded_and_the_speaker(self) -> None:
        req = self._ok()
        fetch_speech("http://host:50021", "あ", speaker=3, rate=16000, requests_mod=req)
        self.assertIn("text=%E3%81%82", req.calls[0][0])
        self.assertIn("speaker=3", req.calls[0][0])

    def test_posts_the_tuned_query_as_json(self) -> None:
        req = self._ok()
        fetch_speech("http://host:50021", "あ", speaker=3, rate=16000, requests_mod=req)
        _url, body, headers = req.calls[1]
        assert isinstance(body, bytes)
        self.assertEqual(json.loads(body)["outputSamplingRate"], 16000)
        assert isinstance(headers, dict)
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_reports_what_the_header_said_not_what_we_asked_for(self) -> None:
        # The engine may decline outputSamplingRate. Playing its samples
        # at the rate we wanted is the wrong pitch for the whole line.
        req = self._ok(rate=24000)
        got = fetch_speech("http://host:50021", "あ", speaker=3, rate=16000, requests_mod=req)
        self.assertEqual(got["rate"], 24000)

    def test_hands_back_a_stream_positioned_at_the_first_sample(self) -> None:
        # The header has been consumed by the time the player sees this,
        # so its first read is audio rather than 'RIFF'.
        req = self._ok(pcm_bytes=64)
        got = fetch_speech("http://host:50021", "あ", speaker=3, rate=16000, requests_mod=req)
        self.assertEqual(got["bytes"], 64)
        self.assertEqual(cast(Any, got["stream"]).read(4), b"\x00\x00\x00\x00")

    def test_retries_a_call_that_throws(self) -> None:
        # WiFi drops a request now and then and the engine is a laptop
        # that may be busy. One retry costs a second; failing costs the
        # whole utterance.
        req = _FakeRequests(
            OSError("ECONNRESET"),
            _FakeResponse(payload={"outputSamplingRate": 24000}),
            _FakeResponse(raw=FakeRaw(wav_head(81920) + b"\x00" * 64)),
        )
        got = fetch_speech("http://host:50021", "あ", speaker=3, rate=16000, requests_mod=req)
        self.assertEqual(got["rate"], 16000)

    def test_gives_up_after_the_retries_run_out(self) -> None:
        req = _FakeRequests(*[OSError("ECONNRESET")] * 6)
        with self.assertRaises(FetchError):
            fetch_speech("http://host:50021", "あ", speaker=3, rate=16000, requests_mod=req)

    def test_rejects_an_utterance_too_long_to_be_safe(self) -> None:
        # The existing player caps a single utterance at 30 s. A runaway
        # length would hold the link and the speaker for minutes.
        req = self._ok(pcm_bytes=99_000_000)
        with self.assertRaises(FetchError):
            fetch_speech("http://host:50021", "あ", speaker=3, rate=16000, requests_mod=req)

    def test_treats_an_http_error_as_a_failed_attempt(self) -> None:
        # An engine that is up but answering 503 would otherwise have
        # its error page parsed as audio.
        req = _FakeRequests(*[_FakeResponse(status=503) for _ in range(6)])
        with self.assertRaises(FetchError):
            fetch_speech("http://host:50021", "あ", speaker=3, rate=16000, requests_mod=req)

    def test_a_body_that_is_not_audio_fails_as_a_fetch_not_a_wav(self) -> None:
        # FastAPI answers a bad speaker id with JSON and a 200 is not
        # guaranteed to mean audio. The caller handles FetchError; a
        # WavError escaping from here would reach the app unhandled.
        req = _FakeRequests(
            _FakeResponse(payload={"outputSamplingRate": 24000}),
            _FakeResponse(raw=FakeRaw(b'{"detail":"speaker not found"}')),
        )
        with self.assertRaises(FetchError):
            fetch_speech("http://host:50021", "あ", speaker=99, rate=16000, requests_mod=req)

    def test_rejects_blank_text_without_touching_the_network(self) -> None:
        req = _FakeRequests()
        with self.assertRaises(FetchError):
            fetch_speech("http://host:50021", "   ", speaker=3, rate=16000, requests_mod=req)
        self.assertEqual(req.calls, [])


if __name__ == "__main__":
    unittest.main()
