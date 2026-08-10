"""Report what the installed UIFlow build actually offers.

The chat panel's geometry is derived from font metrics — how tall a row
is, how wide a Japanese glyph is — and those are properties of whichever
UIFlow 2.0 build happens to be on the device, not of anything in this
repository. `device/buddy_chat.py` carries a table of measured numbers;
this is what measures them, so the table can be re-checked after a
firmware change instead of quietly rotting.

It also dumps the `M5.Speaker` surface, which is the starting point for
anything that wants to make noise, and the network surface — the HTTP
client, sockets, and what the launcher left the radio doing — which is
what the device needs to fetch its own audio from VOICEVOX.

    uv run python host/probe_device.py --port /dev/cu.usbmodem101

The device must be sitting at the REPL. A running Buddy app is
interrupted its way out of by the handshake, so BtnRST is only needed
when that does not take. Read-only: nothing is written to flash and no
state is touched.

Every measurement comes back as a Python object rather than as text to
be scraped, because the raw REPL hands `eval` the `repr` of what ran and
parses it here. Output is JSON so a probe can be diffed against a
previous one.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Sequence
from typing import Any

from device_repl import Repl, ReplError, connect_repl

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

# What each font is measured for, in the order the device returns them.
METRIC_FIELDS: tuple[str, ...] = ("height", "ja", "ascii", "indent")

# HTTP client module names to try. The firmware renamed `urequests` to
# `requests` in 1.20, and which one is present decides how
# device/buddy_tts.py imports it.
HTTP_MODULES: tuple[str, ...] = ("requests", "urequests")


def probe_display(repl: Repl, fonts: Sequence[str] = CANDIDATE_FONTS) -> dict[str, Any]:
    """Font metrics and the Speaker surface.

    The Japanese sample is written as an escape rather than a literal:
    it travels to the device as source text, and not every path between
    here and its parser is UTF-8 clean.

    `setFont` is sticky on this firmware, so the block puts DejaVu9 back
    before it returns. Leaving a 24 px face selected would make the
    launcher's own UI redraw at the wrong size.
    """
    names = ", ".join(repr(name) for name in fonts)
    repl.exec(
        "import M5\n"
        "_L = M5.Lcd\n"
        "_m = {}\n"
        f"for _n in ({names},):\n"
        "    _f = getattr(_L.FONTS, _n, None)\n"
        "    if _f is None:\n"
        "        continue\n"
        "    _L.setFont(_f)\n"
        "    _m[_n] = (_L.fontHeight(), _L.textWidth('\\u3042'),"
        " _L.textWidth('A'), _L.textWidth('> '))\n"
        "_L.setFont(_L.FONTS.DejaVu9)\n"
    )
    raw: dict[str, tuple[int, ...]] = repl.eval("_m")
    return {
        "fonts": repl.eval("sorted(n for n in dir(M5.Lcd.FONTS) if not n.startswith('_'))"),
        "speaker": repl.eval("sorted(n for n in dir(M5.Speaker) if not n.startswith('_'))"),
        "metrics": {
            name: dict(zip(METRIC_FIELDS, values, strict=True)) for name, values in raw.items()
        },
    }


def probe_network(repl: Repl) -> dict[str, Any]:
    """The network surface `device/buddy_tts.py` is built on.

    Each entry answers a question the design rests on:

    * which HTTP client module this build ships, if any;
    * whether its `Response` exposes `raw` — without it the only way to
      get the body is `content`, which loads the whole utterance into
      heap instead of streaming it a block at a time;
    * whether a socket can be told to give up rather than block, since
      a read that waits would freeze the app's 40 ms tick;
    * what the launcher already did to the radio.

    Read-only, like the rest of this tool: the WLAN object is
    constructed but only interrogated. `active(True)` would change the
    radio state, so it is not called here.
    """
    out: dict[str, Any] = {"http": None}
    for name in HTTP_MODULES:
        try:
            repl.exec(f"import {name} as _http")
        except Exception:
            continue
        out["http"] = {
            "module": name,
            "names": repl.eval("sorted(n for n in dir(_http) if not n.startswith('_'))"),
            "response": repl.eval(
                "sorted(n for n in dir(_http.Response) if not n.startswith('_'))"
            ),
        }
        break

    try:
        repl.exec("import socket\n_s = socket.socket()")
        out["socket"] = repl.eval("sorted(n for n in dir(_s) if not n.startswith('_'))")
        repl.exec("_s.close()")
    except Exception as exc:
        out["socket"] = f"unavailable: {exc}"

    try:
        repl.exec("import network\n_w = network.WLAN(network.STA_IF)")
        out["wlan"] = repl.eval("(_w.active(), _w.isconnected(), _w.ifconfig())")
    except Exception as exc:
        out["wlan"] = f"unavailable: {exc}"

    return out


def probe_heap(repl: Repl) -> int:
    """Free heap after a collection. The ceiling everything else fits in."""
    repl.exec("import gc\ngc.collect()")
    return repl.eval("gc.mem_free()")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("--port", default="/dev/cu.usbmodem101")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument(
        "--wait",
        type=float,
        default=180.0,
        help="Seconds to wait for the REPL, if it takes a BtnRST press. 0 to not wait.",
    )
    args = ap.parse_args(argv)

    def nudge() -> None:
        sys.stderr.write("waiting for the REPL — press BtnRST on the device...\n")
        sys.stderr.flush()

    try:
        repl = connect_repl(args.port, args.baud, timeout=args.wait, on_wait=nudge)
    except ReplError as e:
        sys.stderr.write(f"{e}\n")
        return 1

    try:
        report = {
            "display": probe_display(repl),
            "network": probe_network(repl),
            "heap": probe_heap(repl),
        }
    finally:
        with contextlib.suppress(Exception):
            repl.exit_raw_repl()
        repl.close()

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
