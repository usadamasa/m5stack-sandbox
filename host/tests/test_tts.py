"""The device's own fetch path: WAV framing and the VOICEVOX call.

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

import json
import struct
import unittest

import buddy_tts
from buddy_tts import (
    FetchError,
    WavError,
    connect_wifi,
    fetch_speech,
    parse_wav_header,
    quote,
    tune_query,
)


def _chunk(cid: bytes, body: bytes) -> bytes:
    """One RIFF chunk, padded to an even length as the spec requires."""
    return cid + struct.pack("<I", len(body)) + body + (b"\x00" if len(body) & 1 else b"")


def _fmt(rate: int = 16000, channels: int = 1, bits: int = 16) -> bytes:
    return struct.pack(
        "<HHIIHH",
        1,  # PCM
        channels,
        rate,
        rate * channels * bits // 8,
        channels * bits // 8,
        bits,
    )


def _wav(
    pcm_bytes: int,
    before_data: bytes = b"",
    rate: int = 16000,
    channels: int = 1,
    bits: int = 16,
) -> bytes:
    """A header describing `pcm_bytes` of samples. The samples themselves
    are not appended: the parser only ever sees the head of the stream."""
    body = b"WAVE" + _chunk(b"fmt ", _fmt(rate, channels, bits)) + before_data
    body += b"data" + struct.pack("<I", pcm_bytes)
    return b"RIFF" + struct.pack("<I", len(body) + pcm_bytes) + body


class WavHeaderTest(unittest.TestCase):
    def test_reads_what_voicevox_actually_sends(self) -> None:
        # The measured case: fmt then data, PCM starting at 44.
        head = _wav(81920)
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
        self.assertEqual(parse_wav_header(_wav(4096, rate=24000))["rate"], 24000)

    def test_finds_data_behind_a_chunk_we_do_not_care_about(self) -> None:
        # VOICEVOX 0.25.2 emits none, but a fixed 44-byte skip would be
        # betting the whole audio path on that staying true.
        head = _wav(2048, before_data=_chunk(b"LIST", b"INFOsoftware"))
        got = parse_wav_header(head)
        self.assertEqual(got["bytes"], 2048)
        self.assertEqual(head[got["offset"] - 8 : got["offset"] - 4], b"data")

    def test_skips_the_pad_byte_after_an_odd_chunk(self) -> None:
        # RIFF pads odd-sized chunks to even boundaries and does not
        # count the pad in the size field. Miss it and every following
        # chunk id is read one byte late, which finds nothing.
        head = _wav(2048, before_data=_chunk(b"LIST", b"odd"))
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
        head = b"RIFF" + struct.pack("<I", 4) + b"WAVE" + _chunk(b"fmt ", _fmt())
        with self.assertRaises(WavError):
            parse_wav_header(head)

    def test_rejects_a_format_the_speaker_cannot_play(self) -> None:
        # playRaw takes signed 16-bit mono. Handing it stereo or 8-bit
        # would play as noise at double speed rather than fail.
        for channels, bits in ((2, 16), (1, 8)):
            with self.assertRaises(WavError, msg=f"{channels}ch/{bits}-bit"):
                parse_wav_header(_wav(2048, channels=channels, bits=bits))


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
        # 16 kHz over 24 kHz is a third off both the transfer and the
        # heap, on a board with 61 KB of the latter.
        self.assertEqual(
            tune_query({"outputSamplingRate": 24000}, 16000)["outputSamplingRate"], 16000
        )

    def test_forces_mono(self) -> None:
        # playRaw is handed mono. Stereo would play at double speed.
        self.assertIs(tune_query({"outputStereo": True}, 16000)["outputStereo"], False)

    def test_leaves_the_rest_of_the_query_untouched(self) -> None:
        # accent_phrases carries the engine's own reading of the text.
        # Rebuilding it here would change how the line is pronounced.
        query = {
            "accent_phrases": [{"moras": ["a"]}],
            "speedScale": 1.0,
            "outputSamplingRate": 24000,
        }
        tuned = tune_query(query, 16000)
        self.assertEqual(tuned["accent_phrases"], [{"moras": ["a"]}])
        self.assertEqual(tuned["speedScale"], 1.0)


class _FakeResponse:
    def __init__(self, payload: object = None, raw: object = None, status: int = 200) -> None:
        self.status_code = status
        self._payload = payload
        self.raw = raw
        self.closed = False

    def json(self) -> object:
        return self._payload

    def close(self) -> None:
        self.closed = True


class _FakeRequests:
    """Stands in for the firmware's frozen `requests` module."""

    def __init__(self, *responses: object) -> None:
        self._queue = list(responses)
        self.calls: list[tuple[str, object, object]] = []

    def post(
        self,
        url: str,
        data: object = None,
        headers: object = None,
        **_kw: object,
    ) -> object:
        self.calls.append((url, data, headers))
        nxt = self._queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _wav_head(pcm_bytes: int = 81920, rate: int = 16000) -> bytes:
    return _wav(pcm_bytes, rate=rate)


