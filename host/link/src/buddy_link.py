"""1 コマンド分のシリアルセッションと、アプリの起動。

`BuddyLink` は open して聞いて close するまでの 1 回分。起動がここに同居して
いるのは、リンクが自分でポートを開くのではなく REPL が使っていたポートを
そのまま引き取るため。セッションを丸ごと握り、デバイスが下で reboot しても
生き延びる側は `resident_link` の `ResidentLink`。
"""

from __future__ import annotations

import time
from collections.abc import Callable

import serial

from buddy_wire import (
    LineDemux,
    Message,
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
# 起動するのは最後の `run()`。import が読む `/flash/apps/claude_buddy.py` は
# sys.path を整えて `/flash/buddy/app.py` へ橋を渡すだけの起動口で、`main.py`
# も同じ 2 行を書く。キャッシュから先に落とすのは、直前まで走っていたアプリが
# そこへ自分を入れているため。落ちるのは `claude_buddy` だけで、`buddy.app` も
# `buddy.chat` も前の run のまま残る (MicroPython は submodule を package の
# 属性としても持つ)。押し込んだ版を確実に動かしたければ reboot。
#
# The collect matters as much as the delete. The previous run's UI,
# transport and speech objects are unreachable once run() has returned
# but are not yet gone, and re-importing on top of them is what the
# "MemoryError: memory allocation failed" in CLAUDE.md was.
LAUNCH_SOURCE = (
    "import sys, gc\n"
    "for _p in ('/flash', '/flash/apps'):\n"
    "    if _p not in sys.path: sys.path.insert(0, _p)\n"
    "if 'claude_buddy' in sys.modules: del sys.modules['claude_buddy']\n"
    "gc.collect()\n"
    "import claude_buddy\n"
    "claude_buddy.run()\n"
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
