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
import re
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Protocol

import serial

import buddy_speech

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

    def reset_input_buffer(self) -> None: ...


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


class BulkLink(Requester, Protocol):
    """A link that can also push an unframed payload and wait for a reply.

    Audio does not travel as JSON: the device declares a length, drops
    into bulk mode, and reads raw bytes. That needs two things a request
    cannot express — a write with no framing, and a wait for an ack that
    arrives long after the command that caused it.
    """

    def write_raw(self, data: bytes) -> None: ...

    def await_ack(self, expect: str, timeout: float = 5.0) -> Message: ...


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


# ----- speech
#
# PCM does not travel as JSON. The device declares a length, switches
# its transport into bulk mode, and reads raw bytes; see the bulk-mode
# note in device/buddy_serial.py for why the length has to come first.

# Bytes the device reads in one go. Must be even — a 16-bit sample may
# not straddle two blocks — and must match `block` in speak.begin. 2048
# is 64 ms of 16 kHz audio, read in about 11 ms, which leaves the
# device's 40 ms tick comfortably ahead of playback.
BLOCK_BYTES = 2048


def pad_to_blocks(pcm: bytes, block: int = BLOCK_BYTES) -> bytes:
    """Round `pcm` up to a whole number of blocks with silence.

    Not cosmetic. The device reads fixed-size blocks with a call that
    blocks until the block is full, so a short tail does not truncate
    the sound — it parks the device inside a read waiting for bytes that
    are never coming, with Ctrl-C disabled. The cost is up to 64 ms of
    silence at the end.
    """
    if block <= 0 or block % 2:
        raise ValueError(f"block must be a positive even number, got {block}")
    remainder = len(pcm) % block
    if not remainder:
        return pcm
    return pcm + b"\x00" * (block - remainder)


def speak(
    link: BulkLink,
    pcm: bytes,
    rate: int = 16000,
    block: int = BLOCK_BYTES,
    timeout: float = 10.0,
) -> Message:
    """Stream `pcm` to the device's speaker and wait for it to finish.

    `pcm` is signed 16-bit little-endian mono at `rate` — what
    `buddy_speech.synthesize` returns. Blocks for roughly the duration
    of the audio: the device's speaker queue holds about a second, so
    the write paces itself against playback rather than the link.
    """
    if not pcm:
        raise ValueError("nothing to play")
    payload = pad_to_blocks(pcm, block)
    blocks = len(payload) // block

    ack = link.request(
        {"cmd": "speak.begin", "rate": rate, "block": block, "blocks": blocks},
        "speak.begin",
        timeout=timeout,
    )
    if not ack.get("ok"):
        raise RuntimeError(f"device refused speak.begin: {ack.get('err', ack)}")

    # Only after the ack: the ack is what put the device into bulk mode,
    # and bytes sent before it would be parsed as a line and dropped.
    link.write_raw(payload)

    playback_s = len(payload) / 2 / rate
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


