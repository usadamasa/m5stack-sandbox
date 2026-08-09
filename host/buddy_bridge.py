"""Host-side client for the Claude Buddy protocol over USB serial.

Speaks the same line-delimited JSON that `buddy_protocol.py` implements
on the device, but over the USB CDC console instead of BLE. This is what
lets Claude Code drive the Cardputer-Adv; Claude Desktop's Hardware
Buddy owns the BLE side and is not available here.

### Framing

The device multiplexes protocol traffic and `print()` logging onto one
channel, so protocol lines carry a sentinel prefix and everything else
is passed through as log output. `SENTINEL` must stay byte-identical to
`_SENTINEL` in `device/buddy_serial.py`.

### CLI

    python host/buddy_bridge.py --port /dev/cu.usbmodem101 --start --status

`--start` launches the app over the REPL. Note that the device disables
Ctrl-C (`micropython.kbd_intr(-1)`) while the serial transport is up, so
once the app is running the only way back to the REPL is BtnRST.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import socket
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Protocol

import serial

from device_repl import Repl, ReplError, connect_repl, run_and_release

# Keep in sync with _SENTINEL in device/buddy_serial.py.
SENTINEL = b"\x1eBUDDY1 "

# Matches buddy_protocol._send, which uses the same separators. Keeping
# the encodings identical means a byte-level diff of a capture is
# meaningful in both directions.
_JSON_SEPARATORS = (",", ":")

# Protocol payloads are whatever the device chose to send, so the values
# stay Any; naming the alias at least keeps the intent legible.
Message = dict[str, Any]

# What the demux hands back: ("protocol", json body) or ("log", raw line).
Item = tuple[str, bytes]


class SerialPort(Protocol):
    """The slice of `serial.Serial` this module actually uses.

    Narrow on purpose: it is what lets the tests drive a fake port
    without pulling in pyserial's full surface, and it documents the
    contract a replacement transport would have to meet.
    """

    @property
    def in_waiting(self) -> int: ...

    def read(self, size: int = 1, /) -> bytes: ...

    def write(self, data: bytes, /) -> int | None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


SerialFactory = Callable[..., SerialPort]


def encode(obj: Message) -> bytes:
    """Frame one message for the device."""
    body = json.dumps(obj, separators=_JSON_SEPARATORS).encode("utf-8")
    return SENTINEL + body + b"\n"


def decode(payload: bytes) -> Message:
    """Parse one protocol payload (sentinel already stripped)."""
    parsed: Any = json.loads(payload.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"protocol payload is not an object: {parsed!r}")
    return parsed


class Requester(Protocol):
    """The one method the chat helpers need from a link.

    `BuddyLink` and `ResidentLink` both satisfy it, which is what lets
    the CLI and the MCP server share `say`.
    """

    def request(self, obj: Message, expect: str, timeout: float = 5.0) -> Message: ...


# ----- chat
#
# The device renders a transcript in a 232x88 px panel — five rows of a
# 16 px CJK font. Text arrives here as whatever Claude wrote, which is
# prose with markdown in it, so it gets flattened and split before it
# goes on the wire.

# Roughly one panel's worth of text, which is the unit that matters:
# the device renders the *tail* of its transcript, so a message longer
# than the screen loses its opening before anyone can read it.
#
# Both numbers come from the metrics measured in device/buddy_chat.py
# and have to move with them. The panel picks its font from the content,
# and so does `_limit_for` below:
#
#   EFontJA24  27 px tall, 23 px/glyph -> 4 rows x  9 chars
#   DejaVu12   16 px tall, 12 px/glyph -> 6 rows x 17 chars
#
# Rounded down, because wrapping leaves a ragged right edge and a part
# that overflows by one row is a part whose first line is already gone.
MAX_SAY_CHARS_WIDE = 32
MAX_SAY_CHARS = 88

# Keep in step with `_WIDE_FROM` in device/buddy_chat.py: the host has to
# predict which font the panel will choose, and it chooses on this.
_WIDE_FROM = 0x1100

# Seconds between parts of a split message. The panel shows only its
# last rows, so a burst would scroll past unread.
DEFAULT_PACE = 2.0

# Characters after which a split reads as a pause rather than a cut. The
# fullwidth forms are deliberate, not a paste accident: this text is
# mostly Japanese, where they are the sentence ends that actually occur.
_SENTENCE_ENDS = "。！？!?."  # noqa: RUF001


# Markdown that the panel has no way to render. Five rows of about
# fourteen Japanese characters is the whole budget, so every character
# spent on syntax is one the reader does not get: there is no bold to
# show, a `##` costs a fifth of a row, and a code block is unreadable at
# this size anyway. All of it is flattened away here.
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_HEADING = re.compile(r"^#{1,6}\s+")
_QUOTE = re.compile(r"^>\s?")
_BULLET = re.compile(r"^[-*+]\s+")
_ORDERED = re.compile(r"^\d+[.)]\s+")
_RULE = re.compile(r"^(?:[-*_]\s*){3,}$")
_STRONG = re.compile(r"\*\*([^*]+)\*\*")
_EMPHASIS = re.compile(r"\*([^*\n]+)\*")
# Underscore emphasis, but only where both delimiters sit outside a
# word. Without the boundary guards this rewrites `buddy_chat` to
# `buddychat`, which is worse than leaving the markup in. The body is
# allowed to contain underscores — `_buddy_chat.py_` is emphasis around
# an identifier — which is why the match is non-greedy: it stops at the
# first `_` that is itself at a word boundary.
_STRONG_UNDER = re.compile(r"(?<![^\W_])__([^\n]+?)__(?![^\W_])")
_UNDERLINE = re.compile(r"(?<![^\W_])_([^\n]+?)_(?![^\W_])")
_CODE_SPAN = re.compile(r"`+")

_FENCE = "```"
_CODE_MARKER = "[code]"


def _strip_markup(line: str) -> str:
    """Reduce one line of markdown to something the panel can show.

    Structure that survives at this size is kept: a list is still a list
    (every bullet style collapses to a compact ``- ``), and link text
    outlives its URL. Everything that only exists to be styled — bold,
    emphasis, code spans, heading hashes, blockquote markers, horizontal
    rules — is dropped.
    """
    line = _QUOTE.sub("", line)
    if _RULE.match(line):
        return ""
    line = _HEADING.sub("", line)
    if _BULLET.match(line) or _ORDERED.match(line):
        line = "- " + _BULLET.sub("", _ORDERED.sub("", line))
    line = _LINK.sub(r"\1", line)
    line = _STRONG.sub(r"\1", line)
    line = _STRONG_UNDER.sub(r"\1", line)
    line = _EMPHASIS.sub(r"\1", line)
    line = _UNDERLINE.sub(r"\1", line)
    line = _CODE_SPAN.sub("", line)
    return line.strip()


def normalize_for_device(text: str) -> str:
    """Flatten `text` into what should appear on the panel.

    Blank lines are dropped rather than preserved: they cost a row out
    of five and separate nothing the reader cannot already see from the
    colour change between messages.

    Fenced code is replaced by a single `[code]` marker. The marker is
    emitted on the opening fence, so a block whose fence is never closed
    still announces itself rather than vanishing — but everything after
    an unclosed fence is treated as code and dropped, which is the same
    thing every other markdown reader does with it.
    """
    lines: list[str] = []
    in_fence = False
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.strip().startswith(_FENCE):
            in_fence = not in_fence
            if in_fence:
                lines.append(_CODE_MARKER)
            continue
        if in_fence:
            continue
        line = _strip_markup(" ".join(raw.split()))
        if line:
            lines.append(line)
    return "\n".join(lines)


def _split_paragraph(para: str, limit: int) -> list[str]:
    """Break one over-long paragraph at the latest decent boundary."""
    out: list[str] = []
    while len(para) > limit:
        window = para[:limit]
        cut = max(window.rfind(c) for c in _SENTENCE_ENDS) + 1
        if cut <= 0:
            # No sentence end in reach. A space is the next best seam;
            # Japanese has none, so fall through to a hard cut rather
            # than emitting a message the device would have to clip.
            cut = window.rfind(" ") + 1
        if cut <= 0:
            cut = limit
        out.append(para[:cut].strip())
        para = para[cut:].lstrip()
    if para:
        out.append(para)
    return out


def _limit_for(text: str) -> int:
    """How much of `text` fits on one panel.

    Mirrors the font choice in `device/buddy_chat.py`: one wide glyph
    anywhere in the transcript pulls the whole panel onto the 27 px CJK
    face, so a single Japanese character in an otherwise ASCII message
    costs two rows and eight characters per row.
    """
    for ch in text:
        if ord(ch) >= _WIDE_FROM:
            return MAX_SAY_CHARS_WIDE
    return MAX_SAY_CHARS


def split_for_device(text: str, limit: int | None = None) -> list[str]:
    """Break normalized `text` into messages, in order.

    Paragraphs are packed together while they fit so a short exchange
    stays one bubble on screen, and only a paragraph that cannot fit on
    its own gets cut mid-sentence. `limit` defaults to whatever the
    panel can hold for this text; pass one to override.
    """
    if limit is None:
        limit = _limit_for(text)
    if limit < 1:
        raise ValueError(f"limit must be positive, got {limit}")
    parts: list[str] = []
    current = ""
    for para in text.split("\n"):
        for piece in _split_paragraph(para, limit) or [""]:
            if not piece:
                continue
            candidate = piece if not current else current + "\n" + piece
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    parts.append(current)
                current = piece
    if current:
        parts.append(current)
    return parts


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
# The device has no usable WiFi credentials of its own: its NVS keys are
# empty and the only SSID in the bundle belongs to an event venue. So
# they come down the cable, which also means they are never written to
# flash.
#
# They go in before the app does. Measured on hardware: the device
# associates in well under a second from the REPL and cannot once the
# app is running — `connect` is accepted, the association never
# completes, and 15 s later the driver still says "connecting". The
# ESP-IDF heap has ~12 KB free in its largest region with nothing but
# the launcher loaded, and bringing a link up wants DRAM, so the radio
# goes up first and the app inherits it. `buddy_tts.connect_wifi`
# reports an association that is already up without touching the radio,
# which makes a later `net.config` a read-back rather than a reconnect.

# Names for the esp32 port's WLAN.status() codes. The number reaches a
# human through a log line and nothing else, and 201 and 202 are very
# different problems.
_WIFI_STATUS = {
    200: "beacon timeout",
    201: "no ap found",
    202: "wrong password",
    203: "assoc fail",
    204: "handshake timeout",
    1000: "idle",
    1001: "connecting",
    1010: "got ip",
}

# Between polls of isconnected(). The poll runs on the host, one raw-REPL
# round trip each, rather than as a loop inside a single long-running
# block: a block that blocks for half a minute has to be reconciled with
# the transport's own exec timeout, and there is nothing to reconcile if
# every call returns immediately.
_WIFI_POLL_S = 0.5

# Association attempts before the driver gives up. `connect()` on the
# esp32 port retries forever by default, and while it does `status()`
# reads STAT_CONNECTING regardless of the reason — so a deadline here
# would expire while the driver carried on, and nothing would say why.
# Three is what M5Stack's own WLAN STA example uses.
_WIFI_RECONNECTS = 3

# Associating from the REPL has been measured at well under a second.
# The budget is for a retry or two, not for a link that is coming up
# slowly.
_WIFI_TIMEOUT_S = 25.0


def _wifi_connect_source(ssid: str, psk: str) -> str:
    """The statements that start an association. Returns immediately.

    The credentials are embedded with `repr`, not concatenated: this is
    Python source about to be compiled on the device, and an apostrophe
    in a passphrase would close the literal and run the remainder as
    code.
    """
    return (
        "import network\n"
        "_w = network.WLAN(network.STA_IF)\n"
        "_w.active(True)\n"
        # End the launcher's boot-time attempt on the event SSID. Its
        # forever-retry keeps that attempt alive, and a second connect()
        # into it is refused with "Wifi Internal State Error".
        "try:\n"
        "    _w.disconnect()\n"
        "except Exception:\n"
        "    pass\n"
        f"_w.config(reconnects={_WIFI_RECONNECTS})\n"
        f"_w.connect({ssid!r}, {psk!r})\n"
    )


def join_wifi(repl: Repl, ssid: str, psk: str, timeout_s: float = _WIFI_TIMEOUT_S) -> Message:
    """Associate from the raw REPL and report what happened.

    Runs before the app starts; see the note above. Never returns the
    passphrase, which is the one value in this exchange that must not
    end up in a log line.
    """
    if not ssid:
        raise ValueError("no ssid")
    try:
        repl.exec(_wifi_connect_source(ssid, psk))
    except Exception as exc:
        # The device's own traceback, minus anything we sent it. `exec`
        # raises TransportExecError carrying only stderr, so the source
        # — and the passphrase in it — is not in the message.
        return {"ok": False, "err": f"connect refused: {exc}"}

    deadline = time.monotonic() + timeout_s
    while not repl.eval("_w.isconnected()"):
        if time.monotonic() >= deadline:
            break
        time.sleep(_WIFI_POLL_S)

    connected, ip, code = repl.eval("(_w.isconnected(), _w.ifconfig()[0], _w.status())")
    return {"ok": bool(connected), "ip": ip, "status": _WIFI_STATUS.get(code, str(code))}


# The device polls for an association for up to 15 s before answering.
_NET_TIMEOUT_S = 25.0


def net_config(link: Requester, ssid: str, psk: str, timeout: float = _NET_TIMEOUT_S) -> Message:
    """Point the device's radio at an access point.

    Idempotent on the device side: an association that is already up is
    reported as success without touching the radio.
    """
    if not ssid:
        raise ValueError("no ssid")
    ack = link.request(
        {"cmd": "net.config", "ssid": ssid, "psk": psk}, "net.config", timeout=timeout
    )
    if not ack.get("ok"):
        raise RuntimeError(f"device could not join {ssid!r}: {ack.get('err', ack)}")
    return ack


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

    A non-zero `stalls` in the result means the device ran out of audio
    while waiting on the network — the utterance will have gapped.
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


class LineDemux:
    """Split a byte stream into protocol messages and log lines.

    Classification happens only on complete lines, which is what makes
    a read that splits the sentinel itself safe.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[Item]:
        """Absorb a read and return whatever it completed.

        Returns a list of ``(kind, payload)`` where kind is "protocol"
        (payload is the JSON body) or "log" (payload is the raw line).
        """
        self._buf.extend(chunk)
        out: list[Item] = []
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(self._buf[:nl]).rstrip(b"\r")
            del self._buf[: nl + 1]
            if not line:
                continue
            if line.startswith(SENTINEL):
                body = line[len(SENTINEL) :]
                if body:
                    out.append(("protocol", body))
                continue
            out.append(("log", line))
        return out


