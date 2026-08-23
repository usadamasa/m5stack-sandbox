"""Install the device overlay as precompiled bytecode.

The device parses no source. That is not a speed tweak, it is what makes
the speech path fit: a `.py` on flash means MicroPython builds both the
parse tree and the bytecode in GC heap at import time, and on this board
`import buddy_ui_cp` failed with `MemoryError: allocating 776 bytes`
while `gc.mem_free()` still read 55280 — fragmentation, not exhaustion.
Shipping `.mpy` took the clean heap from 55280 to 101120.

Three consequences, and this module is all three:

  - Every module the app imports is compiled here and pushed as `.mpy`.
    The import machinery checks `foo.py` before `foo.mpy` in each
    `sys.path` entry, so the source has to go or the bytecode is dead
    weight. That deletion is why this is the only way into `/flash`: the
    pusher that came before it took `.py` and put it right back on top
    of the bytecode, and the symptom turned up days later as a synth
    that would not run.
  - The upstream peers (`buddy_protocol`, `buddy_ui_cp`, `buddy_state`,
    `buddy_chars`) are compiled too, from the copy read off the device.
    They are not in this repository and are not redistributed by it —
    the m5-onboard skill installs them from the upstream clone. So
    anything this module removes from flash is archived under `vendor/`
    first: the copy being deleted must never be the only one.
  - The launcher is replaced by `device/main.py`, which brings WiFi up
    and then launches the app, so power-on alone is enough. Upstream's
    starts NimBLE, and the ESP-IDF heap that reserves is the heap the
    speech socket then cannot get. `main.py` stays source — MicroPython
    runs `/flash/main.py` and never looks for `main.mpy`.

### 分かれ方

依存は下から上への一方向で、ここが一番上にいる。

    deploy_spec.py    何をどこへ置くか、と run 全体の型 (Deadline / Job)
    deploy_build.py   mpy-cross を呼んで overlay をバイトコードにする
    deploy_device.py  flash を書き換える。ここから先はデバイスが要る
    buddy_deploy.py   発話で確かめる + CLI

### Why it ends by talking

Everything above proves bytes landed on flash, which is not the same as
a bundle that runs: an import that fails, an engine that cannot be
reached and a speaker that stays silent all look identical from a
directory listing. So the last step launches the app and has the device
say so out loud. That exercises the import, the inherited WiFi link, the
VOICEVOX round trip and `M5.Speaker` in one go, and the confirmation is
audible from across the room rather than being another line of output.

The device is left running the app afterwards. That is no longer a dead
end: Ctrl-C works again (see the "Ctrl-C" section of
`device/buddy/serial.py`), so the next deploy interrupts its way back to
the REPL instead of asking for a BtnRST press. `--no-speak` still stops
at the REPL without launching at all.

### Why the timeout lives in here

mpremote opens the port with `timeout=None`, and `raw_paste_write` does
a bare `serial.read(1)` waiting for the flow-control byte. A device that
stops answering mid-transfer blocks forever, which an outer
`timeout 300` was papering over. Two things replace it: a finite read
timeout on the port, so a dead link raises instead of hanging, and a
budget checked between steps, so the run ends naming the step it died
on rather than being killed from outside with no idea where it was.

    uv run python host/buddy_deploy.py --port /dev/cu.usbmodem101
    uv run python host/buddy_deploy.py --compile-only    # no board

Whoever holds the port holds it exclusively — disconnect the MCP server
(`buddy_disconnect`) first. The device must be at the REPL, and since
boot launches the app it usually is not; the handshake interrupts a
running app out of the way, and the wait below covers the case where
that does not take.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from buddy_link import DEFAULT_READ_TIMEOUT, LAUNCH_SOURCE, BuddyLink
from buddy_verbs import say, speak, voicevox_url
from buddy_wire import Message
from deploy_build import build_overlay, check_launcher, compile_source, mpy_cross_abi
from deploy_device import (
    device_mpy_abi,
    find_shadows,
    install_launcher,
    prune,
    prune_stale,
    push_jobs,
    report_flash,
    stage_upstream,
)
from deploy_spec import (
    DEFAULT_BUILD,
    DEFAULT_TIMEOUT_S,
    DEFAULT_VENDOR,
    DEFAULT_WAIT_S,
    LAUNCH_SETTLE_S,
    LAUNCHER,
    MPY_CROSS_ABI,
    SERIAL_READ_TIMEOUT_S,
    UPSTREAM,
    VERIFY_TEXT,
    VERIFY_TIMEOUT_S,
    Deadline,
    DeployError,
    Job,
)
from device_repl import Repl, ReplError, connect_repl, run_and_release

# ------------------------------------------------------------ confirmation


def engine_url(engine: str | None) -> str:
    """Where the device should fetch speech from.

    Resolved before the launch, deliberately: after it the console
    belongs to the app, and a bad engine address discovered then costs a
    round trip through the interrupt to get back from.
    """
    try:
        return voicevox_url(engine)
    except ValueError as exc:
        raise DeployError(f"the bundle is installed, but {exc}") from None


def verify_by_speech(
    repl: Repl,
    port: str,
    text: str,
    url: str,
    log: Callable[[str], None],
    settle: float = LAUNCH_SETTLE_S,
) -> Message:
    """Launch what was just installed and have it say `text` out loud.

    Returns the `speak.end` ack. Raises `DeployError` if the device
    would not say it, naming the layer that refused: `speak.say` fails
    when the device cannot reach the engine, and a `speak.end` that is
    not ok means playback started and was cut short.

    Takes the port over. `run_and_release` hands the REPL's own port to
    the link rather than closing and reopening it, so the traceback from
    a failed import is not lost in the gap — and the caller must not
    close the transport afterwards.

    The device is left running the app. Ctrl-C brings it back — see
    `BuddyLink.interrupt` — so this is no longer the dead end it was.
    """
    log(f"launching the app to confirm out loud (engine: {url})")

    link = BuddyLink(port).open(adopt=run_and_release(repl, LAUNCH_SOURCE, DEFAULT_READ_TIMEOUT))

    def report(msgs: Sequence[Message], logs: Sequence[bytes]) -> None:
        """Print what the device said. Both halves: `drain` empties both,
        and a protocol frame dropped without a word — the `hello` the
        transport sends on handshake, or an ack for something nobody
        asked about — is the kind of thing worth seeing."""
        for line in logs:
            log("  dev | " + line.decode("utf-8", errors="replace"))
        for msg in msgs:
            log(f"  <-- {msg}")

    try:
        # Whatever the app says while starting, said here. On a failed
        # import this is the diagnostic, and the request below would
        # otherwise report it as nothing more than a timeout.
        report(*link.pump(settle))
        try:
            # On the panel first: the words are readable while the
            # engine synthesises, which is seconds.
            say(link, text, timeout=VERIFY_TIMEOUT_S, pace=0)
            ack = speak(link, text, url=url, timeout=VERIFY_TIMEOUT_S)
        except (ConnectionError, TimeoutError, RuntimeError, ValueError) as exc:
            raise DeployError(
                f"the bundle is on flash but the device would not say {text!r}: {exc}"
            ) from None
        if not ack.get("ok"):
            raise DeployError(f"playback ended early: {ack}")
        if ack.get("stalls"):
            # Not a failure: the audio played, with gaps. Worth saying,
            # because it means the stream could not keep up with the tick.
            log(f"  the stream stalled {ack['stalls']} times — the utterance will have gapped")
        return ack
    finally:
        report(*link.drain())
        link.close()


# --------------------------------------------------------------------- cli


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("--port", help="Serial port. Not needed with --compile-only.")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument(
        "--compile-only",
        action="store_true",
        help="Compile and stop. Puts the overlay in front of MicroPython's parser, no board.",
    )
    ap.add_argument("--build", type=Path, default=DEFAULT_BUILD, help="Where the .mpy files go.")
    ap.add_argument(
        "--vendor",
        type=Path,
        default=DEFAULT_VENDOR,
        help="Archive of the upstream sources: the only host-side copy once flash is bytecode.",
    )
    ap.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help="Budget for the whole run, in seconds.",
    )
    ap.add_argument(
        "--wait",
        type=float,
        default=DEFAULT_WAIT_S,
        help="Seconds to wait for the REPL if the interrupt does not get us one.",
    )
    ap.add_argument(
        "--no-speak",
        action="store_true",
        help="Skip the spoken confirmation and leave the device at the REPL.",
    )
    ap.add_argument(
        "--speak-text",
        default=VERIFY_TEXT,
        metavar="TEXT",
        help="What the device says once the bundle is installed.",
    )
    ap.add_argument(
        "--engine",
        metavar="URL",
        help="VOICEVOX engine. Defaults to $VOICEVOX_URL, then this machine's LAN address.",
    )
    ap.add_argument(
        "--settle",
        type=float,
        default=LAUNCH_SETTLE_S,
        help="Seconds to read the app's startup output before speaking to it.",
    )
    return ap.parse_args(argv)


def _log(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


def _build(build_dir: Path) -> list[Job]:
    """Compile the overlay, after checking that the ABI still lines up."""
    abi = mpy_cross_abi()
    if abi != MPY_CROSS_ABI:
        raise DeployError(
            f"mpy-cross emits .mpy v{abi}, this bundle is pinned to v{MPY_CROSS_ABI}. "
            "Bytecode does not cross ABI versions: check the board's firmware "
            "and move the pin in pyproject.toml and MPY_CROSS_ABI together."
        )
    _log(f"mpy-cross emits .mpy v{abi}")

    jobs = build_overlay(build_dir)
    for job in jobs:
        _log(f"  compiled {job.origin} -> {job.dest} ({job.size} bytes)")
    _log(f"  compiled device/{LAUNCHER} as a syntax check ({check_launcher(build_dir)} bytes)")
    return jobs


def _compile_upstream(vendor: Path, build_dir: Path) -> int:
    """`--compile-only` の残り: 名前を挙げた peer だけを通しにかける。

    vendor/ の全部ではない。あのディレクトリは flash のスナップショットで、
    ここが push しないファイル — push 対象の隣に main.mpy として置かれては
    困る launcher を含む — も持っているため。
    """
    missing: list[str] = []
    for name in UPSTREAM:
        src = vendor / f"{name}.py"
        if not src.is_file():
            missing.append(name)
            continue
        size = compile_source(src, build_dir / f"{name}.mpy")
        _log(f"  compiled {src} -> {name}.mpy ({size} bytes)")
    if missing:
        _log(f"  not under {vendor}, so not checked: {', '.join(missing)}")
    return 0


def _push(repl: Repl, jobs: list[Job], vendor: Path, build_dir: Path, deadline: Deadline) -> None:
    """バイトコードをデバイスへ載せて、載ったことを確かめる。"""
    device_abi = device_mpy_abi(repl)
    if device_abi != int(MPY_CROSS_ABI.split(".")[0]):
        raise DeployError(
            f"the device loads .mpy v{device_abi}, mpy-cross emits "
            f"v{MPY_CROSS_ABI}. Pushing this would install modules the "
            "firmware refuses to import."
        )

    staged = [*jobs, *stage_upstream(repl, vendor, build_dir, deadline, _log)]
    push_jobs(repl, staged, deadline, _log)
    install_launcher(repl, vendor, deadline, _log)
    prune(repl, vendor, deadline, _log)
    prune_stale(repl, deadline, _log)

    shadows = find_shadows(repl, staged)
    if shadows:
        raise DeployError(
            "source files are still shadowing the bytecode, so the device "
            f"would go on parsing them: {', '.join(shadows)}"
        )
    report_flash(repl, _log)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.compile_only and not args.port:
        _log("--port is required unless --compile-only is given")
        return 2

    deadline = Deadline(args.timeout)
    build_dir: Path = args.build
    vendor: Path = args.vendor

    try:
        jobs = _build(build_dir)
        if args.compile_only:
            return _compile_upstream(vendor, build_dir)

        deadline.check("opening the port")
        repl = connect_repl(
            args.port,
            args.baud,
            timeout=min(args.wait, deadline.remaining()),
            on_wait=lambda: _log("waiting for the REPL — press BtnRST on the device..."),
        )
    except (DeployError, ReplError) as exc:
        _log(str(exc))
        return 1

    # Set by the confirmation, which hands the REPL's port to the link.
    # Closing the transport after that would take the port with it.
    handed_over = False
    try:
        # mpremote leaves this at None, i.e. block forever. Every read in
        # a transfer goes through it, so this is the whole reason the run
        # cannot hang.
        repl.serial.timeout = SERIAL_READ_TIMEOUT_S
        _push(repl, jobs, vendor, build_dir, deadline)

        if args.no_speak:
            _log("done. Launch with: buddy_start_app (MCP) or buddy_bridge --start")
            return 0

        deadline.check("the spoken confirmation")
        url = engine_url(args.engine)
        handed_over = True
        ack = verify_by_speech(repl, args.port, args.speak_text, url, _log, settle=args.settle)
        _log(f"  speak.end: {ack}")
    except (DeployError, ReplError, OSError) as exc:
        _log(str(exc))
        return 1
    finally:
        if not handed_over:
            repl.close()

    _log(
        f"done, and the device said so: {args.speak_text!r}. The app is running now; "
        "the next deploy will interrupt it back to the REPL on its own."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
