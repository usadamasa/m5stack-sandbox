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
inbound 0x03 on it as ``KeyboardInterrupt``. This used to disable the
interrupt with ``micropython.kbd_intr(-1)``, because a JSON payload
carrying that byte would have killed the app mid-loop, and recovering
from a running app meant a physical BtnRST press.

Neither half of that holds any more:

  - everything the host puts on the wire comes out of ``json.dumps``
    (``host/link/src/buddy_bridge.py``), whose ``ensure_ascii`` default
    escapes 0x00..0x1F as ``\\uXXXX``. There is no path from a message
    payload to a raw 0x03.
  - the bulk mode that did send raw bytes is gone, as the section below
    records.

So the interrupt stays enabled, and ``apps/claude_buddy.py`` catches the
``KeyboardInterrupt`` to tear down and stop at the REPL instead of
rebooting. That is the only way back into a running app without touching
the board, and it costs no memory to have. Anything that reintroduces a
raw binary mode on this channel has to take ``kbd_intr(-1)`` back with
it.

### One byte at a time

``poll()`` drains one byte per ``poll(0)``, which measures at about
24 KiB/s on this board and is capped at ``_MAX_DRAIN`` per tick on top
of that. Slow, and deliberately so: a line has no declared length, and
``readinto`` on this port **blocks until its buffer is full** — measured
at 41 s for a 1024-byte buffer against a 100-byte burst. Anything that
guesses how much to read will eventually freeze the UI loop.

This used to matter, because audio came down the same wire and needed
32 KB/s sustained. There was a bulk mode for it: the host declared a
length, line parsing suspended, and the blocking read became safe
because the size was known. It measured 182 KiB/s and it is gone — the
device now fetches its own audio over WiFi (``buddy/tts.py``) and this
channel carries nothing but JSON commands, which one byte at a time
handles with room to spare.
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


def _binary_streams():
    """Return (stdin_reader, stdout_writer) as byte streams.

    MicroPython exposes ``.buffer`` on the std streams for the esp32
    port, but the attribute is absent on some builds. Fall back to the
    text stream and let the encode/decode happen at the edges.

    No return annotation: `getattr` with a concrete default already gives
    basedpyright a real (if permissive) type for each stream, and a bare
    ``-> tuple:`` here would throw that away in favour of ``Unknown``.
    """
    stdin = getattr(sys.stdin, "buffer", sys.stdin)
    stdout = getattr(sys.stdout, "buffer", sys.stdout)
    return stdin, stdout


def _noop_line(_line):
    # type: (bytes) -> None
    return None


def _noop_state(_st):
    # type: (str) -> None
    return None


class BuddySerial:
    """Nordic-UART-shaped protocol over the USB CDC console."""

    # No pairing layer exists here at all. Nothing in this repository
    # reads the flag any more — apps/claude_buddy.py remaps "connected"
    # to "encrypted" unconditionally — but `buddy_protocol` is upstream
    # and lives only on flash, so the attribute stays.
    pairing_supported = False

    def __init__(
        self,
        name_prefix="Claude",  # type: str
        # on_line/on_state are duck-typed callbacks. MicroPython has no
        # `typing`, so there is no builtin name that spells a callable's
        # signature (see tests/test_device_constraints.py) — both stay
        # Unknown to basedpyright, and so does everything assigned from them
        # below. Ignored per-line rather than left to cascade silently.
        #
        # Upstream's BuddyBLE also took an `on_passkey`. There is no
        # pairing step on this transport, so nothing would ever call it.
        on_line=None,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        on_state=None,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    ) -> None:
        self._on_line = on_line or _noop_line  # pyright: ignore[reportUnknownMemberType]
        self._on_state = on_state or _noop_state  # pyright: ignore[reportUnknownMemberType]

        self._name = name_prefix + "_serial"
        self._rx_buf = bytearray()
        self._shutting_down = False
        self._host_seen = False
        self._last_rx_ms = 0

        self._stdin, self._stdout = _binary_streams()
        self._poller = select.poll()
        self._poller.register(self._stdin, select.POLLIN)

        # Ctrl-C is deliberately left enabled — see the module docstring.
        # Asserted rather than assumed: whatever ran before us may have
        # turned it off, and the escape hatch is only worth having if it
        # is reliably there.
        if micropython is not None:
            micropython.kbd_intr(3)

        print("buddy.serial: up as", self._name)

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

    def send_line(self, payload):
        # type: (bytes | bytearray | str) -> bool
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
            print("buddy.serial: write failed:", e)
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
            # Idempotent with __init__ now, and kept because deinit is
            # also the path a future transport that *did* disable it
            # would come through.
            micropython.kbd_intr(3)
        self._on_line = _noop_line  # pyright: ignore[reportUnknownMemberType]
        self._on_state = _noop_state  # pyright: ignore[reportUnknownMemberType]
        print("buddy.serial: down")

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
            print("buddy.serial: rx overflow, resyncing")
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
        self._on_line(payload)  # pyright: ignore[reportUnknownMemberType]

    def _emit_state(self, state: str) -> None:
        try:
            self._on_state(state)  # pyright: ignore[reportUnknownMemberType]
        except Exception as e:
            print("buddy.serial: on_state error:", e)

    def _write(self, raw: bytes) -> None:
        try:
            # Deliberately duck-typed: this is bytes-mode on the buffer stream
            # and wrong-type on the text-mode fallback, which is exactly the
            # TypeError caught below. basedpyright sees only the text-mode
            # branch's `write(s: str)` and flags the mismatch it's meant to
            # provoke.
            self._stdout.write(raw)  # pyright: ignore[reportArgumentType]
        except TypeError:
            # Text-mode fallback when .buffer was unavailable.
            self._stdout.write(raw.decode("utf-8"))
        flush = getattr(self._stdout, "flush", None)
        if flush is not None:
            flush()
