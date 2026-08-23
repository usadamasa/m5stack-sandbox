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
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from buddy_link import DEFAULT_READ_TIMEOUT, LAUNCH_SOURCE, BuddyLink
from buddy_verbs import say, speak, voicevox_url
from buddy_wire import Message
from device_repl import (
    Repl,
    ReplError,
    connect_repl,
    run_and_release,
)

# host/tools/src/buddy_deploy.py から 3 つ上。デバイスへ載せるソースは
# workspace member をまたいで device/ の下にあるので、member 単位ではなく
# リポジトリのルートを起点にする必要がある。
REPO = Path(__file__).resolve().parents[3]

DEVICE_ROOT = REPO / "device"

DEST_ROOT = "/flash"

# The .mpy ABI mpy-cross must emit. Bytecode is only portable within one
# ABI, and the board reports 6 (`sys.implementation._mpy & 0xff`) under
# MicroPython 1.27. Pinned here as well as in pyproject.toml because a
# dependency bump is the way this breaks, and the failure on the device
# is an ImportError with nothing pointing back at the host.
MPY_CROSS_ABI = "6.3"

# Modules this repository owns, relative to device/. The `buddy` package
# keeps them together on flash — `/flash/buddy/` is this repository's,
# `/flash/` root is the firmware's and upstream's.
OVERLAY: tuple[str, ...] = (
    # Empty, and pushed anyway: MicroPython has no namespace packages, so
    # without it `/flash/buddy/` is a directory rather than a package and
    # every import below fails.
    "buddy/__init__.py",
    "buddy/serial.py",
    "buddy/chat.py",
    # Shipped but never imported: the app pulls it in only when a `dbg.*`
    # frame arrives and drops it again on `dbg.off`, so it costs flash
    # and no heap. Leaving it off the device would mean the one bundle
    # that cannot be inspected is the one already misbehaving.
    "buddy/debug.py",
    "buddy/speak.py",
    "buddy/tts.py",
    "apps/claude_buddy.py",
)

# Peers that live on the device and come from upstream. Read off flash,
# compiled, pushed back as bytecode. `buddy_ble` is deliberately not
# here: the serial transport never imports it, and the NimBLE stack
# behind it reserves the heap speech needs.
UPSTREAM: tuple[str, ...] = (
    "buddy_chars",
    "buddy_protocol",
    "buddy_state",
    "buddy_ui_cp",
)

# Replaces upstream's launcher. Pushed as source: MicroPython executes
# /flash/main.py directly and never looks for a main.mpy.
LAUNCHER = "main.py"

# Launcher-only or BLE-only. Nothing the serial build imports reaches
# these, and each one is heap the app would otherwise never get back.
REMOVE: tuple[str, ...] = (
    "burst_frames.py",
    "buddy_ble.py",
    "buddy_ble.mpy",
    "apps/snake.py",
    "apps/hello_cardputer.py",
)

# Where OVERLAY used to land, before the `buddy` package existed. Deleted
# rather than archived: these are this repository's own modules and git
# has them. Left behind they are flash nothing imports, and — worse — a
# stale copy that an old `sys.path` entry could still resolve.
STALE: tuple[str, ...] = (
    "buddy_serial.mpy",
    "buddy_chat.mpy",
    "buddy_debug.mpy",
    "buddy_speak.mpy",
    "buddy_tts.mpy",
    "buddy_serial.py",
    "buddy_chat.py",
    "buddy_debug.py",
    "buddy_speak.py",
    "buddy_tts.py",
)

# Build output. tmp/ is the scratch directory and may be wiped at any
# time, which is fine — everything in here is regenerated from source.
DEFAULT_BUILD = REPO / "tmp" / "mpy"

# Upstream sources pulled off the device. Not scratch: for the modules
# this repository does not carry, this is the only copy on the host once
# flash holds bytecode. Untracked, because redistributing them is
# exactly what the NOTICE says this repository does not do.
DEFAULT_VENDOR = REPO / "vendor" / "device"

# Whole-run budget. Generous: a full deploy is nine transfers over a
# 115200 line plus however long it takes a human to reach the reset
# button.
DEFAULT_TIMEOUT_S = 300.0

