"""Cardputer-Adv のローカルエミュレータ。実機を挿さずに画面と操作を見る。

`buddy.app.run()` を CPython でそのまま回す。差し替えるのは `run()` の中で
import されるファームウェアの境界だけで、`device/tests/device_fakes.py` が
テストでやっているのと同じ手 (`sys.modules` へ stand-in を置く)。違いは
stand-in が record するだけでなく本当に描くこと — `M5.Lcd` は Pillow の
240x135 の画像に描き、ホストは WiFi と同じ TCP (`buddy/netlink.py`) で繋ぐ。
基板側の stand-in は `emu_board.py` にある。

    uv run python host/tools/src/buddy_emu.py            # 8788 で listen、画面は tmp/emu/screen.png
    uv run python host/link/src/buddy_bridge.py --port tcp://127.0.0.1 --status
    BUDDY_PORT=tcp://127.0.0.1 buddy-mcpd start          # daemon ごと向ける

テストからは `Emulator(port=0).start()` で in-process に回し、
`BuddyLink("tcp://127.0.0.1:<port>")` で繋ぐ (`tests/test_buddy_emu.py`)。

### 本物でないもの

- upstream のピア (`buddy_ui_cp` / `buddy_state` / `buddy_chars` /
  `buddy_protocol`) は flash にしか無く再配布もしない (NOTICE) ので、ここでは
  形だけの stand-in。dashboard の chrome は見た目を寄せただけで、`status` の
  ack も最小限。ponytail: `vendor/device/*.py` が手元にあるなら sys.path に
  足して本物を使う枝が次の段
- USB は無い。`buddy.serial` は stdin を `select.poll` に登録するが、pytest の
  stdin には fileno が無いので、ここでは繋がらない transport の stub にする。
  REPL を要る操作 (`--start` / `--interrupt` / deploy) は `tcp://` と同じく対象外
- 書体の高さは `buddy/chat_font.py` の実測値に固定する (EFontJA24 27 /
  DejaVu12 16 / DejaVu9 15 / VLW 18)。行数が実機と揃うのはこのため。幅は
  `--font` の TTF で測る。無ければ Pillow の既定書体で、日本語は豆腐になる
- VLW は flash に置いてある前提 (README の「チャットパネルの日本語フォント」)。
  `os.stat` を通すためにこのファイル自身のパスを渡し、`loadFont` は名前だけ見る
- Speaker は受け取ったブロックを数えるだけで、音は出ない。VOICEVOX へは
  `requests` が無いので届かず、`speak.say` は ack で断られる
- 入力は無い。実機と同じで、キーボードは読まない
"""

from __future__ import annotations

import argparse
import functools
import gc
import socket
import sys
import threading
import time
import traceback
import types
from collections.abc import Callable
from pathlib import Path
from typing import cast

from PIL import Image

from deploy_spec import DEVICE_ROOT, REPO
from emu_board import BLACK, GRAY_DIM, BuddyProtocol, BuddyState, BuddyUI, Lcd, NoSerial, Speaker

# device/ は package ではない (flash の写し) ので、path に足してから import する。
if str(DEVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(DEVICE_ROOT))

from buddy import app as buddy_app
from buddy import chat as buddy_chat
from buddy import netlink as buddy_netlink
from buddy.netlink import BuddyNet

# `--font` が無いときに探す CJK の書体。README の手順が置く場所と、macOS の内蔵。
_FONT_CANDIDATES = (
    REPO / "tmp" / "fonts" / "BIZUDGothic-Regular.ttf",
    Path("/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc"),
)

# 包む前の本物。2 つ目の Emulator が包みを包まないよう、import 時に 1 度だけ取る。
_REAL_NET = BuddyNet
_REAL_PANEL = buddy_chat.ChatPanel

LineCallback = Callable[[bytes], None]
StateCallback = Callable[[str], None]


# ---------------------------------------------------------------- shim


def _module(name: str, **attrs: object) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _ticks_ms() -> int:
    return int(time.monotonic() * 1000)


def _ticks_diff(a: int, b: int) -> int:
    return a - b


def _ticks_add(a: int, b: int) -> int:
    return a + b


def _print_exception(e: BaseException) -> None:
    traceback.print_exception(e)


def _noop(*_args: object) -> None:
    return None


def _find_font() -> Path | None:
    return next((p for p in _FONT_CANDIDATES if p.exists()), None)


# ---------------------------------------------------------------- エミュレータ


