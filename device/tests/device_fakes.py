"""アプリが触る相手の fake。`test_app.py` と `test_router.py` が使う。

デバイス側のコードが要求するものは 3 種類ある。ファームウェアのモジュール
(`M5` / `machine`)、flash にしか無い upstream のピア (`buddy_ui_cp` /
`buddy_state` / `buddy_chars` / `buddy_protocol`)、そしてこのリポジトリ自身の
`buddy.chat` / `buddy.serial` / `buddy.speak`。どれもホストには無いか、実機を
要るかのどちらかなので、ここで record するだけのものに置き換える。

`buddy/app.py` が import するのは `run()` の中だけなので、差し替えるのは
`sys.modules` へ置く 1 手で済む。`buddy/router.py` の方は何も import せず、
渡されたものを呼ぶだけなので、そちらは fake を渡すだけでいい。
"""

import json
import types
from collections.abc import Callable

from buddy.router import Router

# トランスポートが受け取る 2 つのコールバックと、`poll()` が何を配るかを
# テストが決めるための関数。
LineCallback = Callable[[bytes], None]
StateCallback = Callable[[str], None]
Poll = Callable[["FakeTransport", int], None]


class FakeGc:
    """MicroPython の gc。`mem_free` / `mem_alloc` / `threshold` は向こうの追加。"""

    def __init__(self) -> None:
        self.collects = 0
        self.thresholds: list[int] = []

    def collect(self) -> None:
        self.collects += 1

    def mem_free(self) -> int:
        return 60_000

    def mem_alloc(self) -> int:
        return 100_000

    def threshold(self, value: int) -> None:
        self.thresholds.append(value)


class FakeTime:
    """`ticks_ms` / `ticks_diff` / `sleep_ms` は MicroPython のもの。

    `sleep_ms` が時計を footer の周期 (3 秒) より大きく進める。実時間を
    待たずに、ループ 1 周で周期の枝を踏むため。
    """

    def __init__(self) -> None:
        self.now = 0
        self.slept: list[int] = []

    def ticks_ms(self) -> int:
        return self.now

    def ticks_diff(self, a: int, b: int) -> int:
        return a - b

    def sleep_ms(self, ms: int) -> None:
        self.slept.append(ms)
        self.now += 4_000


