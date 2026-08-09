"""Push the device overlay onto the Cardputer-Adv through the USB REPL.

Replaces the `buddy/scripts/push.py` we used to borrow from the
moremas/build-with-claude clone (Apache-2.0, see NOTICE). Same transfer
mechanism — paste-mode REPL plus base64 chunks, which needs nothing on
the device beyond stock MicroPython — with three changes that matter on
this board:

* **No DTR/RTS reset.** The Cardputer-Adv enumerates as native USB CDC,
  where toggling the modem control lines does nothing. Upstream opens
  with a "hard reset" that silently no-ops here, so we interrupt into
  the REPL instead and say so when we cannot get there.
* **The prompt is checked before writing.** Without a REPL on the other
  end every paste block is swallowed and the push reports success while
  having written nothing. That failure mode cost a whole debugging
  session once; it is now an error before the first byte goes out.
* **Every file is stat'd afterwards.** A short write is otherwise
  indistinguishable from a good one until the app fails to import.

Usage:

    uv run python host/buddy_push.py --port /dev/cu.usbmodem101

The device must be sitting at the REPL. If the Buddy app is running it
has disabled Ctrl-C (`micropython.kbd_intr(-1)`), so press BtnRST first.
Whoever holds the port holds it exclusively — disconnect the MCP server
(`buddy_disconnect`) before running this.
"""

from __future__ import annotations

import argparse
import base64
import sys
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import serial

from buddy_bridge import SerialPort

# The overlay this repository owns. Everything else on the device
# (buddy_protocol, buddy_ui_cp, buddy_state, buddy_chars, main.py) comes
# from upstream and is installed by the m5-onboard skill.
DEFAULT_FILES: tuple[str, ...] = ("buddy_serial.py", "apps/claude_buddy.py")

DEST_ROOT = "/flash"

# Source bytes per paste block. One block per chunk rather than one huge
# block: a paste of several thousand base64 lines has been observed to
# truncate on the device's rx side without any error surfacing.
CHUNK_BYTES = 512

PROMPT = b">>>"


# ----- paste-mode payloads
#
# Kept as pure string builders so the wire format is unit-testable
# without a board attached.


def open_script(dest: str) -> str:
    """Return the paste block that opens `dest` for writing.

    Creates the parent directory when `dest` names one. Only a single
    level is handled, which is all the install layout uses: peer modules
    at `/flash/` and apps at `/flash/apps/`.
    """
    lines = ["import ubinascii"]
    if "/" in dest:
        parent = dest.rsplit("/", 1)[0]
        lines += [
            "import uos",
            f"try: uos.stat('{DEST_ROOT}/{parent}')",
            f"except OSError: uos.mkdir('{DEST_ROOT}/{parent}')",
        ]
    lines.append(f"fp = open('{DEST_ROOT}/{dest}', 'wb')")
    return "\n".join(lines) + "\n"


def chunk_script(chunk: bytes) -> str:
    """Return the paste block that appends one chunk to the open file."""
    b64 = base64.b64encode(chunk).decode("ascii")
    return f"fp.write(ubinascii.a2b_base64('{b64}'))\n"


def close_script(dest: str, size: int) -> str:
    """Return the paste block that closes the file and reports its size.

    The device stats what it just wrote rather than echoing what we
    think we sent, so a truncated transfer shows up as a mismatch here
    instead of as an ImportError on the next launch.
    """
    return (
        "fp.close()\n"
        "import uos\n"
        f"print('PUSHED', '{dest}', uos.stat('{DEST_ROOT}/{dest}')[6], {size})\n"
    )


def iter_chunks(data: bytes, size: int = CHUNK_BYTES) -> Iterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


# ----- REPL session


class PushError(RuntimeError):
    """A transfer step the device refused or answered unexpectedly."""


