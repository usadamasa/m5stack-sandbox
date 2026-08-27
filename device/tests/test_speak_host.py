"""ホスト側から見た発話の契約: `buddy_verbs.speak` と `voicevox_url`。

デバイス側の `SpeechPlayer` は `test_speak.py`。同じ経路の両端だが、こちらは
デバイスのモジュールを import しない — link の double に対して、送る命令の
形と待ち方だけを見る。
"""

import os
import unittest
from typing import Any
from unittest import mock

import buddy_verbs
from buddy import speak as buddy_speak


class RateContractTest(unittest.TestCase):
    def test_both_ends_default_to_the_same_rate(self) -> None:
        # ホストが rate を省いたときと、デバイスが msg に rate を見つけなかった
        # ときで違う音が出ないように。
        self.assertEqual(buddy_speak._DEFAULT_RATE, buddy_verbs.DEFAULT_RATE)  # pyright: ignore[reportPrivateUsage]


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