# ----- launching the app
#
# The app takes the console over: once its transport is up it disables
# Ctrl-C and speaks the sentinel protocol on the same wire. So the launch
# happens through the REPL, and the port that the REPL was using is
# handed straight to the link rather than closed and reopened — the
# device says whatever it is going to say about a failed import in that
# gap, and reopening would miss it.

# Imported rather than exec'd: the launcher uses the same path, and
# compiling an 18 KB source string in one go is a poor fit for the
# ~65 KB of free heap this bundle leaves behind.
LAUNCH_SOURCE = (
    "import sys\n"
    "for _p in ('/flash', '/flash/apps'):\n"
    "    if _p not in sys.path: sys.path.insert(0, _p)\n"
    "import claude_buddy\n"
)

# Default read timeout for a link. Short: the readers poll `in_waiting`
# and a blocking read would stall them on a quiet device.
DEFAULT_READ_TIMEOUT = 0.05


def launch_app(
    port: str,
    baud: int = 115200,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
    wait: float = 180.0,
    on_wait: Callable[[], None] | None = None,
) -> SerialPort:
    """Import the Buddy app on the device. Returns the port to read it on.

    One-way: the app disables Ctrl-C once its transport is up, so a
    second call will not find a REPL to talk to. Getting there needs a
    BtnRST press, which is what `wait` is for.

    Hand the result to `BuddyLink.open(adopt=...)` or
    `ResidentLink.connect(adopt=...)`; whoever takes it owns it.
    """
    repl = connect_repl(port, baud, timeout=wait, on_wait=on_wait)
    return run_and_release(repl, LAUNCH_SOURCE, read_timeout)