# How long to wait for the REPL when the interrupt does not get us one.
# Long enough to reach over and press BtnRST, short enough that an
# unattended device fails the command.
DEFAULT_WAIT_S = 45.0

# Read timeout on the port once it is open. mpremote's own default is
# None, i.e. block forever; this is what turns a device that stopped
# answering into an error instead of a wedged process.
SERIAL_READ_TIMEOUT_S = 5.0

# mpy-cross on five small modules is milliseconds. This only exists so
# that a wedged child cannot outlive the budget it was checked against.
COMPILE_TIMEOUT_S = 60.0

# What the device says once the bundle is on flash. Short on purpose:
# the panel holds four rows of nine wide glyphs, and synthesis is the
# slow part of the round trip.
VERIFY_TEXT = "デプロイ完了なのだ"

# Seconds to let the app talk after the launch before anything is asked
# of it. The same settle `buddy_bridge --start` uses: a failed import
# prints its traceback in this window, and reading it is how the run
# gets to say what went wrong instead of just timing out.
LAUNCH_SETTLE_S = 4.0

# Per-request patience once the app is up. Synthesis has its own, much
# longer budget inside `buddy_verbs.speak`.
VERIFY_TIMEOUT_S = 10.0


class DeployError(RuntimeError):
    """The deploy cannot go ahead. `ReplError` means the link; this means us."""


class DeployTimeout(DeployError):
    """The budget ran out. A distinct type so the message can say so."""


