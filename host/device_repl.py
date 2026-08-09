"""Getting a usable MicroPython REPL on the far end of the USB cable.

Everything here is a thin layer over `mpremote`, which is MicroPython's
own remote-control tool and ships the transport this repository used to
hand-roll. Used as a library rather than a subprocess: the pieces we
want — `SerialTransport.exec`, `eval`, `fs_writefile` — are the same
objects its CLI drives.

### Why mpremote rather than what was here before

The previous code drove **paste mode** (Ctrl-E / Ctrl-D). Paste mode has
no flow control, so a long transfer can outrun the device's rx buffer
and truncate with nothing said; the workaround was a fixed chunk size
and a sleep per line, which is a guess dressed up as a constant. It also
echoes every line back, so reading a result meant scraping the echo and
excluding our own source from the match.

`mpremote` uses the **raw REPL**, and raw-paste mode inside it, which
has a window-based acknowledgement protocol and no echo. That turns both
problems into non-problems: `exec` either completes or raises, and
`eval` hands back a real Python object.

### Why the wait loop is still ours

`mpremote`'s own `wait=` retries `open()`, which covers a port that has
not re-enumerated yet. It does not cover this board's actual obstacle:
the Buddy app calls `micropython.kbd_intr(-1)`, so the port opens fine
and Ctrl-C does nothing. The only way back is a physical BtnRST press,
and the loop below exists to wait for a human to make it.
"""

from __future__ import annotations

import contextlib
import io
import time
from collections.abc import Callable
from typing import Any, Protocol

from mpremote.transport import TransportError
from mpremote.transport_serial import SerialTransport


class ReplError(RuntimeError):
    """The device would not give us a REPL, or refused what we ran on it."""


class Stat(Protocol):
    """The one field `fs_stat`'s result is read for. mpremote returns an
    `os.stat_result`, which a fake cannot construct meaningfully."""

    @property
    def st_size(self) -> int: ...


