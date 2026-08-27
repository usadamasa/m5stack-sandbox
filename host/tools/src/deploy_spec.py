"""何をどこへ置くか、と、run 全体の型。

`buddy_deploy` から切り出した。ビルド (`deploy_build`) もデバイス操作
(`deploy_device`) も CLI もここを見るので、ここは誰も見ない — 依存は
一方向に保つ。

デバイスがソースを読まない理由と、この overlay が何を置き換えているかは
`buddy_deploy` の docstring にある。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# host/tools/src/deploy_spec.py から 3 つ上。デバイスへ載せるソースは
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
    # アプリ本体。`apps/claude_buddy.py` は sys.path を整えてここへ橋を渡す
    # だけの起動口で、組み立てと main loop はこちら、届いた 1 行の振り分けは
    # router にある。
    "buddy/app.py",
    "buddy/router.py",
    "buddy/serial.py",
    # チャットパネル。パネルの幾何と verb の振り分けが `chat`、書体の選択と
    # 計測が `chat_font`、transcript の保持が `chat_log`、行の折り返しが
    # `chat_wrap`。4 つとも `chat` から import されるので、揃っていないと
    # 起動時に ImportError になる。
    "buddy/chat.py",
    "buddy/chat_font.py",
    "buddy/chat_log.py",
    "buddy/chat_wrap.py",
    # Shipped but never imported: the app pulls it in only when a `dbg.*`
    # frame arrives and drops it again on `dbg.off`, so it costs flash
    # and no heap. Leaving it off the device would mean the one bundle
    # that cannot be inspected is the one already misbehaving.
    "buddy/debug.py",
    # 発話。ライフサイクルと `speak.*` verb の振り分けが `speak`、speaker へ
    # ブロックを渡すのが `speak_out`、socket から 2 KiB のブロックを貯めて
    # 渡すのが `speak_stream`。後ろ 2 つとも `speak` が import するので、
    # 載せ忘れると実機では ImportError になる。
    "buddy/speak.py",
    "buddy/speak_out.py",
    "buddy/speak_stream.py",
    "buddy/tts.py",
    # `buddy/tts.py` から切り出した、engine が返した WAV を解いて samples の
    # 頭を見つけるところ。tts が import するので、載せ忘れると実機では
    # ImportError になる。
    "buddy/wav.py",
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