class ReplSession:
    """Paste-mode conversation with the MicroPython REPL."""

    def __init__(self, ser: SerialPort) -> None:
        self._ser = ser

    def drain(self, wait: float = 0.2) -> bytes:
        """Sleep `wait` seconds, then read whatever has queued up."""
        time.sleep(wait)
        out = bytearray()
        while self._ser.in_waiting:
            out += self._ser.read(self._ser.in_waiting)
            time.sleep(0.03)
        return bytes(out)

    def ensure_prompt(self, attempts: int = 3) -> None:
        """Interrupt to the REPL, or explain why we cannot get there.

        A Buddy app that is already up has turned Ctrl-C off, so this is
        the point where the caller learns they need BtnRST — before any
        paste block has been sent into a void.
        """
        for _ in range(attempts):
            for _ in range(4):
                self._ser.write(b"\x03")
                time.sleep(0.05)
            self._ser.write(b"\r\n")
            if PROMPT in self.drain(wait=0.4):
                return
        raise PushError(
            "no REPL prompt after Ctrl-C. The Buddy app disables Ctrl-C while "
            "its serial transport is up — press BtnRST on the device, then retry."
        )

    def paste(self, script: str, settle: float = 0.2) -> str:
        """Run one paste-mode block and return everything it echoed."""
        self._ser.write(b"\x05")  # Ctrl-E: paste mode, no auto-indent
        time.sleep(0.1)
        self.drain(wait=0.1)
        for line in script.splitlines():
            self._ser.write(line.encode("utf-8") + b"\r\n")
            time.sleep(0.005)
        self._ser.write(b"\x04")  # Ctrl-D: execute
        return self.drain(wait=settle).decode("utf-8", errors="replace")


def push_file(session: ReplSession, src: Path, dest: str, *, quiet: bool = False) -> int:
    """Copy `src` to `/flash/<dest>`. Returns the size the device reports."""
    data = src.read_bytes()

    # `except OSError:` is part of the block we just echoed, so matching
    # on "Error" here would flag every push with a subdirectory.
    echoed = session.paste(open_script(dest), settle=0.2)
    if "Traceback" in echoed:
        raise PushError(f"could not open {dest} for writing:\n{echoed}")

    sent = 0
    for chunk in iter_chunks(data):
        echoed = session.paste(chunk_script(chunk), settle=0.05)
        if "Traceback" in echoed or "Error" in echoed:
            raise PushError(f"{dest}: write failed at offset {sent}:\n{echoed}")
        sent += len(chunk)
        if not quiet:
            sys.stderr.write(f"\r  {dest}: {sent}/{len(data)} bytes")
            sys.stderr.flush()
    if not quiet:
        sys.stderr.write("\n")

    echoed = session.paste(close_script(dest, len(data)), settle=0.3)
    marker = f"PUSHED {dest} {len(data)} {len(data)}"
    if marker not in echoed:
        raise PushError(f"{dest}: device did not confirm {len(data)} bytes on flash:\n{echoed}")
    return len(data)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("--port", required=True)
    ap.add_argument(
        "--src",
        default="device",
        help="Directory holding the overlay sources (default: device).",
    )
    ap.add_argument(
        "--files",
        nargs="*",
        default=list(DEFAULT_FILES),
        help="Paths under --src to upload.",
    )
    ap.add_argument("--baud", type=int, default=115200)
    return ap.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)

    src_dir = Path(args.src).resolve()
    sources: list[tuple[Path, str]] = []
    for name in args.files:
        path = src_dir / name
        if not path.is_file():
            sys.stderr.write(f"missing source: {path}\n")
            return 2
        sources.append((path, name))

    with serial.Serial(args.port, args.baud, timeout=1.0) as ser:
        session = ReplSession(ser)
        try:
            session.ensure_prompt()
            for path, name in sources:
                sys.stderr.write(f"uploading {name}...\n")
                push_file(session, path, name)
        except PushError as e:
            sys.stderr.write(f"\n{e}\n")
            return 1

    sys.stderr.write("done. Launch with: buddy_start_app (MCP) or buddy_bridge --start\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
