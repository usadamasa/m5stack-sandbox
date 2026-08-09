"""USB-serial transport for Claude Buddy.

Duck-types the subset of ``buddy_ble.BuddyBLE`` that
``buddy_protocol.BuddyProtocol`` and ``apps/claude_buddy.py`` actually
touch, so the protocol, UI, state and chars layers run unchanged:

    send_line / disconnect / forget_bonds / deinit
    pairing_supported / advertised_name / encrypted

The one addition is ``poll()``. BLE delivers inbound data from an IRQ;
a serial line has no such callback, so the app's main loop has to pump
us. ``BuddyBLE.poll()`` exists as a no-op so the call site stays
transport-agnostic.

### Framing

The device's stdout is shared with ``print()`` debug logging — this
bundle is chatty, and a host that treated every line as protocol would
choke on the first log message. Protocol lines are therefore prefixed
with a sentinel (``_SENTINEL``) that plain logging never emits: 0x1E
(ASCII Record Separator) followed by a version tag. The host filters on
the prefix and passes everything else through as log output.

Inbound uses the same framing, but is matched leniently: the sentinel
may appear anywhere on the line, not just at the start. A fragment left
unterminated in the rx buffer — the bare 0x04 that ends a paste-mode
launch, a REPL echo, half a line from before a reset — otherwise lands
in front of the next frame and would cost us a message. A line with no
sentinel at all is a human at the REPL or leftover noise, and is
dropped silently.

### Annotations

MicroPython parses function annotations and throws them away, so they
are free here — but only as long as every name in one is a builtin.
There is no `typing` and no `__future__` on the device, and reaching for
either turns into an ImportError at launch. `host/tests/
test_device_constraints.py` enforces that mechanically.

### Ctrl-C

Host and device share one USB CDC channel, and MicroPython treats an
inbound 0x03 on it as ``KeyboardInterrupt``. A JSON payload containing
that byte would kill the app mid-loop, so ``__init__`` disables the
interrupt with ``micropython.kbd_intr(-1)`` and ``deinit`` restores it.
While a session is up there is no Ctrl-C escape hatch — recovery is
BtnRST.
"""

import select
import sys
import time

try:
    import micropython
except ImportError:  # pragma: no cover - host-side import for inspection
    micropython = None

# 0x1E = ASCII Record Separator. Never produced by print()-style logging,
# survives a text-oriented channel, and still greppable in a raw capture.
_SENTINEL = b"\x1eBUDDY1 "

# Cap the bytes drained per poll() call. The app's loop runs every 40 ms
# and repaints the LCD from the same thread; an unbounded drain would let
# a chatty host starve the UI. Anything left over is picked up next tick.
_MAX_DRAIN = 512

# Longest line we will buffer before assuming the host is desynchronised
# and resetting. Status acks are well under 512 B; 4 KiB is generous
# without letting a runaway peer exhaust the heap.
_MAX_LINE = 4096


def _binary_streams() -> tuple:
    """Return (stdin_reader, stdout_writer) as byte streams.

    MicroPython exposes ``.buffer`` on the std streams for the esp32
    port, but the attribute is absent on some builds. Fall back to the
    text stream and let the encode/decode happen at the edges.
    """
    stdin = getattr(sys.stdin, "buffer", sys.stdin)
    stdout = getattr(sys.stdout, "buffer", sys.stdout)
    return stdin, stdout


