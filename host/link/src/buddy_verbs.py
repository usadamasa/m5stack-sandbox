"""The verbs the host sends: chat, speech and debug.

Each one takes a link rather than owning one. `BuddyLink` from a CLI
run and `ResidentLink` from the MCP server both satisfy `Requester`,
which is what lets a one-shot command and a session-long server share
this code.
"""

from __future__ import annotations

import os
import socket
import time
from typing import Protocol

from buddy_text import DEFAULT_PACE, normalize_for_device, split_for_device
from buddy_wire import Message, Requester


def say(
    link: Requester,
    text: str,
    role: str = "claude",
    timeout: float = 5.0,
    pace: float = DEFAULT_PACE,
) -> list[Message]:
    """Put `text` on the device's chat panel. Returns one ack per part.

    Sends synchronously, one part at a time: waiting for each ack means
    a failure names the part that failed instead of leaving the
    transcript half-written.

    `pace` is the pause between parts. The panel only shows its last
    rows, so without it a split message scrolls past faster than anyone
    can read; pass 0 when nobody is watching the screen.
    """
    parts = split_for_device(normalize_for_device(text))
    acks: list[Message] = []
    for i, part in enumerate(parts):
        if i and pace > 0:
            time.sleep(pace)
        acks.append(
            link.request(
                {"cmd": "chat.say", "role": role, "text": part, "id": f"say-{i}"},
                "chat.say",
                timeout=timeout,
            )
        )
    return acks


# ----- network
#
# Nothing here brings the radio up, and nothing on the device does
# either. The credentials live in the bundle's own boot-time connect,
# written once by `host/provision_wifi.py`, so the device is already on
# the network before anything in this file runs.
#
# It has to be that way round. Measured on hardware: the device
# associates in well under a second from the REPL and not at all once
# the app is running — `connect` is accepted, the association never
# completes, and 15 s later the driver still says "connecting". The
# ESP-IDF heap has ~12 KB free in its largest region with nothing but
# the launcher loaded, and bringing a link up wants DRAM. The app
# inherits a link; it cannot make one.
#
# So there is no `net.*` verb, no `--wifi`, and no credential on this
# path at all. A device that will not speak has not been provisioned
# (or the engine is down) — `host/provision_wifi.py --verify` says
# which.


# ----- speech
#
# Synthesis happens on a VOICEVOX engine, and the device fetches from it
# directly over WiFi. Nothing but the text crosses the cable.

# VOICEVOX's own default port.
_ENGINE_PORT = 50021

# Zundamon, normal. Style ids come from the engine's /speakers.
ZUNDAMON = 3

# 16 kHz over the engine's default 24 kHz. The device has 61 KB of heap
# and no PSRAM, so a third off the stream is worth more than the
# bandwidth it saves.
DEFAULT_RATE = 16000

# Long enough to cover synthesis, which is seconds: the device does not
# answer speak.say until the engine has produced the whole WAV and the
# response headers are in.
_SYNTHESIS_TIMEOUT_S = 60.0

# A loopback engine is reachable from this Mac and from nowhere else.
# It is the likeliest mistake to make here, and on the device it
# surfaces as a connection timeout seconds later, nowhere near its
# cause.
_LOOPBACK = ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def voicevox_url(explicit: str | None = None) -> str:
    """Where the engine is, as the device will address it.

    Resolution order: the argument, then `$VOICEVOX_URL`, then this
    machine's LAN address — the engine runs here, in Docker, published
    with `-p 50021:50021` so it listens on every interface rather than
    just loopback.

    A bare host or address is given a scheme and the default port. The
    device does no URL parsing; it concatenates paths onto whatever it
    is handed.
    """
    raw = explicit or os.environ.get("VOICEVOX_URL") or _lan_address()
    if not raw:
        raise ValueError(
            "cannot work out where VOICEVOX is — set $VOICEVOX_URL to "
            "http://<this-mac-on-the-lan>:50021"
        )

    url = raw.strip().rstrip("/")
    if "://" not in url:
        url = f"http://{url}"
    if ":" not in url.split("://", 1)[1]:
        url = f"{url}:{_ENGINE_PORT}"

    host = url.split("://", 1)[1].split(":")[0]
    if host in _LOOPBACK:
        raise ValueError(
            f"{url} is loopback — reachable from this Mac but not from the "
            "device. Use this machine's LAN address and publish the "
            "container with `-p 50021:50021`."
        )
    return url