class _FakeRaw:
    """A socket-shaped object holding a fixed stream."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.closed = False

    def read(self, n: int) -> bytes:
        chunk = self.data[self.pos : self.pos + n]
        self.pos += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class FetchSpeechTest(unittest.TestCase):
    def _ok(self, rate: int = 16000, pcm_bytes: int = 81920) -> _FakeRequests:
        # The body carries real samples up to a cap, so a test can read
        # across the seam between the over-read header buffer and the
        # socket without materialising 80 KB per case.
        body = b"\x00" * min(pcm_bytes, 8192)
        return _FakeRequests(
            _FakeResponse(payload={"outputSamplingRate": 24000, "outputStereo": True}),
            _FakeResponse(raw=_FakeRaw(_wav_head(pcm_bytes, rate) + body)),
        )

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
        self.assertEqual(got["stream"].read(4), b"\x00\x00\x00\x00")

    def test_retries_a_call_that_throws(self) -> None:
        # WiFi drops a request now and then and the engine is a laptop
        # that may be busy. One retry costs a second; failing costs the
        # whole utterance.
        req = _FakeRequests(
            OSError("ECONNRESET"),
            _FakeResponse(payload={"outputSamplingRate": 24000}),
            _FakeResponse(raw=_FakeRaw(_wav_head() + b"\x00" * 64)),
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
            _FakeResponse(raw=_FakeRaw(b'{"detail":"speaker not found"}')),
        )
        with self.assertRaises(FetchError):
            fetch_speech("http://host:50021", "あ", speaker=99, rate=16000, requests_mod=req)

    def test_the_stream_crosses_from_the_held_bytes_into_the_socket(self) -> None:
        # The header probe over-reads, so the first samples arrive in a
        # buffer and the rest come off the wire. A reader that lost the
        # seam would drop or repeat audio exactly once per utterance.
        req = self._ok(pcm_bytes=4096)
        got = fetch_speech("http://host:50021", "あ", speaker=3, rate=16000, requests_mod=req)
        stream = got["stream"]
        first = b""
        while len(first) < 600:
            chunk = stream.read(600 - len(first))
            if not chunk:
                break
            first += chunk
        self.assertEqual(first, b"\x00" * 600)

    def test_rejects_blank_text_without_touching_the_network(self) -> None:
        req = _FakeRequests()
        with self.assertRaises(FetchError):
            fetch_speech("http://host:50021", "   ", speaker=3, rate=16000, requests_mod=req)
        self.assertEqual(req.calls, [])


class _FakeTime:
    """MicroPython's ticks API, driven by sleep_ms rather than a clock."""

    now = 0

    @classmethod
    def ticks_ms(cls) -> int:
        return cls.now

    @classmethod
    def ticks_diff(cls, a: int, b: int) -> int:
        return a - b

    @classmethod
    def sleep_ms(cls, ms: int) -> None:
        cls.now += ms