class Deadline:
    """A budget for the whole run, checked between steps.

    Steps are not interruptible — mpremote's transfers either complete
    or raise on the port's read timeout — so this cannot cut one short.
    What it does is stop the run from starting a step it has no time
    left for, and name the step it stopped at.
    """

    def __init__(self, budget: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.budget = budget
        self._clock = clock
        self._end = clock() + budget

    def remaining(self) -> float:
        return self._end - self._clock()

    def check(self, step: str) -> None:
        if self.remaining() <= 0.0:
            raise DeployTimeout(
                f"the {self.budget:.0f}s budget ran out before: {step}. "
                "Raise --timeout if the link is just slow; if it is not, the "
                "device stopped answering."
            )


@dataclass(frozen=True)
class Job:
    """One compiled module, ready to push.

    `origin` is only for the log, and it earns its place: "which copy of
    buddy_ui_cp did this run compile" is the first question when the app
    starts behaving like a different version.
    """

    built: Path
    dest: str
    origin: str
    size: int

    @property
    def shadow(self) -> str:
        """The source path on the device that would hide this bytecode."""
        return f"{DEST_ROOT}/{self.dest[: -len('.mpy')]}.py"


# ---------------------------------------------------------------- mpy-cross


def _mpy_cross_binary() -> str:
    try:
        # PyPI 版の mpy-cross に型情報が無く、stub パッケージも存在しない。
        import mpy_cross  # pyright: ignore[reportMissingTypeStubs]
    except ImportError:
        raise DeployError(
            "mpy-cross is not installed. It is in the dev dependency group: run `uv sync`."
        ) from None
    return str(mpy_cross.mpy_cross)


def mpy_cross_abi(binary: str | None = None) -> str:
    """The `.mpy` ABI this mpy-cross emits, as "<version>.<sub-version>"."""
    text = _run([binary or _mpy_cross_binary(), "--version"], "mpy-cross --version")
    match = re.search(r"mpy v(\d+\.\d+)", text)
    if match is None:
        raise DeployError(f"mpy-cross did not report an .mpy version: {text.strip()!r}")
    return match.group(1)


def compile_source(src: Path, out: Path, binary: str | None = None) -> int:
    """Compile one module. Returns the size of the bytecode written."""
    out.parent.mkdir(parents=True, exist_ok=True)
    _run([binary or _mpy_cross_binary(), "-o", str(out), str(src)], f"mpy-cross on {src}")
    return out.stat().st_size


def _run(argv: list[str], what: str) -> str:
    try:
        res = subprocess.run(
            argv, capture_output=True, text=True, check=False, timeout=COMPILE_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        raise DeployError(f"{what} did not finish within {COMPILE_TIMEOUT_S:.0f}s") from None
    if res.returncode:
        raise DeployError(f"{what} failed: {(res.stderr or res.stdout).strip()}")
    # --version goes to stdout on some builds and stderr on others.
    return res.stdout + res.stderr


def mpy_abi_of(data: bytes) -> int:
    """The `.mpy` format version out of a compiled module's header.

    Byte 0 is 'M' and byte 1 is the version. The remaining header bytes
    encode the sub-version, feature flags and native architecture; none
    of them matter here, because bytecode-only output carries no native
    code and mpy-cross leaves that byte at zero.
    """
    if len(data) < 4 or data[0:1] != b"M":
        raise DeployError("not a .mpy file: the 'M' magic is missing")
    return data[1]


def device_mpy_abi(repl: Repl) -> int:
    """The `.mpy` version the firmware on the far end will load.

    `sys.implementation._mpy` packs the version into the low byte and
    the sub-version and native arch above it. Only the low byte has to
    match what we emit, since we emit no native code.
    """
    repl.exec("import sys")
    return int(repl.eval("sys.implementation._mpy")) & 0xFF


# ------------------------------------------------------------------- build


def build_overlay(build_dir: Path, src_dir: Path | None = None) -> list[Job]:
    """Compile the modules this repository owns.

    Runs before the port is opened. A syntax error in the overlay should
    not be discovered halfway through rewriting flash.
    """
    src_dir = src_dir if src_dir is not None else DEVICE_ROOT
    jobs: list[Job] = []
    for rel in OVERLAY:
        src = src_dir / rel
        if not src.is_file():
            raise DeployError(f"missing overlay source: {src}")
        stem = rel[: -len(".py")]
        out = build_dir / f"{stem}.mpy"
        jobs.append(Job(out, f"{stem}.mpy", str(src.relative_to(REPO)), compile_source(src, out)))
    return jobs


def check_launcher(build_dir: Path, src_dir: Path | None = None) -> int:
    """Compile device/main.py purely to have MicroPython's parser see it.

    The result is never pushed — the launcher has to stay source — so it
    lands in a directory of its own rather than next to the modules that
    are, where a stray `main.mpy` would be an invitation.
    """
    src_dir = src_dir if src_dir is not None else DEVICE_ROOT
    src = src_dir / LAUNCHER
    if not src.is_file():
        raise DeployError(f"missing launcher source: {src}")
    return compile_source(src, build_dir / "syntax-check" / "main.mpy")


# ---------------------------------------------------------------- transfer


def push_file(repl: Repl, src: Path, dest: str, *, quiet: bool = False) -> int:
    """Copy `src` to `/flash/<dest>`. Returns the size the device reports.

    What is left of `buddy/scripts/push.py`, which this repository used
    to borrow from the upstream clone and then reimplemented over paste
    mode (Apache-2.0, see NOTICE). `mpremote` owns the transfer now — see
    host/device_repl.py for why — so what remains is the part specific to
    this install layout: peer modules at `/flash/`, apps at
    `/flash/apps/`, hence the one directory level that gets created.
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


# ------------------------------------------------------------------ device


def _archive(vendor: Path, name: str, data: bytes) -> Path:
    path = vendor / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def stage_upstream(
    repl: Repl, vendor: Path, build_dir: Path, deadline: Deadline, log: Callable[[str], None]
) -> list[Job]:
    """Get every upstream peer onto the device as bytecode.

    Three states, because a deploy is run more than once:

      - source still on flash: read it, archive it, compile it, push it.
      - already converted: nothing to do, and saying so is the point —
        silence would look the same as a module that got skipped.
      - neither source nor bytecode: fall back to the archive, and if
        that is empty too, say which module and how to get it back
        rather than pushing a half-installed bundle.
    """
    jobs: list[Job] = []
    for name in UPSTREAM:
        deadline.check(f"staging {name}")
        on_device = f"{DEST_ROOT}/{name}.py"
        cached = vendor / f"{name}.py"

        if repl.fs_exists(on_device):
            _archive(vendor, f"{name}.py", bytes(repl.fs_readfile(on_device)))
            log(f"  archived {name}.py from flash -> {cached}")
        elif repl.fs_exists(f"{DEST_ROOT}/{name}.mpy"):
            log(f"  {name}: already bytecode on flash, left alone")
            continue
        elif not cached.is_file():
            raise DeployError(
                f"{name} is on neither the device nor in {vendor}. It comes from "
                "upstream, not from this repository — reinstall the bundle with "
                "the m5-onboard skill and run this again."
            )

        out = build_dir / f"{name}.mpy"
        jobs.append(Job(out, f"{name}.mpy", str(cached), compile_source(cached, out)))
    return jobs


def push_jobs(
    repl: Repl, jobs: Sequence[Job], deadline: Deadline, log: Callable[[str], None]
) -> None:
    """Push each module, then delete the source that would shadow it."""
    for job in jobs:
        deadline.check(f"pushing {job.dest}")
        push_file(repl, job.built, job.dest, quiet=True)
        note = ""
        if repl.fs_exists(job.shadow):
            repl.fs_rmfile(job.shadow)
            note = f", removed {job.shadow}"
        log(f"  {job.dest}: {job.size} bytes from {job.origin}{note}")


def install_launcher(
    repl: Repl,
    vendor: Path,
    deadline: Deadline,
    log: Callable[[str], None],
    src_dir: Path | None = None,
) -> None:
    """Replace the launcher, keeping upstream's if this is the first run."""
    src_dir = src_dir if src_dir is not None else DEVICE_ROOT
    deadline.check("installing the launcher")
    ours = (src_dir / LAUNCHER).read_bytes()
    target = f"{DEST_ROOT}/{LAUNCHER}"

    if repl.fs_exists(target):
        current = bytes(repl.fs_readfile(target))
        # Only upstream's launcher is worth keeping, and after the first
        # run the file on flash is ours. Comparing is what stops a second
        # run from overwriting the archive with a copy of device/main.py.
        if current != ours and not (vendor / LAUNCHER).is_file():
            _archive(vendor, LAUNCHER, current)
            log(f"  archived the upstream launcher -> {vendor / LAUNCHER}")

    push_file(repl, src_dir / LAUNCHER, LAUNCHER, quiet=True)
    log(f"  {LAUNCHER}: {len(ours)} bytes (source, not bytecode)")


def prune(repl: Repl, vendor: Path, deadline: Deadline, log: Callable[[str], None]) -> None:
    """Delete what the serial build never imports, archiving it first."""
    for rel in REMOVE:
        deadline.check(f"removing {rel}")
        target = f"{DEST_ROOT}/{rel}"
        if not repl.fs_exists(target):
            continue
        if not (vendor / rel).is_file():
            _archive(vendor, rel, bytes(repl.fs_readfile(target)))
        repl.fs_rmfile(target)
        log(f"  removed {rel} (archived under {vendor})")


def prune_stale(repl: Repl, deadline: Deadline, log: Callable[[str], None]) -> None:
    """Delete what an older layout left on flash. See `STALE`."""
    for rel in STALE:
        deadline.check(f"removing {rel}")
        target = f"{DEST_ROOT}/{rel}"
        if not repl.fs_exists(target):
            continue
        repl.fs_rmfile(target)
        log(f"  removed {rel} (moved into the buddy package)")


def find_shadows(repl: Repl, jobs: Sequence[Job]) -> list[str]:
    """Sources still hiding the bytecode next to them.

    The deploy is not done while one of these exists: the device would
    keep parsing source, the heap would stay where it was, and every
    visible sign would say the push succeeded.
    """
    return [job.shadow for job in jobs if repl.fs_exists(job.shadow)]


def report_flash(repl: Repl, log: Callable[[str], None]) -> None:
    for path in (DEST_ROOT, f"{DEST_ROOT}/buddy", f"{DEST_ROOT}/apps"):
        try:
            names = sorted(entry.name for entry in repl.fs_listdir(path))
        except OSError as exc:
            # Said rather than skipped. A missing /flash/apps means the
            # app is not where the launcher looks for it, and a silent
            # gap in this listing reads as an empty directory.
            log(f"  {path}: could not be listed: {exc}")
            continue
        log(f"  {path}: {', '.join(names)}")


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
