"""mpy-cross を呼んで overlay をバイトコードにする。

ポートを開く前に済む仕事だけがここにある。overlay の syntax error を
flash を書き換えている途中で見つけるのは遅すぎるので、ビルドが先に来る。

ABI のピンが要る理由は `deploy_spec.MPY_CROSS_ABI` にある。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from deploy_spec import COMPILE_TIMEOUT_S, DEVICE_ROOT, LAUNCHER, OVERLAY, REPO, DeployError, Job


def _mpy_cross_binary() -> str:
    try:
        # PyPI 版の mpy-cross に型情報が無く、stub パッケージも存在しない。
        # 触る面だけを写した手書きのスタブが `host/tools/typings/` にある。
        import mpy_cross
    except ImportError:
        raise DeployError(
            "mpy-cross is not installed. It is in the dev dependency group: run `uv sync`."
        ) from None
    return str(mpy_cross.mpy_cross)


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