class _FakeWLAN:
    def __init__(
        self,
        connects_after: int = 1,
        ip: str = "192.168.0.42",
        refuse_connects: int = 0,
        active: bool = False,
        status_code: int = 1010,
    ) -> None:
        self._active = active
        self.status_code = status_code
        self._connected = False
        self._polls = 0
        self._connects_after = connects_after
        self._refuse = refuse_connects
        self._ip = ip
        self.connect_calls: list[tuple[str, str]] = []
        self.active_calls: list[bool] = []
        self.config_calls: list[dict[str, object]] = []
        self.disconnects = 0

    def config(self, **kw: object) -> None:
        self.config_calls.append(kw)

    def active(self, on: bool | None = None) -> bool:
        if on is not None:
            self.active_calls.append(on)
            self._active = on
        return self._active

    def isconnected(self) -> bool:
        if self.connect_calls:
            self._polls += 1
            if self._polls >= self._connects_after:
                self._connected = True
        return self._connected

    def connect(self, ssid: str, psk: str) -> None:
        if self._refuse > 0:
            self._refuse -= 1
            # What the ESP32 driver raises when it is asked to associate
            # while it is already mid-attempt.
            raise OSError("Wifi Internal State Error")
        self.connect_calls.append((ssid, psk))

    def disconnect(self) -> None:
        self.disconnects += 1
        self._connected = False

    def ifconfig(self) -> tuple[str, str, str, str]:
        return (self._ip, "255.255.255.0", "192.168.0.1", "192.168.0.1")

    def status(self, what: str | None = None) -> int:
        if what == "rssi":
            return -52
        return self.status_code


class _FakeNetwork:
    STA_IF = 0

    def __init__(self, wlan: _FakeWLAN) -> None:
        self._wlan = wlan

    def WLAN(self, _iface: int) -> _FakeWLAN:
        return self._wlan


class ConnectWifiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._real_time = buddy_tts.time
        buddy_tts.time = _FakeTime()
        self.addCleanup(setattr, buddy_tts, "time", self._real_time)
        _FakeTime.now = 0

    def test_connects_and_reports_the_address(self) -> None:
        wlan = _FakeWLAN(connects_after=3)
        got = connect_wifi("home", "hunter2", network_mod=_FakeNetwork(wlan))
        self.assertTrue(got["ok"])
        self.assertEqual(got["ip"], "192.168.0.42")
        self.assertEqual(wlan.connect_calls, [("home", "hunter2")])
        self.assertTrue(wlan.active())

    def test_an_association_that_is_already_up_is_left_alone(self) -> None:
        # The host sends net.config before every utterance in the
        # simplest arrangement. Re-associating each time would add
        # seconds and drop the link in between.
        wlan = _FakeWLAN(connects_after=1)
        wlan._connected = True
        got = connect_wifi("home", "hunter2", network_mod=_FakeNetwork(wlan))
        self.assertTrue(got["ok"])
        self.assertEqual(wlan.connect_calls, [])

    def test_gives_up_after_the_timeout(self) -> None:
        # An SSID that is not here must not hang the app's loop forever.
        wlan = _FakeWLAN(connects_after=10_000)
        got = connect_wifi("nope", "x", timeout_ms=1000, network_mod=_FakeNetwork(wlan))
        self.assertFalse(got["ok"])
        self.assertIn("timeout", got["err"])

    def test_bounds_the_reconnects_so_the_timeout_can_mean_something(self) -> None:
        # `connect()` retries forever by default on this port. While it
        # does, `status()` stays STAT_CONNECTING whether the password is
        # wrong or the AP is absent, and the driver keeps trying past
        # any deadline set here. `config(reconnects=n)` is the
        # documented way to bound it, and is what M5Stack's own WLAN STA
        # example does before connecting.
        wlan = _FakeWLAN(connects_after=2)
        connect_wifi("home", "hunter2", network_mod=_FakeNetwork(wlan))
        self.assertEqual(wlan.config_calls, [{"reconnects": buddy_tts._RECONNECTS}])

    def test_ends_the_launchers_attempt_before_starting_its_own(self) -> None:
        # main.py connects to an event SSID at boot, and the default
        # forever-retry keeps that attempt alive long after it has
        # failed. A second connect() into that state is refused with
        # "Wifi Internal State Error"; disconnect() is what ends it.
        wlan = _FakeWLAN(connects_after=2, active=True)
        connect_wifi("home", "hunter2", network_mod=_FakeNetwork(wlan))
        self.assertEqual(wlan.disconnects, 1)
        self.assertEqual(wlan.connect_calls, [("home", "hunter2")])

    def test_never_takes_the_interface_down(self) -> None:
        # A down/up cycle appears in neither the MicroPython docs nor
        # M5Stack's example, and doing it to a driver that is mid-
        # attempt resets the board — which is what the port vanishing
        # partway through a paste turned out to be.
        wlan = _FakeWLAN(connects_after=2, active=True)
        connect_wifi("home", "hunter2", network_mod=_FakeNetwork(wlan))
        self.assertNotIn(False, wlan.active_calls)

    def test_a_connect_that_is_still_refused_is_reported(self) -> None:
        wlan = _FakeWLAN(refuse_connects=99, active=True)
        got = connect_wifi("home", "hunter2", network_mod=_FakeNetwork(wlan))
        self.assertFalse(got["ok"])
        self.assertIn("Wifi Internal State Error", got["err"])

    def test_refuses_an_empty_ssid(self) -> None:
        wlan = _FakeWLAN()
        got = connect_wifi("", "x", network_mod=_FakeNetwork(wlan))
        self.assertFalse(got["ok"])
        self.assertEqual(wlan.connect_calls, [])

    def test_reports_the_ssid_it_tried(self) -> None:
        # The ack is the only feedback the host gets; without the SSID a
        # typo looks the same as an AP that is out of range.
        wlan = _FakeWLAN(connects_after=10_000)
        got = connect_wifi("typo", "x", timeout_ms=500, network_mod=_FakeNetwork(wlan))
        self.assertEqual(got["ssid"], "typo")

    def test_never_puts_the_password_in_the_result(self) -> None:
        # The result is JSON-encoded onto the wire and printed by the
        # host. The password has no business in either.
        wlan = _FakeWLAN(connects_after=2)
        got = connect_wifi("home", "hunter2", network_mod=_FakeNetwork(wlan))
        self.assertNotIn("hunter2", json.dumps(got))