class BuddyLink:
    """An open serial session with the device."""

    def __init__(
        self, port: str, baud: int = 115200, read_timeout: float = DEFAULT_READ_TIMEOUT
    ) -> None:
        self.port = port
        self.baud = baud
        self.read_timeout = read_timeout
        self._ser: SerialPort | None = None
        self._demux = LineDemux()
        self._msgs: list[Message] = []
        self._logs: list[bytes] = []
        # Set when the port goes away mid-session (device reset).
        self.dropped = False

    # ----- lifecycle

    @property
    def _io(self) -> SerialPort:
        """The open port, or a readable error instead of an AttributeError."""
        if self._ser is None:
            raise RuntimeError("link is not open; use `with BuddyLink(port)` or call open()")
        return self._ser

    def open(self, adopt: SerialPort | None = None) -> BuddyLink:
        """Open the port, or take over one `launch_app` already opened.

        Idempotent, so `with link:` after an adopting open does not throw
        the adopted port away and open a second one.
        """
        if self._ser is None:
            self._ser = (
                adopt
                if adopt is not None
                else serial.Serial(self.port, self.baud, timeout=self.read_timeout)
            )
        return self

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def __enter__(self) -> BuddyLink:
        return self.open()

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ----- traffic

    def send(self, obj: Message) -> None:
        self._io.write(encode(obj))
        self._io.flush()

    def _read_available(self) -> None:
        # The device resets itself on app exit (claude_buddy.py's finally
        # block) and re-enumerates, which surfaces here as ENXIO. That is
        # ordinary lifecycle, not an error worth unwinding the stack for
        # — losing the buffered logs at that moment would throw away the
        # traceback that explains an unexpected reset.
        try:
            waiting = self._io.in_waiting
            data = self._io.read(waiting if waiting else 1)
        except OSError:
            self.dropped = True
            return
        if not data:
            return
        for kind, payload in self._demux.feed(data):
            if kind == "protocol":
                try:
                    self._msgs.append(decode(payload))
                except ValueError:
                    self._logs.append(b"<undecodable protocol line> " + payload)
            else:
                self._logs.append(payload)

    def pump(self, duration: float = 0.0) -> tuple[list[Message], list[bytes]]:
        """Read for `duration` seconds, then hand back what arrived."""
        deadline = time.monotonic() + duration
        while True:
            self._read_available()
            if time.monotonic() >= deadline:
                break
        return self.drain()

    def drain(self) -> tuple[list[Message], list[bytes]]:
        msgs, logs = self._msgs, self._logs
        self._msgs, self._logs = [], []
        return msgs, logs

    def await_ack(self, expect: str, timeout: float = 5.0) -> Message:
        """Wait for the first reply whose `ack` is `expect`.

        Unrelated traffic that arrives meanwhile — the `hello` the device
        emits on handshake, for instance — stays queued for `drain()`.
        """
        deadline = time.monotonic() + timeout
        while True:
            for i, msg in enumerate(self._msgs):
                if msg.get("ack") == expect:
                    return self._msgs.pop(i)
            if self.dropped:
                raise ConnectionError(f"device dropped off USB while waiting for {expect!r}")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"no {expect!r} ack within {timeout:.1f}s")
            self._read_available()

    def request(self, obj: Message, expect: str, timeout: float = 5.0) -> Message:
        """Send `obj` and return the first reply whose `ack` is `expect`."""
        self.send(obj)
        return self.await_ack(expect, timeout)


