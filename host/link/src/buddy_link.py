"""The two ways to hold a serial session open, and how to start one.

`BuddyLink` is one command's worth: open, ask, close. `ResidentLink`
outlives the device rebooting under it, which is what the MCP server
needs from a link it keeps for a whole session. Launching the app lives
here too, because both of them adopt the port the REPL was using rather
than opening one of their own.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections import deque
from collections.abc import Callable

import serial

from buddy_wire import (
    LineDemux,
    Message,
    SerialFactory,
    SerialPort,
    decode,
    encode,
)
from device_repl import connect_repl, run_and_release

# ----- launching the app
#
# The app takes the console over: once its transport is up it speaks the
# sentinel protocol on the same wire. So the launch happens through the
# REPL, and the port that the REPL was using is handed straight to the
# link rather than closed and reopened — the device says whatever it is
# going to say about a failed import in that gap, and reopening would
# miss it.

# Imported rather than exec'd: the launcher uses the same path, and
# compiling an 18 KB source string in one go is a poor fit for the
# ~65 KB of free heap this bundle leaves behind.
#
# The module is dropped from the cache first. `claude_buddy` calls run()
# from its module body, so a second `import` of a module still in
# sys.modules is a no-op — and the app that was running a moment ago put
# it there. Before Ctrl-C worked this could not happen: every exit went
# through machine.reset() and took sys.modules with it. Now the common
# case is a device that was interrupted back to the REPL, where a plain
# import launches nothing and says nothing about why.
#
# The collect matters as much as the delete. The previous run's UI,
# transport and speech objects are unreachable once run() has returned
# but are not yet gone, and re-importing on top of them is what the
# "MemoryError: memory allocation failed" in AGENTS.md was.
LAUNCH_SOURCE = (
    "import sys, gc\n"
    "for _p in ('/flash', '/flash/apps'):\n"
    "    if _p not in sys.path: sys.path.insert(0, _p)\n"
    "if 'claude_buddy' in sys.modules: del sys.modules['claude_buddy']\n"
    "gc.collect()\n"
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

    Needs a REPL on the far end, and a running app is not one. Send
    `interrupt()` first to drop it back to the prompt; `wait` is how long
    a BtnRST press is waited for when that is not possible — a device
    wedged below the Python level, or a bundle old enough to still
    disable Ctrl-C.

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

    def interrupt(self) -> None:
        """Ctrl-C the device, dropping a running app back to the REPL.

        One raw byte, deliberately outside the sentinel framing: 0x03 is
        taken by MicroPython's console reader before any Python on the
        device sees it, so framing it would only make it invisible.

        Nothing acks this. The app catches the KeyboardInterrupt, tears
        its transport down and stops *without* rebooting, so the port
        stays open and the prompt on the other end is live. What it says
        on the way out arrives as log lines — `pump()` for them.
        """
        self._io.write(b"\x03")
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

    def interrupt(self) -> None:
        """Ctrl-C the device. See `BuddyLink.interrupt`.

        Under the write lock like any other write, so it cannot land in
        the middle of a frame somebody else is putting on the wire. The
        reader thread stays on the port: the app's parting words, and
        the REPL banner behind them, are what tells you it worked.
        """
        with self._write_lock:
            self._io.write(b"\x03")
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

        A running app is interrupted first — that is what gets us a REPL
        without anyone touching the board. `wait` covers the case where
        it does not answer, and is short on purpose: a tool call that
        blocks for three minutes waiting for a BtnRST press is worse
        than one that says it needs one.
        """
        if self.connected:
            # Best-effort. A device already at the REPL ignores it, and
            # a port that has gone away is about to be reported by the
            # launch anyway.
            with contextlib.suppress(Exception):
                self.interrupt()
                time.sleep(0.5)
        self.disconnect()
        ser = launch_app(self.port, self.baud, self.read_timeout, wait=wait)
        self.connect(adopt=ser)
        time.sleep(settle)