class BuddySerial:
    """Nordic-UART-shaped protocol over the USB CDC console."""

    # No BLE pairing layer exists here at all. Reporting False makes
    # apps/claude_buddy.py remap the "connected" state event to
    # "encrypted", which is the path that drives send_hello() — the
    # same branch the stripped UIFlow 2.0 BLE build takes.
    pairing_supported = False

    def __init__(self, name_prefix="Claude", on_line=None, on_passkey=None, on_state=None) -> None:
        self._on_line = on_line or (lambda _line: None)
        self._on_passkey = on_passkey or (lambda _pk: None)
        self._on_state = on_state or (lambda _st: None)

        self._name = name_prefix + "_serial"
        self._rx_buf = bytearray()
        self._shutting_down = False
        self._host_seen = False
        self._last_rx_ms = 0

        self._stdin, self._stdout = _binary_streams()
        self._poller = select.poll()
        self._poller.register(self._stdin, select.POLLIN)

        # Must happen before the first inbound byte can arrive.
        if micropython is not None:
            micropython.kbd_intr(-1)

        print("buddy_serial: up as", self._name)

    # ----- transport surface

    @property
    def advertised_name(self) -> str:
        return self._name

    @property
    def connected(self) -> bool:
        return self._host_seen

    # Forwarded verbatim into the status ack as `sec`
    # (buddy_protocol.py:258), where it tells the host whether the link
    # is cryptographically protected — not whether it is hard to reach.
    #
    # A USB CDC console is plaintext on the wire, and the device cannot
    # authenticate whatever sits on the other end of the cable. So the
    # honest answer is False, the same answer the unauthenticated
    # UIFlow 2.0 BLE build gives.
    #
    # Requiring physical access is a real barrier, but it is a different
    # property than the one this field names. Reporting True would tell
    # a host it may relax exactly the behaviour `sec` exists to gate.
    encrypted = False

    def send_line(self, payload) -> bool:
        """Push one JSON line to the host. Returns False if no session."""
        if self._shutting_down or not self._host_seen:
            return False
        if not isinstance(payload, (bytes, bytearray)):
            payload = payload.encode("utf-8")
        if payload.endswith(b"\n"):
            payload = payload[:-1]
        try:
            self._write(_SENTINEL + payload + b"\n")
        except OSError as e:
            print("buddy_serial: write failed:", e)
            return False
        return True

    def disconnect(self) -> None:
        """Drop the logical session. The cable stays up; the host has to
        re-handshake before we will emit anything again."""
        if self._host_seen:
            self._host_seen = False
            self._emit_state("disconnected")

    def forget_bonds(self) -> None:
        # No bonding store on a wire. Present for parity with BuddyBLE so
        # buddy_protocol's unpair path (line 235) does not need a guard.
        pass

    def deinit(self) -> None:
        self._shutting_down = True
        try:
            self._poller.unregister(self._stdin)
        except (OSError, KeyError):
            pass
        if micropython is not None:
            # Restore the Ctrl-C escape hatch for whatever runs next.
            micropython.kbd_intr(3)
        self._on_line = lambda _line: None
        self._on_state = lambda _st: None
        print("buddy_serial: down")

    # ----- inbound pump

    def poll(self) -> None:
        """Drain pending stdin bytes and dispatch complete lines.

        Called from the app's main loop. Callbacks fire synchronously in
        loop context — unlike the BLE path there is no scheduler hop, so
        the UI-safety dance in claude_buddy.py is unnecessary here (but
        harmless, and we keep the call sites identical).
        """
        if self._shutting_down:
            return
        drained = 0
        while drained < _MAX_DRAIN and self._poller.poll(0):
            chunk = self._stdin.read(1)
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            drained += len(chunk)
            self._rx_buf.extend(chunk)

        while True:
            nl = self._rx_buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(self._rx_buf[:nl])
            # MicroPython's bytearray has no slice deletion (`del b[:n]`
            # raises TypeError), so rebind to the tail instead. Messages
            # are small and arrive a few per second at most; the copy is
            # not worth an index-offset scheme.
            self._rx_buf = self._rx_buf[nl + 1 :]
            self._handle_line(line)

        if len(self._rx_buf) > _MAX_LINE:
            print("buddy_serial: rx overflow, resyncing")
            self._rx_buf = bytearray()

    # ----- internals

    def _handle_line(self, line: bytes) -> None:
        line = line.rstrip(b"\r")
        # Search rather than match the prefix: an unterminated fragment
        # left in the rx buffer — a paste-mode 0x04, a REPL echo, half a
        # line from before a reset — ends up in front of the sentinel and
        # would otherwise make us drop a perfectly good frame. 0x1E is
        # not something print()-style logging emits, so finding the
        # sentinel anywhere on the line is still unambiguous.
        idx = line.find(_SENTINEL)
        if idx < 0:
            # REPL noise or a stray log echo. Not ours.
            return
        payload = line[idx + len(_SENTINEL) :]
        if not payload:
            return
        self._last_rx_ms = time.ticks_ms()
        if not self._host_seen:
            self._host_seen = True
            self._emit_state("connected")
        self._on_line(payload)

    def _emit_state(self, state: str) -> None:
        try:
            self._on_state(state)
        except Exception as e:
            print("buddy_serial: on_state error:", e)

    def _write(self, raw: bytes) -> None:
        try:
            self._stdout.write(raw)
        except TypeError:
            # Text-mode fallback when .buffer was unavailable.
            self._stdout.write(raw.decode("utf-8"))
        flush = getattr(self._stdout, "flush", None)
        if flush is not None:
            flush()