class ResidentLink:
    """A session whose reads run on a background thread.

    `BuddyLink` only reads while a caller is waiting, which is fine for a
    one-shot CLI run. An MCP server outlives any single tool call, so
    device-initiated traffic — the `hello` on handshake, and anything the
    device pushes later — has to be captured in between. This class owns
    the port for the life of the server and buffers what arrives.

    Writes are serialised with a lock; reads happen only on the reader
    thread, so the two never race on the same file descriptor.
    """

    def __init__(
        self,
        port: str,
        baud: int = 115200,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        log_history: int = 500,
        serial_factory: SerialFactory | None = None,
    ) -> None:
        self.port = port
        self.baud = baud
        self.read_timeout = read_timeout
        # Logs are unbounded chatter; drop the oldest rather than grow
        # without limit across a long-lived server. Protocol messages are
        # kept in full — losing an ack would be a correctness bug.
        self._logs: deque[bytes] = deque(maxlen=log_history)
        self._msgs: deque[Message] = deque()
        self._demux = LineDemux()
        self._cv = threading.Condition()
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None
        self._ser: SerialPort | None = None
        self._serial_factory = serial_factory
        self.dropped = False

    # ----- lifecycle

    @property
    def connected(self) -> bool:
        return self._ser is not None

    @property
    def _io(self) -> SerialPort:
        if self._ser is None:
            raise RuntimeError("link is not connected; call connect() first")
        return self._ser

    def connect(self, adopt: SerialPort | None = None) -> None:
        """Open the port and start reading, or take over an open one.

        `adopt` is how a launch is picked up: `launch_app` leaves the
        REPL's port open precisely so the reader can start on it without
        a gap.
        """
        if self._ser is not None:
            return
        if adopt is not None:
            ser = adopt
        else:
            factory: SerialFactory = self._serial_factory or serial.Serial
            ser = factory(self.port, self.baud, timeout=self.read_timeout)
        self._ser = ser
        self.dropped = False
        self._stop.clear()
        # The port is handed to the thread rather than read off `self`:
        # disconnect() clears the attribute after a bounded join, so a
        # reader still blocked in read() would otherwise wake up to None.
        self._reader = threading.Thread(
            target=self._read_loop, args=(ser,), name="buddy-reader", daemon=True
        )
        self._reader.start()

    def disconnect(self) -> None:
        self._stop.set()
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.join(timeout=2.0)
        ser, self._ser = self._ser, None
        if ser is not None:
            # Closing a port that already went away is not news.
            with contextlib.suppress(Exception):
                ser.close()

    # ----- reader thread

    def _read_loop(self, ser: SerialPort) -> None:
        while not self._stop.is_set():
            try:
                waiting = ser.in_waiting
                data = ser.read(waiting if waiting else 1)
            except OSError:
                # SerialException subclasses OSError, so this covers both
                # a closed port and the ENXIO of a device that reset.
                with self._cv:
                    self.dropped = True
                    self._cv.notify_all()
                return
            if not data:
                continue
            items = self._demux.feed(data)
            if not items:
                continue
            with self._cv:
                for kind, payload in items:
                    if kind == "protocol":
                        try:
                            self._msgs.append(decode(payload))
                        except ValueError:
                            self._logs.append(b"<undecodable protocol line> " + payload)
                    else:
                        self._logs.append(payload)
                self._cv.notify_all()

    # ----- traffic

    def send(self, obj: Message) -> None:
        with self._write_lock:
            self._io.write(encode(obj))
            self._io.flush()

    def await_ack(self, expect: str, timeout: float = 5.0) -> Message:
        """Wait for the reader thread to surface a matching ack."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                for i, msg in enumerate(self._msgs):
                    if msg.get("ack") == expect:
                        del self._msgs[i]
                        return msg
                if self.dropped:
                    raise ConnectionError(f"device dropped off USB while waiting for {expect!r}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"no {expect!r} ack within {timeout:.1f}s")
                self._cv.wait(remaining)

    def request(self, obj: Message, expect: str, timeout: float = 5.0) -> Message:
        """Send `obj` and wait for the reader to surface a matching ack."""
        self.send(obj)
        return self.await_ack(expect, timeout)

    def events(self) -> tuple[list[Message], list[bytes]]:
        """Drain everything buffered since the last call."""
        with self._cv:
            msgs = list(self._msgs)
            logs = list(self._logs)
            self._msgs.clear()
            self._logs.clear()
        return msgs, logs

    def start_app(self, settle: float = 8.0, wait: float = 15.0) -> None:
        """Relaunch the app and come back reading the same port.

        The port cannot be in two places at once: the REPL needs it to
        run the import, so the reader is stopped first and restarted on
        the port the launch hands back. Nothing is drained here — the
        reader collects the startup output, including a traceback from a
        failed import, for `events()`.

        `wait` is short on purpose. Getting to the REPL needs a BtnRST
        press, and a tool call that blocks for three minutes waiting for
        one is worse than one that says so.
        """
        self.disconnect()
        ser = launch_app(self.port, self.baud, self.read_timeout, wait=wait)
        self.connect(adopt=ser)
        time.sleep(settle)


def _dump(msgs: list[Message], logs: list[bytes]) -> None:
    for line in logs:
        print("  log |", line.decode("utf-8", errors="replace"))
    for msg in msgs:
        print("  <-- ", json.dumps(msg, ensure_ascii=False))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("--port", required=True)
    ap.add_argument("--start", action="store_true", help="Launch the app over the REPL first.")
    ap.add_argument("--status", action="store_true", help="Request a status ack.")
    ap.add_argument("--name", help="Set the device name.")
    ap.add_argument("--owner", help="Set the owner string.")
    ap.add_argument(
        "--say",
        action="append",
        default=[],
        metavar="TEXT",
        help="Put TEXT on the device's chat panel. Repeatable.",
    )
    ap.add_argument("--role", default="claude", choices=("claude", "user", "sys"))
    ap.add_argument(
        "--pace",
        type=float,
        default=DEFAULT_PACE,
        help="Seconds between the parts of a split --say. 0 sends flat out.",
    )
    ap.add_argument("--chat-clear", action="store_true", help="Wipe the chat panel.")
    ap.add_argument("--chat-info", action="store_true", help="Report the panel's font/geometry.")
    ap.add_argument(
        "--speak",
        action="append",
        default=[],
        metavar="TEXT",
        help="Have the device fetch TEXT from VOICEVOX and play it. Repeatable.",
    )
    ap.add_argument(
        "--wifi",
        metavar="SSID",
        help=(
            "Join this network from the REPL, before --start. Password from "
            "$BUDDY_WIFI_PSK. Needs the device at the REPL, so BtnRST first."
        ),
    )
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument(
        "--wait",
        type=float,
        default=180.0,
        help="Seconds to wait for the REPL when --wifi needs it. Needs a BtnRST press.",
    )
    ap.add_argument(
        "--engine",
        metavar="URL",
        help="VOICEVOX engine. Defaults to $VOICEVOX_URL, then this machine's LAN address.",
    )
    ap.add_argument(
        "--speaker",
        type=int,
        default=ZUNDAMON,
        help=f"VOICEVOX style id. {ZUNDAMON} is Zundamon (normal).",
    )
    ap.add_argument("--rate", type=int, default=DEFAULT_RATE)
    ap.add_argument(
        "--no-show",
        action="store_true",
        help="Do not also put spoken text on the chat panel.",
    )
    ap.add_argument("--watch", type=float, default=0.0, help="Read traffic for N seconds and exit.")
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--settle", type=float, default=4.0, help="Seconds to wait after --start.")
    args = ap.parse_args()

    def nudge_repl() -> None:
        print("waiting for the REPL — press BtnRST on the device...")

    if args.wifi:
        # Before the app and before the link: see the note above
        # `join_wifi`. The REPL and the running app cannot both own the
        # port, so this finishes and closes before BuddyLink opens.
        try:
            repl = connect_repl(args.port, args.baud, timeout=args.wait, on_wait=nudge_repl)
        except ReplError as e:
            sys.stderr.write(f"{e}\n")
            return 1
        try:
            result = join_wifi(repl, args.wifi, os.environ.get("BUDDY_WIFI_PSK", ""))
        except OSError as e:
            # The port vanished mid-exchange: the board reset and
            # re-enumerated. Bringing the radio up is the current spike
            # of this whole exchange — a few hundred mA on transmit —
            # and this board browns out under it when the battery is
            # low. Worth saying, because the bare errno reads like a
            # cable fault.
            sys.stderr.write(
                f"device dropped off USB while joining WiFi ({e}). Powering the "
                "radio draws a few hundred mA and this board browns out on it "
                "when the battery is low; leave it on USB for a while and retry.\n"
            )
            return 1
        finally:
            # Back to the friendly REPL before letting go: --start
            # interrupts and pastes into it, and a port left in raw mode
            # would swallow that silently.
            with contextlib.suppress(Exception):
                repl.exit_raw_repl()
            repl.close()
        print("wifi:", json.dumps({"ssid": args.wifi, **result}, ensure_ascii=False))
        if not result.get("ok"):
            sys.stderr.write(f"could not join {args.wifi!r}\n")
            return 1

    link = BuddyLink(args.port, baud=args.baud)
    if args.start:
        print("starting app over REPL...")
        try:
            link.open(
                adopt=launch_app(
                    args.port, args.baud, link.read_timeout, wait=args.wait, on_wait=nudge_repl
                )
            )
        except ReplError as e:
            sys.stderr.write(f"{e}\n")
            return 1
    else:
        link.open()

    with link:
        # Whatever happens, print what the device said first. On a failed
        # launch the buffered logs are the diagnostic.
        try:
            if args.start:
                # The reader is already on the port the launch handed
                # over, so this is just letting the device talk.
                link.pump(args.settle)
                _dump(*link.drain())

            if args.status:
                ack = link.request({"cmd": "status"}, "status", timeout=args.timeout)
                print("status:", json.dumps(ack, ensure_ascii=False))

            if args.name is not None:
                ack = link.request({"cmd": "name", "name": args.name}, "name", timeout=args.timeout)
                print("name:", json.dumps(ack, ensure_ascii=False))

            if args.owner is not None:
                ack = link.request(
                    {"cmd": "owner", "owner": args.owner}, "owner", timeout=args.timeout
                )
                print("owner:", json.dumps(ack, ensure_ascii=False))

            if args.chat_info:
                ack = link.request({"cmd": "chat.info"}, "chat.info", timeout=args.timeout)
                print("chat.info:", json.dumps(ack, ensure_ascii=False))

            if args.chat_clear:
                ack = link.request({"cmd": "chat.clear"}, "chat.clear", timeout=args.timeout)
                print("chat.clear:", json.dumps(ack, ensure_ascii=False))

            for text in args.say:
                for ack in say(link, text, role=args.role, timeout=args.timeout, pace=args.pace):
                    print("chat.say:", json.dumps(ack, ensure_ascii=False))

            engine: str | None = None
            if args.speak:
                # Resolved once, and before the first request, so a bad
                # engine address is reported here rather than after the
                # device has already been told to say something.
                engine = voicevox_url(args.engine)
                print(f"engine: {engine}")

            for text in args.speak:
                if not args.no_show:
                    # Sent first so the words are on screen while the
                    # engine synthesises, not after playback has ended.
                    for ack in say(link, text, timeout=args.timeout, pace=0):
                        print("chat.say:", json.dumps(ack, ensure_ascii=False))
                ack = speak(
                    link,
                    text,
                    url=engine,
                    speaker=args.speaker,
                    rate=args.rate,
                    timeout=args.timeout,
                )
                print("speak.end:", json.dumps(ack, ensure_ascii=False))

            if args.watch:
                print(f"watching for {args.watch:.1f}s...")
                link.pump(args.watch)
        finally:
            _dump(*link.drain())
            if link.dropped:
                print("  !! device dropped off USB (reset)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