class Repl(Protocol):
    """The slice of `mpremote`'s SerialTransport this repository uses.

    Narrow on purpose, exactly like `SerialPort` in buddy_bridge: it is
    what lets the tests drive a fake device without a board attached,
    and it documents the contract in one place instead of leaving it
    implied across three call sites.
    """

    # The open pyserial port. mpremote's own `repl` command reaches for
    # this to hand the console to a terminal, so it is a seam the tool
    # supports rather than an internal. Typed loosely because naming it
    # properly would mean importing buddy_bridge's SerialPort, and
    # buddy_bridge imports this module.
    serial: Any

    def enter_raw_repl(self, soft_reset: bool = True, timeout_overall: int = 10) -> None: ...

    def exit_raw_repl(self) -> None: ...

    def exec_raw_no_follow(self, command: str) -> None: ...

    def exec(self, command: str, data_consumer: Callable[[bytes], None] | None = None) -> bytes: ...

    # Not CPython's builtin. mpremote's `eval` runs `print(repr(<expr>))`
    # on the *device* and parses the reply with `ast.literal_eval`, so
    # nothing from the device is executed on this machine. It is the
    # reason the host no longer scrapes printed output with regexes.
    #
    # The return really is arbitrary: it is whatever the expression
    # evaluated to over there, and the caller is the only thing that
    # knows what shape to expect.
    def eval(self, expression: str, parse: bool = True) -> Any:  # noqa: ANN401
        ...

    def fs_writefile(
        self,
        dest: str,
        data: bytes,
        chunk_size: int = 256,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None: ...

    # A bytearray, not bytes — that is what mpremote hands back, and
    # narrowing it here would be a lie the type checker enforces on the
    # fake and not on the device.
    def fs_readfile(
        self,
        src: str,
        chunk_size: int = 256,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> bytearray: ...

    def fs_stat(self, src: str) -> Stat: ...

    def fs_isdir(self, src: str) -> bool: ...

    def fs_mkdir(self, path: str) -> None: ...

    def close(self) -> None: ...


ReplFactory = Callable[[str, int], Repl]

# Between attempts. A reset takes a couple of seconds to re-enumerate;
# polling faster only produces more failed opens.
_POLL_S = 1.0

# How long one handshake gets before we call it a miss. `mpremote`
# defaults to 10 s, which is right for a device that is merely slow but
# wrong for a poll loop that expects to fail for a while.
_ATTEMPT_TIMEOUT_S = 2


def _open_transport(port: str, baud: int) -> Repl:
    # exclusive=True is pyserial's own flock, and it is what turns "the
    # MCP server still holds the port" from a silent interleaving of two
    # conversations into an error at open time.
    return SerialTransport(port, baudrate=baud, exclusive=True)


def run_and_release(repl: Repl, source: str, read_timeout: float) -> Any:  # noqa: ANN401
    """Start `source` on the device and hand the open port back.

    For code that takes the console over and never returns — an app that
    speaks its own protocol on the same wire. `exec` would wait for an
    end that is not coming, and closing the port to reopen it would drop
    whatever the device says while starting, which is exactly the
    startup traceback worth having.

    This is what `mpremote repl`'s own ctrl-k injection does:
    `enter_raw_repl(soft_reset=False)`, `exec_raw_no_follow`, then back
    to reading the port directly.

    Nothing is written to the port after this. The old paste-mode launch
    had to send a trailing newline because Ctrl-D carries none and the
    stray byte would be prepended to the next protocol frame — whose
    sentinel then no longer started the line, so the device dropped it
    and the first request after a launch timed out. Raw-paste
    acknowledges its own terminator before execution begins, so there is
    nothing left in the device's buffer to clean up.

    The returned port is the caller's to close.
    """
    repl.exec_raw_no_follow(source)
    port = repl.serial
    # mpremote opens blocking, with a one second inter-byte timeout. A
    # reader that polls `in_waiting` needs neither, and a blocking read
    # would stall the caller's loop on a quiet device.
    port.timeout = read_timeout
    port.inter_byte_timeout = None
    return port


def connect_repl(
    port: str,
    baud: int = 115200,
    timeout: float = 180.0,
    factory: ReplFactory | None = None,
    on_wait: Callable[[], None] | None = None,
) -> Repl:
    """Block until the device answers in the raw REPL, then hand it over.

    The caller owns the returned transport and must `close()` it.

    `on_wait` is called once, the first time the device is found not to
    be there — that is the moment to tell the operator to press BtnRST.
    Printing it on every poll would just be noise.
    """
    make = factory if factory is not None else _open_transport
    deadline = time.monotonic() + timeout
    announced = False
    last_noise = ""

    while True:
        repl: Repl | None = None
        try:
            repl = make(port, baud)
            # soft_reset=False on purpose. A soft reset re-runs boot.py
            # and main.py, which on this board means relaunching the
            # UIFlow launcher — and we would only have to interrupt it
            # again a moment later.
            #
            # mpremote prints whatever it read when the handshake fails.
            # That is the right call for a one-shot CLI and the wrong one
            # inside a poll loop, so it is captured and kept for the
            # give-up message instead.
            noise = io.StringIO()
            with contextlib.redirect_stdout(noise):
                repl.enter_raw_repl(soft_reset=False, timeout_overall=_ATTEMPT_TIMEOUT_S)
            last_noise = noise.getvalue()
            # One real round trip before declaring the link usable. The
            # handshake alone has proved not to be enough: macOS presents
            # the device node before the USB interface is configured, and
            # a handle opened in that window has answered once and then
            # raised ENXIO on the next ioctl.
            repl.eval("1")
            return repl
        except (TransportError, OSError, ValueError) as exc:
            if repl is not None:
                # A handle left open on a macOS cu.* device blocks the
                # next open, so the retry would fail for a new reason.
                with contextlib.suppress(Exception):
                    repl.close()
            if not announced:
                announced = True
                if on_wait is not None:
                    on_wait()
            if time.monotonic() >= deadline:
                tail = f"\nlast heard: {last_noise.strip() or exc}"
                raise ReplError(
                    f"device never reached the REPL within {timeout:.0f}s. The Buddy "
                    "app disables Ctrl-C while its serial transport is up, so this "
                    f"needs a BtnRST press on the device.{tail}"
                ) from None
            time.sleep(_POLL_S)
