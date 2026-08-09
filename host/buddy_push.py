"""Push the device overlay onto the Cardputer-Adv through the USB REPL.

Replaces the `buddy/scripts/push.py` we used to borrow from the
moremas/build-with-claude clone (Apache-2.0, see NOTICE), and, since,
this repository's own paste-mode reimplementation of it. The transfer
itself is now `mpremote`'s — see host/device_repl.py for why — which
leaves this module with the part that is actually specific to us: which
files make up the overlay, where they go, and confirming they landed
whole.

Usage:

    uv run python host/buddy_push.py --port /dev/cu.usbmodem101

The device must be sitting at the REPL. If the Buddy app is running it
has disabled Ctrl-C (`micropython.kbd_intr(-1)`), so press BtnRST; the
transfer waits for that rather than failing. Whoever holds the port
holds it exclusively — disconnect the MCP server (`buddy_disconnect`)
before running this.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from device_repl import Repl, ReplError, connect_repl

# The overlay this repository owns. Everything else on the device
# (buddy_protocol, buddy_ui_cp, buddy_state, buddy_chars, main.py) comes
# from upstream and is installed by the m5-onboard skill.
DEFAULT_FILES: tuple[str, ...] = (
    "buddy_serial.py",
    "buddy_chat.py",
    "buddy_speak.py",
    "buddy_tts.py",
    "apps/claude_buddy.py",
)

DEST_ROOT = "/flash"


def push_file(repl: Repl, src: Path, dest: str, *, quiet: bool = False) -> int:
    """Copy `src` to `/flash/<dest>`. Returns the size the device reports.

    Only one level of directory is created, which is all the install
    layout uses: peer modules at `/flash/` and apps at `/flash/apps/`.
    """
    data = src.read_bytes()
    target = f"{DEST_ROOT}/{dest}"

    def progress(written: int, total: int) -> None:
        sys.stderr.write(f"\r  {dest}: {written}/{total} bytes")
        sys.stderr.flush()

    try:
        if "/" in dest:
            parent = f"{DEST_ROOT}/{dest.rsplit('/', 1)[0]}"
            if not repl.fs_isdir(parent):
                repl.fs_mkdir(parent)
        repl.fs_writefile(target, data, progress_callback=None if quiet else progress)
        # Stat what landed rather than trusting the write. A short
        # transfer is otherwise indistinguishable from a good one until
        # the app fails to import.
        landed = repl.fs_stat(target).st_size
    except Exception as exc:
        # mpremote raises TransportError for a link problem and OSError
        # for a device-side filesystem error; both mean the same thing
        # to the operator, and neither names the file on its own.
        raise ReplError(f"{dest}: transfer failed: {exc}") from None
    finally:
        if not quiet:
            sys.stderr.write("\n")

    if landed != len(data):
        raise ReplError(f"{dest}: {landed} bytes on flash, sent {len(data)}")
    return landed


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
    ap.add_argument(
        "--wait",
        type=float,
        default=180.0,
        help=(
            "Seconds to wait for the REPL. Getting there needs a BtnRST press, "
            "so this polls rather than failing straight away. 0 to not wait."
        ),
    )
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

    def nudge() -> None:
        sys.stderr.write("waiting for the REPL — press BtnRST on the device...\n")
        sys.stderr.flush()

    try:
        repl = connect_repl(args.port, args.baud, timeout=args.wait, on_wait=nudge)
    except ReplError as e:
        sys.stderr.write(f"{e}\n")
        return 1

    try:
        for path, name in sources:
            sys.stderr.write(f"uploading {name}...\n")
            push_file(repl, path, name)
    except ReplError as e:
        sys.stderr.write(f"\n{e}\n")
        return 1
    finally:
        repl.close()

    sys.stderr.write("done. Launch with: buddy_start_app (MCP) or buddy_bridge --start\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