class HandleNetRawTest(unittest.TestCase):
    def setUp(self) -> None:
        self._real_time = buddy_tts.time
        buddy_tts.time = _FakeTime()
        self.addCleanup(setattr, buddy_tts, "time", self._real_time)
        _FakeTime.now = 0

    def _line(self, **over: object) -> bytes:
        msg: dict[str, object] = {"cmd": "net.config", "ssid": "home", "psk": "hunter2"}
        msg.update(over)
        return json.dumps(msg).encode("utf-8")

    def test_connects_and_acks(self) -> None:
        wlan = _FakeWLAN(connects_after=2)
        got = buddy_tts.handle_net_raw(self._line(), network_mod=_FakeNetwork(wlan))
        assert got is not None
        self.assertEqual(got["ack"], "net.config")
        self.assertTrue(got["ok"])
        self.assertEqual(wlan.connect_calls, [("home", "hunter2")])

    def test_a_failure_still_acks(self) -> None:
        # Silence here would leave the host waiting on a request it can
        # never distinguish from a device that crashed.
        wlan = _FakeWLAN(connects_after=10_000)
        got = buddy_tts.handle_net_raw(self._line(timeout_ms=500), network_mod=_FakeNetwork(wlan))
        assert got is not None
        self.assertEqual(got["ack"], "net.config")
        self.assertFalse(got["ok"])

    def test_the_ack_carries_no_password(self) -> None:
        wlan = _FakeWLAN(connects_after=2)
        got = buddy_tts.handle_net_raw(self._line(), network_mod=_FakeNetwork(wlan))
        self.assertNotIn("hunter2", json.dumps(got))

    def test_other_verbs_fall_through(self) -> None:
        # Anything this does not own has to reach buddy_protocol
        # untouched, or the upstream dispatcher never sees it.
        self.assertIsNone(buddy_tts.handle_net_raw(b'{"cmd":"status"}'))
        self.assertIsNone(buddy_tts.handle_net_raw(b'{"cmd":"speak.say"}'))

    def test_junk_is_not_ours(self) -> None:
        self.assertIsNone(buddy_tts.handle_net_raw(b"not json"))
        self.assertIsNone(buddy_tts.handle_net_raw(b"[1,2,3]"))


if __name__ == "__main__":
    unittest.main()
