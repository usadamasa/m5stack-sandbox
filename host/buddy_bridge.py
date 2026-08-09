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
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Protocol

import serial

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

    def request(self, obj: Message, expect: str, timeout: float = 5.0) -> Message:
        """Send `obj` and return the first reply whose `ack` is `expect`.

        Unrelated traffic that arrives meanwhile — the `hello` the device
        emits on handshake, for instance — stays queued for `drain()`.
        """
        self.send(obj)
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

    def request(self, obj: Message, expect: str, timeout: float = 5.0) -> Message:
        """Send `obj` and wait for the reader to surface a matching ack."""
        self.send(obj)
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