def _lan_address() -> str | None:
    """This machine's address on the LAN, or None.

    Opening a UDP socket towards an off-link address and asking what the
    kernel bound is the portable way to find which interface would carry
    the traffic. No packet is sent.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 53))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


class SpeechLink(Requester, Protocol):
    """A link that can also wait for an ack the request did not carry.

    `speak.say` is answered when playback starts; `speak.end` follows
    when it finishes, seconds later, with no command in between to hang
    it off.
    """

    def await_ack(self, expect: str, timeout: float = 5.0) -> Message: ...


def speak(
    link: SpeechLink,
    text: str,
    url: str | None = None,
    speaker: int = ZUNDAMON,
    rate: int = DEFAULT_RATE,
    timeout: float = 10.0,
) -> Message:
    """Have the device fetch `text` from VOICEVOX and play it.

    Returns the `speak.end` ack, which arrives once the last block has
    been played. Blocks for synthesis plus playback.

    `stalls` は再生が始まった後に speaker が空になった回数 — 0 でなければ
    その回数だけ音が途切れている。socket を待っただけの tick は数えない。
    """
    if not text.strip():
        raise ValueError("nothing to say")

    ack = link.request(
        {
            "cmd": "speak.say",
            "text": text,
            "url": voicevox_url(url),
            "speaker": speaker,
            "rate": rate,
        },
        "speak.say",
        timeout=_SYNTHESIS_TIMEOUT_S,
    )
    if not ack.get("ok"):
        # Waiting for speak.end here would block until the timeout for
        # an utterance that never started.
        raise RuntimeError(f"device refused speak.say: {ack.get('err', ack)}")

    playback_s = ack.get("bytes", 0) / 2 / max(ack.get("rate", rate), 1)
    return link.await_ack("speak.end", timeout=playback_s + timeout)


# ----- debug
#
# Verbs `device/buddy/debug.py` answers. Bare here, `dbg.`-prefixed on
# the wire, and the device sets the ack name equal to the command name —
# which is what lets one helper cover all of them without a table.
#
# The device does not import that module until the first of these
# arrives, so the first call of a session pays an import and the ones
# after it do not. `off` drops it again.
DEBUG_OPS = ("mem", "frag", "gc", "state", "eval", "exec", "off")

# The two that compile a string on the device, and so the two that need
# one.
_DEBUG_OPS_WITH_SOURCE = ("eval", "exec")


def debug(link: Requester, op: str, src: str = "", timeout: float = 8.0) -> Message:
    """Ask the running app one question about itself.

    Bulky answers do not come back through here. `frag` prints its heap
    map, and a failing `eval` prints its traceback, to the log channel —
    drain the link afterwards to read them.
    """
    if op not in DEBUG_OPS:
        raise ValueError(f"unknown debug op {op!r}; expected one of {', '.join(DEBUG_OPS)}")
    if op in _DEBUG_OPS_WITH_SOURCE and not src:
        raise ValueError(f"dbg.{op} needs a `src` to run")
    cmd = f"dbg.{op}"
    obj: Message = {"cmd": cmd}
    if op in _DEBUG_OPS_WITH_SOURCE:
        obj["src"] = src
    return link.request(obj, cmd, timeout=timeout)


# What the device says when the debug module is first pulled in. Audio
# only: the chat panel may well be the thing being inspected, and
# overwriting it to announce that somebody is looking at it would be a
# poor trade.
DEBUG_ENTER_TEXT = "デバッグモードに入ったのだ"


def announce_debug_entry(link: SpeechLink, ack: Message, url: str | None = None) -> bool:
    """Say "debug mode" out loud, if this ack is the one that entered it.

    Only the device knows which call that was — it sets `entered` on the
    frame that imported `buddy.debug`, because a fresh host process
    cannot tell whether an earlier one already loaded it.

    Returns whether anything was actually said. Failure is swallowed on
    purpose: a silent engine, a dropped WiFi link or an unplugged speaker
    are all reasons for the announcement not to happen and none of them
    are reasons for the inspection to fail.
    """
    if not ack.get("entered"):
        return False
    try:
        speak(link, DEBUG_ENTER_TEXT, url=url)
    except Exception as e:
        print(f"  (could not announce debug mode: {e})")
        return False
    return True
