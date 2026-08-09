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
import json
import sys
import time

try:
    import serial
except ImportError:  # pragma: no cover - exercised only without pyserial
    serial = None

# Keep in sync with _SENTINEL in device/buddy_serial.py.
SENTINEL = b"\x1eBUDDY1 "

# Matches buddy_protocol._send, which uses the same separators. Keeping
# the encodings identical means a byte-level diff of a capture is
# meaningful in both directions.
_JSON_SEPARATORS = (",", ":")


def encode(obj: dict) -> bytes:
    """Frame one message for the device."""
    body = json.dumps(obj, separators=_JSON_SEPARATORS).encode("utf-8")
    return SENTINEL + body + b"\n"


def decode(payload: bytes) -> dict:
    """Parse one protocol payload (sentinel already stripped)."""
    return json.loads(payload.decode("utf-8"))


class LineDemux:
    """Split a byte stream into protocol messages and log lines.

    Classification happens only on complete lines, which is what makes
    a read that splits the sentinel itself safe.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[tuple[str, bytes]]:
        """Absorb a read and return whatever it completed.

        Returns a list of ``(kind, payload)`` where kind is "protocol"
        (payload is the JSON body) or "log" (payload is the raw line).
        """
        self._buf.extend(chunk)
        out: list[tuple[str, bytes]] = []
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

    def __init__(self, port: str, baud: int = 115200, read_timeout: float = 0.05):
        self.port = port
        self.baud = baud
        self.read_timeout = read_timeout
        self._ser = None
        self._demux = LineDemux()
        self._msgs: list[dict] = []
        self._logs: list[bytes] = []
        # Set when the port goes away mid-session (device reset).
        self.dropped = False

    # ----- lifecycle

    def open(self) -> "BuddyLink":
        if serial is None:
            raise RuntimeError("pyserial is required; install it in the venv")
        self._ser = serial.Serial(self.port, self.baud, timeout=self.read_timeout)
        return self

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def __enter__(self) -> "BuddyLink":
        return self.open()

    def __exit__(self, *_exc) -> None:
        self.close()

    # ----- app launch

    def start_app(self, settle: float = 4.0) -> None:
        """Interrupt to the REPL and import the Buddy app.

        One-way: the app disables Ctrl-C once its transport is up, so a
        second call will not find a REPL to talk to.
        """
        s = self._ser
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

        # Paste mode echoes what we just sent, so some of what follows is
        # our own input coming back. Keep it anyway: a startup traceback
        # arrives on this same channel, and discarding the echo would
        # discard the one diagnostic that explains a failed launch.
        time.sleep(settle)
        self._read_available()

    # ----- traffic

    def send(self, obj: dict) -> None:
        self._ser.write(encode(obj))
        self._ser.flush()

    def _read_available(self) -> None:
        # The device resets itself on app exit (claude_buddy.py's finally
        # block) and re-enumerates, which surfaces here as ENXIO. That is
        # ordinary lifecycle, not an error worth unwinding the stack for
        # — losing the buffered logs at that moment would throw away the
        # traceback that explains an unexpected reset.
        try:
            waiting = self._ser.in_waiting
            data = self._ser.read(waiting if waiting else 1)
        except (OSError, serial.SerialException):
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

    def pump(self, duration: float = 0.0) -> tuple[list[dict], list[bytes]]:
        """Read for `duration` seconds, then hand back what arrived."""
        deadline = time.monotonic() + duration
        while True:
            self._read_available()
            if time.monotonic() >= deadline:
                break
        return self.drain()

    def drain(self) -> tuple[list[dict], list[bytes]]:
        msgs, logs = self._msgs, self._logs
        self._msgs, self._logs = [], []
        return msgs, logs

    def request(self, obj: dict, expect: str, timeout: float = 5.0) -> dict:
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
                raise ConnectionError(
                    "device dropped off USB while waiting for {!r}".format(expect)
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "no {!r} ack within {:.1f}s".format(expect, timeout)
                )
            self._read_available()


def _dump(msgs: list[dict], logs: list[bytes]) -> None:
    for line in logs:
        print("  log |", line.decode("utf-8", errors="replace"))
    for msg in msgs:
        print("  <-- ", json.dumps(msg, ensure_ascii=False))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
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
                ack = link.request(
                    {"cmd": "name", "name": args.name}, "name", timeout=args.timeout
                )
                print("name:", json.dumps(ack, ensure_ascii=False))

            if args.owner is not None:
                ack = link.request(
                    {"cmd": "owner", "owner": args.owner}, "owner", timeout=args.timeout
                )
                print("owner:", json.dumps(ack, ensure_ascii=False))

            if args.watch:
                print("watching for {:.1f}s...".format(args.watch))
                link.pump(args.watch)
        finally:
            _dump(*link.drain())
            if link.dropped:
                print("  !! device dropped off USB (reset)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