class Recorder:
    """呼ばれた名前と引数を並べておくだけの土台。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def record(self, name: str, *args: object) -> None:
        self.calls.append((name, args))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class FakeUi(Recorder):
    """`buddy_ui_cp.BuddyUI`。`_redraw_chrome` は持たない — 無いときの枝が既定。"""

    BLACK = 0x000000
    GRAY_DIM = 0x777777

    def update_identity(self, name: str, owner: str) -> None:
        self.record("update_identity", name, owner)

    def update_footer(self, stats: dict[str, object], battery: dict[str, object]) -> None:
        self.record("update_footer", stats, battery)

    def set_connection(self, state: str) -> None:
        self.record("set_connection", state)

    def update_heartbeat(self, payload: dict[str, object]) -> None:
        self.record("update_heartbeat", payload)

    def restore_button_hints(self) -> None:
        self.record("restore_button_hints")


class FakeChat(Recorder):
    """ChatPanel。ack を返した行が `chat.clear` なら panel を返す。"""

    def __init__(self) -> None:
        super().__init__()
        self.active = False
        self.ack: dict[str, object] | None = {"ack": "chat.say", "ok": True}

    def handle_raw(self, raw: bytes) -> dict[str, object] | None:
        self.record("handle_raw", raw)
        if self.ack is None:
            return None
        self.active = b"clear" not in raw
        return self.ack

    def render(self) -> None:
        self.record("render")

    def info(self) -> dict[str, object]:
        return {"vlw": False}


class FakeSpeech(Recorder):
    def __init__(self) -> None:
        super().__init__()
        self.active = False
        self.ack: dict[str, object] | None = {"ack": "speak.say", "ok": True}

    def handle_raw(self, raw: bytes) -> dict[str, object] | None:
        self.record("handle_raw", raw)
        return self.ack

    def pump(self) -> None:
        self.record("pump")

    def stop(self) -> None:
        self.record("stop")


class FakeProto(Recorder):
    def on_line(self, raw: bytes) -> None:
        self.record("on_line", raw)

    def send_hello(self) -> None:
        self.record("send_hello")


class FakeState(Recorder):
    name = "Buddy"
    owner = "usadamasa"

    def stats(self) -> dict[str, object]:
        return {"naps": 0}

    def tick_nap(self) -> None:
        self.record("tick_nap")


class FakeBle(Recorder):
    """BuddySerial のうち、ack を返す口だけ。"""

    advertised_name = "Claude_serial"

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[bytes] = []

    def send_line(self, data: bytes) -> None:
        self.lines.append(data)

    def acks(self) -> list[dict[str, object]]:
        return [json.loads(line.decode("utf-8")) for line in self.lines]


class FakeLcd(Recorder):
    """`M5.Lcd`。名前は M5GFX のものなので camelCase のまま。"""

    def fillScreen(self, colour: int) -> None:
        self.record("fillScreen", colour)

    def setTextColor(self, fg: int, bg: int) -> None:
        self.record("setTextColor", fg, bg)

    def drawString(self, text: str, x: int, y: int) -> None:
        self.record("drawString", text, x, y)


class FakeTransport(FakeBle):
    """`poll()` で何を配るかをテストが決められる BuddySerial。

    実機のトランスポートと同じで、コールバックが呼ばれるのは `poll()` の
    中だけ。ループを止めるのもここから — `poll` に渡した関数が
    KeyboardInterrupt を投げれば、Ctrl-C と同じ経路を通る。
    """

    def __init__(self, on_line: LineCallback, on_state: StateCallback, poll: Poll) -> None:
        super().__init__()
        self.on_line = on_line
        self.on_state = on_state
        self._poll = poll
        self.polls = 0
        self.deinits = 0

    def poll(self) -> None:
        self.polls += 1
        self._poll(self, self.polls)

    def deinit(self) -> None:
        self.deinits += 1


class FakeSerialModule:
    """`buddy.serial` の代わり。組み立てられたトランスポートを掴んでおく。"""

    def __init__(self, poll: Poll) -> None:
        self._poll = poll
        self.made: FakeTransport | None = None

    def BuddySerial(self, on_line: LineCallback, on_state: StateCallback) -> FakeTransport:
        self.made = FakeTransport(on_line, on_state, self._poll)
        return self.made


def firmware_modules(
    ui: FakeUi, state: FakeState, lcd: FakeLcd, machine: Recorder
) -> dict[str, types.ModuleType]:
    """`run()` が import するもの。`sys.modules` へそのまま置ける形で返す。"""

    def module(name: str, **attrs: object) -> types.ModuleType:
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        return mod

    def protocol(**_kw: object) -> FakeProto:
        # BuddyProtocol は名前付き引数で組み立てられる。中身は見ない。
        return FakeProto()

    chars = Recorder()
    return {
        "M5": module("M5", Lcd=lcd),
        "machine": module("machine", reset=lambda: machine.record("reset")),
        "buddy_ui_cp": module(
            "buddy_ui_cp", BuddyUI=lambda: ui, BLACK=FakeUi.BLACK, GRAY_DIM=FakeUi.GRAY_DIM
        ),
        "buddy_state": module("buddy_state", BuddyState=lambda: state),
        "buddy_chars": module(
            "buddy_chars",
            sweep_partials=lambda: chars.record("sweep_partials"),
            CharReceiver=lambda: chars,
        ),
        "buddy_protocol": module("buddy_protocol", BuddyProtocol=protocol),
    }


def make_router(**bound: object) -> Router:
    """振り分けだけを見るための Router。差さっていないものは既定の fake。"""
    router = Router(FakeUi(), bound.get("chat") or FakeChat(), FakeState(), Recorder())
    router.ble = bound.get("ble") or FakeBle()
    router.speech = bound.get("speech") or FakeSpeech()
    router.proto = bound.get("proto") or FakeProto()
    return router