class BuddyLink:
    """An open serial session with the device."""

    # Imported rather than exec'd: the launcher uses the same path, and
    # compiling an 18 KB source string in one go is a poor fit for the
    # ~65 KB of free heap this bundle leaves behind.
    _LAUNCH = (
        "import sys\n"
        "for _p in ('/flash', '/flash/apps'):\n"
        "    if _p not in sys.path: sys.path.insert(0, _p)\n"
        "import claude_buddy\n"
    )

    def __init__(self, port: str, baud: int = 115200, read_timeout: float = 0.05) -> None:
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

    def open(self) -> BuddyLink:
        self._ser = serial.Serial(self.port, self.baud, timeout=self.read_timeout)
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

    # ----- app launch

    def start_app(self, settle: float = 4.0) -> None:
        """Interrupt to the REPL and import the Buddy app.

        One-way: the app disables Ctrl-C once its transport is up, so a
        second call will not find a REPL to talk to.
        """
        s = self._io
        for _ in range(5):
            s.write(b"\x03")
            time.sleep(0.05)
        s.write(b"\r\n")
        time.sleep(0.3)
        s.reset_input_buffer()

        s.write(b"\x05")  # paste mode: no auto-indent mangling
        time.sleep(0.1)
        for line in self._LAUNCH.splitlines():
            s.write(line.encode("utf-8") + b"\r\n")
            time.sleep(0.005)
        s.write(b"\x04")
        # See ResidentLink.start_app: the paste-mode terminator needs a
        # newline behind it or it corrupts the next frame.
        s.write(b"\r\n")

        # Paste mode echoes what we just sent, so some of what follows is
        # our own input coming back. Keep it anyway: a startup traceback
        # arrives on this same channel, and discarding the echo would
        # discard the one diagnostic that explains a failed launch.
        time.sleep(settle)
        self._read_available()

    # ----- traffic

    def send(self, obj: Message) -> None:
        self._io.write(encode(obj))
        self._io.flush()

    def write_raw(self, data: bytes) -> None:
        """Push unframed bytes. Only valid while the device is in bulk mode."""
        self._io.write(data)
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
        read_timeout: float = 0.05,
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

    def connect(self) -> None:
        if self._ser is not None:
            return
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

    def write_raw(self, data: bytes) -> None:
        """Push unframed bytes. Only valid while the device is in bulk mode.

        Held under the write lock for the whole payload: a JSON command
        interleaved into an audio stream would be consumed as samples,
        and the transfer would end up as many bytes short as the command
        was long.
        """
        with self._write_lock:
            self._io.write(data)
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

    def start_app(self, settle: float = 8.0) -> None:
        """Interrupt to the REPL and import the Buddy app.

        Unlike BuddyLink.start_app there is no explicit drain: the reader
        thread is already collecting, and the paste-mode echo plus any
        startup traceback land in the log buffer for `events()`.
        """
        io = self._io
        with self._write_lock:
            for _ in range(5):
                io.write(b"\x03")
                time.sleep(0.05)
            io.write(b"\r\n")
            time.sleep(0.3)
            io.write(b"\x05")
            time.sleep(0.1)
            for line in BuddyLink._LAUNCH.splitlines():
                io.write(line.encode("utf-8") + b"\r\n")
                time.sleep(0.005)
            io.write(b"\x04")
            # Terminate the line. 0x04 carries no newline, so without
            # this it sits unconsumed in the device's rx buffer and gets
            # prepended to the next frame — whose sentinel is then not at
            # the start of the line, so the transport drops it. The
            # symptom is a launch that looks fine followed by exactly one
            # timed-out request.
            io.write(b"\r\n")
            io.flush()
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
        help="Synthesize TEXT on this machine and play it on the device. Repeatable.",
    )
    ap.add_argument("--voice", default=buddy_speech.DEFAULT_VOICE)
    ap.add_argument("--rate", type=int, default=buddy_speech.DEFAULT_RATE)
    ap.add_argument(
        "--no-show",
        action="store_true",
        help="Do not also put spoken text on the chat panel.",
    )
    ap.add_argument("--watch", type=float, default=0.0, help="Read traffic for N seconds and exit.")
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--settle", type=float, default=4.0, help="Seconds to wait after --start.")
    args = ap.parse_args()

    with BuddyLink(args.port) as link:
        # Whatever happens, print what the device said first. On a failed
        # launch the buffered logs are the diagnostic.
        try:
            if args.start:
                print("starting app over REPL...")
                link.start_app(settle=args.settle)
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

            for text in args.speak:
                pcm = buddy_speech.synthesize(text, voice=args.voice, rate=args.rate)
                print(f"speaking {buddy_speech.duration_s(pcm, args.rate):.1f}s...")
                if not args.no_show:
                    # Sent first so the words are on screen before the
                    # audio starts, not after it has finished.
                    for ack in say(link, text, timeout=args.timeout, pace=0):
                        print("chat.say:", json.dumps(ack, ensure_ascii=False))
                ack = speak(link, pcm, rate=args.rate, timeout=args.timeout)
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
