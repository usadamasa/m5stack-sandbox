"""flash を書き換える側。ここから先はデバイスが要る。

転送、upstream の退避と変換、launcher の入れ替え、要らなくなったものの
削除、そして「本当に載ったか」の確認。どれも `Deadline` を渡されて、
残り時間が無ければ手を付ける前に止まる。

消す前に必ず `vendor/` へ写す。upstream のモジュールはこのリポジトリが
持っておらず、消してしまえば手元の唯一の控えが消える。
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from deploy_build import compile_source
from deploy_spec import (
    DEST_ROOT,
    DEVICE_ROOT,
    LAUNCHER,
    REMOVE,
    STALE,
    UPSTREAM,
    Deadline,
    DeployError,
    Job,
)
from device_repl import Repl, ReplError


def device_mpy_abi(repl: Repl) -> int:
    """The `.mpy` version the firmware on the far end will load.

    `sys.implementation._mpy` packs the version into the low byte and
    the sub-version and native arch above it. Only the low byte has to
    match what we emit, since we emit no native code.
    """
    repl.exec("import sys")
    return int(repl.eval("sys.implementation._mpy")) & 0xFF


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
