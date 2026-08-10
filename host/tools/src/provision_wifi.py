"""Point the device at your own WiFi, once, instead of every boot.

Run this after flashing and it stops being part of the workflow:

    export BUDDY_WIFI_PSK=...
    uv run python host/provision_wifi.py --port /dev/cu.usbmodem101 \\
        --ssid MyNetwork --verify

### What it edits, and why that file

`/flash/wifi_event.py` is where the bundle keeps the credentials it
auto-connects with at boot, and it ships with an M5Stack event venue's
AP baked in (`cardputer` / `cardconnect`, both public at the venue).
Its own docstring names replacing `SSID` and `PASSWORD` as the
supported way to use the bundle elsewhere, so this is the documented
seam rather than a patch around one.

NVS looks like the obvious target and is not. UIFlow's startup does read
WiFi credentials from `uiflow/ssid0` and `uiflow/pswd0` — but this
device has `uiflow/boot_option` set to 2, "user app mode", which skips
UIFlow's framework entirely so that `/flash/main.py` can run as the
launcher. Nothing on the boot path reads those keys; measured, they are
present and empty while the device is nonetheless associated. The
credentials have to go where `main.py` looks, which is this module.

### What it costs

The passphrase ends up in plaintext on the device's filesystem. That is
a real change: the alternative it replaces — handing credentials down
the cable into the raw REPL on every boot — never persisted anything.
It buys a device that is on the network before the app starts, which is
the only time it can get there (see host/buddy_bridge.py's network
note), with no host involvement at all.

### Recovery

A half-written `wifi_event.py` is not fatal. `main.py` imports it inside
a try/except and boots without a network when the import fails, so the
REPL is still reachable and running this again fixes it. That is why
the file is written in place rather than through a rename dance.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import os
import re
import sys
import time
from collections.abc import Sequence

from device_repl import Repl, ReplError, connect_repl

DEST = "/flash/wifi_event.py"

# The two module-level assignments, and nothing that merely mentions the
# names. Anchored at column zero because both occur in prose inside the
# module docstring — "Replace ``SSID`` / ``PASSWORD`` with your own" —
# and in the body as arguments to `sta.connect(SSID, PASSWORD)`.
_ASSIGNMENTS = {
    "SSID": re.compile(rb"^SSID[ \t]*=[ \t]*(?P<value>.+?)[ \t]*$", re.MULTILINE),
    "PASSWORD": re.compile(rb"^PASSWORD[ \t]*=[ \t]*(?P<value>.+?)[ \t]*$", re.MULTILINE),
}


class ProvisionError(RuntimeError):
    """The file on the device is not the one this knows how to edit."""


def _literal(value: str) -> bytes:
    """`value` as Python source.

    `repr` rather than quoting by hand: this becomes a line in a file
    the device compiles at boot, and an apostrophe in a passphrase would
    otherwise close the literal and leave the remainder as code.
    """
    return repr(value).encode("utf-8")


def read_credentials(source: bytes) -> tuple[str, str]:
    """The `(ssid, password)` a `wifi_event.py` would connect with.

    Both sides of the edit go through here — the file that arrived and
    the file that was written back — so a rewrite that produced
    something unexpected is caught rather than trusted.
    """
    out: list[str] = []
    for name, pattern in _ASSIGNMENTS.items():
        found = pattern.findall(source)
        if len(found) != 1:
            raise ProvisionError(f"{DEST}: expected one `{name} =` line, found {len(found)}")
        try:
            value = ast.literal_eval(found[0].decode("utf-8"))
        except (SyntaxError, ValueError) as exc:
            # literal_eval, never eval. Whatever is in this file came
            # off the device, and it is not going to run on this end.
            raise ProvisionError(f"{DEST}: `{name}` is not a literal: {exc}") from None
        if not isinstance(value, str):
            raise ProvisionError(f"{DEST}: `{name}` is {type(value).__name__}, not a string")
        out.append(value)
    return out[0], out[1]


def patch_credentials(source: bytes, ssid: str, psk: str) -> bytes:
    """Return `source` with its two credential assignments replaced.

    Everything else is preserved byte for byte, including the comment
    banner around them: this is upstream's file, kept on the device and
    deliberately not copied into this repository, so the diff after a
    firmware update should be exactly two lines.
    """
    if not ssid:
        raise ProvisionError("no ssid")

    # Read first. It validates that there is exactly one of each
    # assignment before anything is rewritten, so a file with two
    # `SSID =` lines fails here rather than being half-edited.
    read_credentials(source)

    out = source
    for name, value in (("SSID", ssid), ("PASSWORD", psk)):
        out = _ASSIGNMENTS[name].sub(
            # A callable, not a template: `\g<...>` and backslashes in a
            # replacement string are interpreted, and a passphrase is
            # exactly the sort of value that contains one.
            lambda _m, _n=name, _v=value: _n.encode("ascii") + b" = " + _literal(_v),
            out,
            count=1,
        )
    return out


def provision(repl: Repl, ssid: str, psk: str, *, quiet: bool = False) -> dict[str, object]:
    """Rewrite the device's boot credentials. Returns what it wrote.

    The result carries the SSID and the file's new size, never the
    passphrase — it goes into a log line and a tool result, and this is
    the one value in the exchange that must not.
    """

    def note(message: str) -> None:
        if not quiet:
            sys.stderr.write(message + "\n")

    try:
        # `bytes` because the patched result is compared against what
        # comes back, and mpremote hands over a bytearray.
        before = bytes(repl.fs_readfile(DEST))
    except Exception as exc:
        # Creating the file instead would mean this repository carrying
        # a copy of an upstream module, which is precisely what the
        # overlay is arranged to avoid.
        raise ProvisionError(
            f"{DEST}: cannot read it ({exc}). The bundle should have installed it; "
            "reinstall with the m5-onboard skill rather than letting this write one."
        ) from None

    was, _ = read_credentials(before)
    note(f"{DEST}: currently joins {was!r}")

    after = patch_credentials(before, ssid, psk)
    if after == before:
        note("already provisioned with these credentials; writing anyway to be sure")

    try:
        repl.fs_writefile(DEST, after)
        landed = bytes(repl.fs_readfile(DEST))
    except Exception as exc:
        raise ProvisionError(f"{DEST}: transfer failed: {exc}") from None

    # Read back rather than trust the write. A short transfer is
    # otherwise indistinguishable from a good one until the device fails
    # to import the file at its next boot, hours later.
    if landed != after:
        raise ProvisionError(
            f"{DEST}: {len(landed)} bytes on flash, sent {len(after)}. Run this again."
        )
    now_ssid, now_psk = read_credentials(landed)
    if now_ssid != ssid or now_psk != psk:
        raise ProvisionError(f"{DEST}: wrote {ssid!r} but the file reads back as {now_ssid!r}")

    note(f"{DEST}: now joins {ssid!r} ({len(after)} bytes)")
    return {"dest": DEST, "ssid": ssid, "was": was, "bytes": len(after)}


# ----- verification
#
# Names for the esp32 port's WLAN.status() codes. The number reaches a
# human through a log line and nothing else, and 201 and 202 are very
# different problems.
_WIFI_STATUS = {
    200: "beacon timeout",
    201: "no ap found",
    202: "wrong password",
    203: "assoc fail",
    204: "handshake timeout",
    1000: "idle",
    1001: "connecting",
    1010: "got ip",
}

# How long to leave the device alone after a reset before asking for a
# REPL.
#
# Not politeness — polling through the boot does not work. Measured:
# starting to poll straight after `machine.reset()` failed to reach the
# raw REPL for 90 s, while waiting 25 s first got in on the first
# attempt in 0.1 s. The repeated handshake attempts land while
# `/flash/main.py` is initialising NimBLE and running its WiFi splash,
# and the device does not come back from them.
#
# 25 s covers that boot: NimBLE, then `wifi_event.CONNECT_TIMEOUT_MS`
# (8 s), then the launcher menu.
_SETTLE_S = 25.0

# And then how long to wait for the REPL, which by that point answers
# immediately or not at all.
_VERIFY_WAIT_S = 30.0


def reset(repl: Repl) -> None:
    """Reboot the device and let go of the port. Never raises.

    Both halves fail in the ordinary case and mean nothing is wrong.
    `exec` does not get an answer because the device has already gone,
    and `close` fails too: mpremote clears RTS on the way out, and the
    ioctl for that lands on a file descriptor whose device has
    disappeared (`OSError: [Errno 6] Device not configured`). Measured —
    it is the reset working, not a fault.
    """
    with contextlib.suppress(Exception):
        repl.exec("import machine; machine.reset()")
    with contextlib.suppress(Exception):
        repl.close()


def verify(repl: Repl) -> dict[str, object]:
    """Report what the radio is doing. Run after a reboot, not before.

    Reads only. The point is to see the association `main.py` made on
    its own, which is the whole claim provisioning makes.
    """
    connected, ip, code = repl.eval(
        "(lambda w: (w.isconnected(), w.ifconfig()[0], w.status()))"
        "(__import__('network').WLAN(__import__('network').STA_IF))"
    )
    return {"ok": bool(connected), "ip": ip, "status": _WIFI_STATUS.get(code, str(code))}


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("--port", required=True)
    ap.add_argument("--ssid", required=True, help="The network the device should join at boot.")
    ap.add_argument(
        "--psk",
        default=None,
        help="Passphrase. Defaults to $BUDDY_WIFI_PSK so it stays out of shell history.",
    )
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument(
        "--wait",
        type=float,
        default=180.0,
        help=(
            "Seconds to wait for the REPL. A running app is interrupted out of "
            "the way, and this covers the case where that does not take — it "
            "polls rather than failing straight away. 0 to not wait."
        ),
    )
    ap.add_argument(
        "--verify",
        action="store_true",
        help="Reboot afterwards and report whether the device joined on its own.",
    )
    return ap.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    psk = args.psk if args.psk is not None else os.environ.get("BUDDY_WIFI_PSK", "")
    if not psk:
        sys.stderr.write("no passphrase: pass --psk or set $BUDDY_WIFI_PSK\n")
        return 2

    def nudge() -> None:
        sys.stderr.write("waiting for the REPL — press BtnRST on the device...\n")
        sys.stderr.flush()

    try:
        repl = connect_repl(args.port, args.baud, timeout=args.wait, on_wait=nudge)
    except ReplError as e:
        sys.stderr.write(f"{e}\n")
        return 1

    try:
        provision(repl, args.ssid, psk)
    except ProvisionError as e:
        sys.stderr.write(f"{e}\n")
        with contextlib.suppress(Exception):
            repl.close()
        return 1

    if not args.verify:
        repl.close()
        sys.stderr.write("reboot the device to pick it up (BtnRST).\n")
        return 0

    # The reset is what proves the claim: the credentials are only
    # consulted by main.py on a fresh boot, so a check without one would
    # be reporting the association this session started with.
    sys.stderr.write("resetting to check the device joins on its own...\n")
    reset(repl)

    # Do not poll through the boot; see _SETTLE_S.
    sys.stderr.write(f"letting it boot for {_SETTLE_S:.0f}s...\n")
    time.sleep(_SETTLE_S)

    try:
        repl = connect_repl(args.port, args.baud, timeout=_VERIFY_WAIT_S, on_wait=nudge)
    except ReplError as e:
        sys.stderr.write(f"came back but would not give a REPL: {e}\n")
        return 1
    try:
        result = verify(repl)
    finally:
        repl.close()

    sys.stderr.write(f"wifi: {result}\n")
    if not result["ok"]:
        sys.stderr.write(f"the device did not join {args.ssid!r} on its own\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
