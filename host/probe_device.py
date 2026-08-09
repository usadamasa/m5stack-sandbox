"""Report what the installed UIFlow build actually offers.

The chat panel's geometry is derived from font metrics — how tall a row
is, how wide a Japanese glyph is — and those are properties of whichever
UIFlow 2.0 build happens to be on the device, not of anything in this
repository. `device/buddy_chat.py` carries a table of measured numbers;
this is what measures them, so the table can be re-checked after a
firmware change instead of quietly rotting.

It also dumps the `M5.Speaker` surface, which is the starting point for
anything that wants to make noise.

    uv run python host/probe_device.py --port /dev/cu.usbmodem101

The device must be sitting at the REPL — press BtnRST if the Buddy app
is running, since it disables Ctrl-C. Read-only: nothing is written to
flash and no state is touched.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

import serial

from buddy_push import PushError, ReplSession

# Fonts worth measuring. Anything absent on the build is skipped rather
# than reported as an error — the point is to find out what is there.
CANDIDATE_FONTS: tuple[str, ...] = (
    "EFontJA24",
    "AlibabaSansJA24",
    "EFontCN24",
    "Montserrat14",
    "DejaVu12",
    "DejaVu9",
)

# Every reported line is tagged so it can be picked out of the paste-mode
# echo, which interleaves our own input with the output.
_TAG = "PROBE"


def probe_script(fonts: Sequence[str] = CANDIDATE_FONTS) -> str:
    """The paste block to run on the device.

    The Japanese sample is written as an escape rather than a literal:
    it travels through the REPL as source text, and not every path
    between here and the device's parser is UTF-8 clean.
    """
    names = ", ".join(repr(name) for name in fonts)
    return (
        "import M5, gc\n"
        "L = M5.Lcd\n"
        f"print('{_TAG} fonts', sorted(n for n in dir(L.FONTS) if not n.startswith('_')))\n"
        f"print('{_TAG} speaker', sorted(n for n in dir(M5.Speaker) if not n.startswith('_')))\n"
        f"for _n in ({names},):\n"
        "    _f = getattr(L.FONTS, _n, None)\n"
        "    if _f is None:\n"
        "        continue\n"
        "    L.setFont(_f)\n"
        f"    print('{_TAG} metric', _n, 'h', L.fontHeight(),"
        " 'ja', L.textWidth('\\u3042'), 'ascii', L.textWidth('A'),"
        " 'indent', L.textWidth('> '))\n"
        "L.setFont(L.FONTS.DejaVu9)\n"
        "gc.collect()\n"
        f"print('{_TAG} heap', gc.mem_free())\n"
    )


def extract(echoed: str) -> list[str]:
    """Pull the tagged report lines out of the paste-mode echo.

    Lines are matched on the tag appearing anywhere, not at the start:
    the echo can leave a `===` prompt fragment in front of one.
    """
    out: list[str] = []
    for line in echoed.splitlines():
        idx = line.find(_TAG)
        if idx >= 0 and "print(" not in line:
            out.append(line[idx:].rstrip())
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("--port", default="/dev/cu.usbmodem101")
    ap.add_argument("--baud", type=int, default=115200)
    args = ap.parse_args(argv)

    with serial.Serial(args.port, args.baud, timeout=1.0) as ser:
        session = ReplSession(ser)
        try:
            session.ensure_prompt()
        except PushError as e:
            sys.stderr.write(f"{e}\n")
            return 1
        lines = extract(session.paste(probe_script(), settle=1.5))

    if not lines:
        sys.stderr.write("device answered nothing — is the REPL responsive?\n")
        return 1
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
