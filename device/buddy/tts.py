"""Fetch speech from a VOICEVOX engine over WiFi.

The device used to be handed PCM over the USB cable, synthesised on the
Mac by `say`. It now asks for its own audio instead: the Mac runs
`voicevox/voicevox_engine` in Docker and this end streams the result
straight into `M5.Speaker`. The voice is Zundamon (`speaker=3`).

### Why the engine and not a hosted API

The public VOICEVOX-compatible hosts are HTTPS-only, and a TLS handshake
costs this chip about 40 KB of heap it would rather spend on audio. They
also answer with a job to poll rather than a stream, and their download
URLs 404 until synthesis finishes. A local engine is plain HTTP, answers
immediately, and — the part that matters most here — takes
`outputSamplingRate`, so the rate is the host's choice rather than the
engine's (`buddy_verbs.DEFAULT_RATE` says which and why).

### Who brings the radio up

Not this module, and not the app. `/flash/main.py` connects at boot,
before either exists, using the credentials in `/flash/wifi_event.py` —
which `host/provision_wifi.py` writes once.

It has to be that way round. Measured on hardware, `connect()` from
inside the running app is accepted and then never completes: the
ESP-IDF heap has around 12 KB free in its largest region once the
bundle is loaded, and bringing a link up wants DRAM. The app inherits a
link; it cannot make one.

So there is no `net.*` command and nothing here touches `network`. If
the radio is down, the first POST below fails with an OSError and the
utterance is lost — which is the correct and only outcome, since this
end cannot fix it.

### The two calls

    POST /audio_query?text=...&speaker=3   -> a JSON query (~2.7 KB)
    POST /synthesis?speaker=3              -> audio/wav, Content-Length set

`outputSamplingRate` is edited into the query between the two. Measured
against VOICEVOX 0.25.2: 2.56 s of speech is 122924 bytes at the default
24 kHz and 81964 at 16 kHz.

### 割れているところ

返ってきた WAV を解くのは `buddy/wav.py`。RIFF の並びを歩いて samples の
頭を見つけるところと、読み過ぎたぶんを抱える読み口があちらにある。ここに
残るのは engine とのやりとり — URL の組み立て、retry、長さの上限、そして
解けなかったものを `FetchError` に言い換える判断。

### MicroPython

No `typing`, no `__future__`, no slice deletion, no `contextlib`. The
transport is injectable so `host/tests/test_tts.py` can exercise all of
this without a board or a running engine.
"""

import json

from buddy import wav

# 型検査だけの import。デバイスの上では `False` なので走らない。事情と
# 使い方は `device/typings/buddy_types.pyi` の docstring にある。
_TYPE_CHECKING = False
if _TYPE_CHECKING:
    from buddy_types import (  # noqa: F401
        HttpClient,
        HttpResponse,
        SpeechSource,
    )

# Characters RFC 3986 says need no escaping. Everything else in the
# text — which is Japanese, so almost all of it — travels as %XX.
_UNRESERVED = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"

# Longest utterance we will start. Matches `buddy.speak._MAX_BYTES`:
# 30 s at 16 kHz 16-bit (20 s at 24 kHz), far more than a notification
# and far less than enough to hold the speaker and the link for a
# noticeable time. Kept as
# its own constant rather than imported so neither module has to load
# the other on a device counting every kilobyte.
_MAX_BYTES = 960000

# Attempts per HTTP call, not retries on top of one. WiFi drops a
# request now and then and the engine is a laptop that may be busy;
# a second attempt costs about a second, and failing costs the whole
# utterance. Availability beyond this is explicitly out of scope.
_TRIES = 3

# 1 回の POST が待ってよい上限 (秒)。
#
# これが無いと、詰まった WiFi がアプリの main loop をそのまま人質に取る。
# 実測 (issue #92): ack が一切返らなくなり、Ctrl-C も raw REPL も効かず、
# BtnRST でしか戻らなくなった。止まる場所は C のブロッキング呼び出しの中で、
# そこでは MicroPython が割り込みを見に来ない — socket 側で切るしかない。
#
# 8 秒は、engine が実際に使う時間 (短い台詞で合成まで込み 1.8 秒) の倍以上。
# `_TRIES` と掛けて最悪 24 秒だが、そこまで行くのは engine が死んでいるときで、
# その 24 秒は「黙る」だけで済む。
HTTP_TIMEOUT = 8.0

# ファームウェアの `requests.post` が `timeout` を取らなかったら False へ倒れ、
# 以後は渡さない。signature はこちらの持ち物ではないので、取らない相手に
# 当たったときに黙って喋れなくなるより、ブロックしうる方を選ぶ。
_post_takes_timeout = True


class FetchError(RuntimeError):
    """The engine could not be reached, or gave us something unusable."""