class Emulator:
    """`buddy.app.run()` を別スレッドで回し、画面と listen ポートを見せる。

    プロセスに 1 つ。stand-in は `sys.modules` に、shim は本物の `time` / `gc` /
    `sys` に足すので、2 つ目を作ると 1 つ目の LCD が差し替わる。
    """

    def __init__(self, port: int = 8788, font: Path | None = None) -> None:
        self.lcd = Lcd(font if font is not None else _find_font())
        self.speaker = Speaker()
        self.reset_requested = False
        self._port = port
        self._bound: int | None = None
        self._stop = threading.Event()
        self._interrupted = False
        self._thread: threading.Thread | None = None

    # -- 見る

    @property
    def port(self) -> int:
        if self._bound is None:
            raise RuntimeError("not started")
        return self._bound

    @property
    def screen(self) -> Image.Image:
        return self.lcd.snapshot()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.screen.save(path)

    def wait_drawn(self, text: str, timeout: float = 3.0) -> None:
        """`text` を含む drawString が来るまで待つ。ack は描画より先に返る。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(text in drawn for drawn, _x, _y, _c in self.lcd.drawn):
                return
            time.sleep(0.02)
        raise TimeoutError(f"{text!r} was not drawn within {timeout}s")

    # -- 回す

    def start(self) -> Emulator:
        self._install()
        self._thread = threading.Thread(target=buddy_app.run, name="buddy-emu", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 5.0
        while self._bound is None and self._thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        if self._bound is None:
            raise RuntimeError("the app did not bring its listener up")
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5.0)

    def _sleep_ms(self, ms: int) -> None:
        # main loop が毎 tick ここを通る。外から KeyboardInterrupt を投げ込む
        # 唯一の口で、Ctrl-C と同じ経路 (`_shutdown(to_repl=True)`) で畳ませる。
        # 投げるのは 1 度だけ。`_shutdown` 自身もここで待つ。
        if self._stop.is_set() and not self._interrupted:
            self._interrupted = True
            raise KeyboardInterrupt
        time.sleep(ms / 1000)

    def _net(self, on_line: LineCallback, on_state: StateCallback) -> BuddyNet:
        made = _REAL_NET(on_line=on_line, on_state=on_state, port=self._port)
        # port=0 で bind したときの実際の番号。`_ls` の型注釈は device 側の
        # `Listener` Protocol で、そちらには getsockname が無い (実機で要らない)。
        listener = cast(socket.socket, made._ls)  # pyright: ignore[reportPrivateUsage]
        self._bound = listener.getsockname()[1]
        return made

    def _reset(self) -> None:
        # 実機は reboot する。ここでは覚えておくだけで、スレッドは静かに終わる。
        self.reset_requested = True

    def _install(self) -> None:
        import buddy

        # MicroPython にしか無いもの。本物のモジュールに足す。
        time.ticks_ms = _ticks_ms  # pyright: ignore[reportAttributeAccessIssue]
        time.ticks_diff = _ticks_diff  # pyright: ignore[reportAttributeAccessIssue]
        time.ticks_add = _ticks_add  # pyright: ignore[reportAttributeAccessIssue]
        time.sleep_ms = self._sleep_ms  # pyright: ignore[reportAttributeAccessIssue]
        gc.mem_free = lambda: 60_000  # pyright: ignore[reportAttributeAccessIssue]
        gc.mem_alloc = lambda: 100_000  # pyright: ignore[reportAttributeAccessIssue]
        gc.threshold = _noop  # pyright: ignore[reportAttributeAccessIssue]
        sys.print_exception = _print_exception  # pyright: ignore[reportAttributeAccessIssue]

        lcd = self.lcd
        sys.modules.update(
            {
                "M5": _module("M5", Lcd=lcd, Speaker=self.speaker, begin=_noop),
                # reset_cause 1 = PWRON。エミュレータは毎回電源を入れたところから。
                "machine": _module("machine", reset=self._reset, reset_cause=lambda: 1),
                "micropython": _module("micropython", kbd_intr=_noop, mem_info=_noop),
                "buddy_ui_cp": _module(
                    "buddy_ui_cp", BuddyUI=lambda: BuddyUI(lcd), BLACK=BLACK, GRAY_DIM=GRAY_DIM
                ),
                "buddy_state": _module("buddy_state", BuddyState=BuddyState),
                "buddy_chars": _module(
                    "buddy_chars", sweep_partials=_noop, CharReceiver=lambda: None
                ),
                "buddy_protocol": _module("buddy_protocol", BuddyProtocol=BuddyProtocol),
            }
        )
        no_serial = _module("buddy.serial", BuddySerial=NoSerial)
        sys.modules["buddy.serial"] = no_serial
        buddy.serial = no_serial  # pyright: ignore[reportAttributeAccessIssue]

        # `run()` は引数無しで組み立てるので、既定値を差し替えるにはクラスを包む。
        buddy_netlink.BuddyNet = self._net
        buddy_chat.ChatPanel = functools.partial(_REAL_PANEL, vlw_path=__file__)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the Cardputer app on this machine.")
    ap.add_argument("--port", type=int, default=8788, help="TCP port to listen on (0 = any).")
    ap.add_argument("--font", type=Path, default=None, help="TTF/TTC for text width and the PNG.")
    ap.add_argument("--screen", type=Path, default=REPO / "tmp" / "emu" / "screen.png")
    args = ap.parse_args(argv)

    emu = Emulator(port=args.port, font=args.font).start()
    print(f"buddy_emu: tcp://127.0.0.1:{emu.port}  screen -> {args.screen}")
    seen = -1
    try:
        while not emu.reset_requested:
            if emu.lcd.frame != seen:
                seen = emu.lcd.frame
                emu.save(args.screen)
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        emu.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
