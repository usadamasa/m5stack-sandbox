"""chatter のテストが共有する stub と組み立て。

実機もネットワークも要らない。デバイスは stub、クロックは変数、台詞の出所は
リスト。喋る側 (`test_chatter`) とペーシング (`test_chatter_pace`) の両方が
同じ `build()` から `ChatterService` を起こす — 継ぎ目が変わっても、テストが
見ている振る舞いが同じであることをここが担保する。
"""

import random
import threading
import time
from collections.abc import Sequence
from typing import Any

from buddy_chatter import ChatterService
from buddy_wire import Message
from chatter_core import ChatterConfig, Event


class StubLink:
    """何にでも答え、何を言われたかを記録するデバイス。"""

    def __init__(self, connected: bool = True) -> None:
        self._connected = connected
        self.said: list[str] = []
        self.spoke: list[str] = []
        self.fail_with: Exception | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    def request(self, obj: Message, expect: str, timeout: float = 5.0) -> Message:
        if self.fail_with is not None:
            raise self.fail_with
        if expect == "chat.say":
            self.said.append(str(obj["text"]))
            return {"ack": "chat.say", "ok": True}
        if expect == "speak.say":
            self.spoke.append(str(obj["text"]))
            # `bytes` と `rate` は speak() が再生のタイムアウトへ変える値。
            # 何も待たされないように小さくしておく。
            return {"ack": "speak.say", "ok": True, "bytes": 32, "rate": 16000}
        return {"ack": expect, "ok": True}

    def await_ack(self, expect: str, timeout: float = 5.0) -> Message:
        return {"ack": expect, "ok": True, "stalls": 0}


class ListSource:
    """決め打ちの台詞を配り、何本取られたかを数える。"""

    def __init__(self, lines: list[str] | None = None) -> None:
        self.lines = lines if lines is not None else [f"line {i} なのだ" for i in range(50)]
        self.calls = 0
        self.contexts: list[list[Event]] = []

    def next_line(self, context: Sequence[Event]) -> str | None:
        self.calls += 1
        self.contexts.append(list(context))
        return self.lines.pop(0) if self.lines else None


class Clock:
    """テストが言ったときにだけ進む monotonic クロック。"""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class PinnedRandom(random.Random):
    """求められた範囲の真ん中へ固定したジッター。

    activity のテストは 1 回の抽選がしきい値のどちら側へ落ちるかを論じる。
    本物の生成器ではそれがコイン投げになってしまう。
    """

    def random(self) -> float:
        return 0.5

    def uniform(self, a: float, b: float) -> float:
        return (a + b) / 2


def build(
    link: StubLink | None,
    source: ListSource | None = None,
    lock: threading.Lock | None = None,
    real_clock: bool = False,
    rng: random.Random | None = None,
    **overrides: Any,  # noqa: ANN401 — mirrors ChatterConfig's own field types
) -> tuple[ChatterService, Clock, ListSource]:
    """stub へ配線した service。ジッターは固定値へ留めてある。

    `real_clock` は実際のスレッドを回すテスト用。そちらはテストが手で進める
    クロックでは駆動できない。
    """
    settings: dict[str, Any] = {
        # 上下限を等しくすると uniform() を差し替えずに決定的になるので、
        # ペーシングのテストが算術として読める。
        "gap_min": 30.0,
        "gap_max": 30.0,
        "idle_min": 100.0,
        "idle_max": 100.0,
        "engine": "http://192.0.2.1:50021",
        # 既定は off。実際の `~/.claude/projects` を読むテストにしないため
        # で、これを見るテストは自分で tmp の root を渡す。
        "sessions": False,
    }
    settings.update(overrides)
    cfg = ChatterConfig(**settings)
    clock = Clock()
    src = source if source is not None else ListSource()
    service = ChatterService(
        cfg,
        lambda: link,
        lock if lock is not None else threading.Lock(),
        source=src,
        rng=rng if rng is not None else random.Random(1),
        clock=time.monotonic if real_clock else clock,
    )
    return service, clock, src