def quote(text: str) -> str:
    """Percent-encode `text` for a query parameter.

    MicroPython has no `urllib.parse`. The text is Japanese, so nearly
    every byte needs escaping — and an unescaped `&` or `=` would split
    one parameter into two and have the engine synthesise the wrong
    line rather than fail.
    """
    out = []  # type: list[str]
    for b in text.encode("utf-8"):
        if b in _UNRESERVED:
            out.append(chr(b))
        else:
            out.append("%%%02X" % b)
    return "".join(out)


def tune_query(query, rate):
    # type: (dict[str, object], int) -> dict[str, object]
    """Edit the engine's audio_query in place, and hand it back.

    Only the two output settings are touched. `accent_phrases` carries
    the engine's own reading of the text — rebuilding any of it here
    would change how the line is pronounced.

    In place rather than copied: the query is a few kilobytes of nested
    dicts and this board has 61 KB of heap.
    """
    query["outputSamplingRate"] = rate
    query["outputStereo"] = False
    return query


def _default_requests():
    """The firmware's HTTP client.

    Frozen into this build as `requests` — the name `urequests` was
    retired upstream, which is why the reference implementations that
    predate MicroPython 1.20 import the other one.
    """
    import requests

    return requests


def _call_post(req, url, data, headers):
    # type: (HttpClient, str, bytes | None, dict[str, str] | None) -> HttpResponse
    """1 回ぶんの POST。`timeout` を取らないファームウェアなら無しで呼び直す。"""
    global _post_takes_timeout
    if _post_takes_timeout:
        try:
            return req.post(url, data=data, headers=headers, timeout=HTTP_TIMEOUT)
        except TypeError:
            _post_takes_timeout = False
            print("buddy.tts: this requests has no timeout=; POSTs can block")
    return req.post(url, data=data, headers=headers)


def _post(req, url, data, headers):
    # type: (HttpClient, str, bytes | None, dict[str, str] | None) -> HttpResponse
    """POST with retries. Raises FetchError once the attempts run out.

    `req` is the firmware's `requests` module or a test double — no
    `typing.Protocol` on MicroPython to name what the two have in common,
    so the calls through it below are ignored per-line, and the response
    it hands back stays equally duck-typed for the same reason.
    """
    last = None
    for _ in range(_TRIES):
        try:
            res = _call_post(req, url, data, headers)
        except Exception as e:  # OSError, and whatever the TLS-less stack raises
            last = e
            continue
        status = getattr(res, "status_code", 200)
        if status != 200:
            last = "HTTP " + str(status)
            try:
                res.close()
            except Exception:
                pass
            continue
        return res
    raise FetchError("POST failed after " + str(_TRIES) + " tries: " + str(last))


def fetch_speech(url, text, speaker=3, rate=24000, requests_mod=None):
    # type: (str, str, int, int, HttpClient | None) -> SpeechSource
    """Ask VOICEVOX for `text` and return a stream of its samples.

    Returns ``{"stream", "bytes", "rate", "response"}``: a reader
    already positioned at the first sample, how many bytes of PCM to
    expect, the rate the engine actually used, and the response to close
    when playback ends.

    Blocks for as long as synthesis takes — seconds. The caller's UI
    loop stops for that time; there is no way around it without a
    non-blocking HTTP client, and the alternative to blocking is a
    second thread, which on this build shares a GIL and would not help.
    """
    if not text.strip():
        raise FetchError("nothing to say")

    req = requests_mod if requests_mod is not None else _default_requests()
    base = url.rstrip("/")
    speaker_arg = "speaker=" + str(int(speaker))

    # `res` is whatever `req.post()` handed back — equally duck-typed, so
    # every member access on it below is ignored per-line for the same
    # reason as `_post()`'s own body.
    res = _post(req, base + "/audio_query?text=" + quote(text) + "&" + speaker_arg, None, None)
    try:
        query = res.json()
    finally:
        try:
            res.close()
        except Exception:
            pass

    body = json.dumps(tune_query(query, rate)).encode("utf-8")
    # The query is the largest thing we hold at once. Let it go before
    # the audio starts arriving.
    query = None

    res = _post(
        req,
        base + "/synthesis?" + speaker_arg,
        body,
        {"Content-Type": "application/json"},
    )
    body = None

    try:
        info, pcm = wav.open_pcm(res.raw)
    except wav.WavError as e:
        try:
            res.close()
        except Exception:
            pass
        raise FetchError("engine did not answer with playable audio: " + str(e))

    if info["bytes"] < 1 or info["bytes"] > _MAX_BYTES:
        try:
            res.close()
        except Exception:
            pass
        raise FetchError("utterance is " + str(info["bytes"]) + " bytes, out of range")

    return {
        "stream": pcm,
        "bytes": info["bytes"],
        "rate": info["rate"],
        "response": res,
    }
