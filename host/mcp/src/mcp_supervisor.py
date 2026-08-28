"""落ちたリンクを拾い直し、health を書き直す周期処理。

### なぜ要るか

2026-08-28 09:03、デバイスが reboot して USB が再列挙された。デバイス自身は
その後 4 時間ずっと正常だった (`up` 15035 秒、TCP に即答) のに、daemon は死んだ
fd を握ったまま 13:10 まで一度も開き直さなかった。log に `reopening` の行は
1 本も無い。

開き直す経路が `mcp_state.get_link` — tool 呼び出しの中 — にしか無かったため。
chatter が通る `live_link` はデバイスロックの外から呼ばれるので開き直せず、
誰も tool を呼ばない 4 時間は、開き直す機会そのものが無かった。

ここはロックを取れる場所として在る。chatter の tick とは別のスレッドで回り、
ロックを取ってから開き直すので、進行中の tool 呼び出しと競合しない。

### 何を開き直すのか

`mcp_state.wanted` に載っているポートだけ。`buddy_disconnect` はそれを `None`
にするので、明示的に手放したポートをここが取り返す道は構造として無い —
`buddy_deploy.py` と `esptool` はそれを頼りにポートを受け取る。

### health

起動時の疏通確認 (`mcp_health.check_on_start`) は VOICEVOX の HTTP と
`claude --version` まで見るが、それを 60 秒ごとに叩き直すのは無駄。起動時の
Check をそのまま持ち回り、`serial` の 1 項目と `checked_at` だけを毎 tick
書き直す。`health.json` の「今デバイスが答えるか」はこれで生きた値になる。

依存は `serve -> supervisor -> health -> state` の一方向。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Protocol

import mcp_health
from buddy_wire import Message
from mcp_health import Check

log = logging.getLogger("buddy.supervisor")

# tick の間隔。デバイスが reboot してから拾い直すまでの最悪の待ち時間であり、
# `health.json` の鮮度でもある。短くしても得るものは少ない: chatter の発話は
# 40 秒より詰まらないので、1 分あれば「黙り続ける」ことはなくなる。
INTERVAL = 60.0


class Device(Protocol):
    """リンクのうち、ここが使う面だけ。"""

    @property
    def port(self) -> str: ...

    def request(self, obj: Message, expect: str, timeout: float = 5.0) -> Message: ...


class DeviceState(Protocol):
    """`mcp_state` のうち、ここが見る面だけ。

    モジュールそのものを受け取る形にしてある。テストは同じ面を持つ偽物を
    渡せばよく、patch 先を持たないので、モジュールを割ったときにテストが
    黙って本物のシリアルポートを開きに行くこともない。
    """

    wanted: str | None
    device_lock: threading.Lock

    def live_link(self) -> Device | None: ...

    def get_link(self, port: str | None = None) -> Device: ...


def _why(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def health_writer(
    baseline: list[Check], save: Callable[[list[Check]], None]
) -> Callable[[Check], None]:
    """起動時の Check を持ち回り、`serial` の 1 項目だけ差し替えて書く係。

    起動時に `serial` が無い run (ポートを開けなかったとき) もあるので、
    無ければ末尾に足す。後から挿し直されたぶんが health に出るのはそのため。
    """
    others = [check for check in baseline if check.name != "serial"]
    index = next((i for i, check in enumerate(baseline) if check.name == "serial"), len(others))

    def write(serial: Check) -> None:
        save([*others[:index], serial, *others[index:]])

    return write


class Supervisor:
    """1 本のスレッドで tick を回す。

    `report` は tick ごとの `serial` の Check の行き先。`health_writer` が
    作るものを渡す想定だが、テストはただの list.append を渡す。
    """

    def __init__(
        self,
        state: DeviceState,
        report: Callable[[Check], None],
        *,
        interval: float = INTERVAL,
    ) -> None:
        self._state = state
        self._report = report
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # 最後に log へ出した結末。同じことを毎 tick 言わないための記憶で、
        # ボードを抜いている間ずっと出続ける行がこれで 1 度きりになる。
        self.outcome = ""

    # ----- 1 tick

    def tick(self) -> str:
        """1 周ぶん。例外は投げない。

        返り値は結末: `released` / `busy` / `ok` / `reopened` / `mute` /
        `failed`。前の 2 つはデバイスを触っていないので health も書かない
        — 触っていない tick で書くと `checked_at` だけが進んで中身は前の
        tick のもの、という嘘になる。
        """
        target = self._state.wanted
        if target is None:
            # 明示的に手放したポート。取り返さない。
            return "released"
        if not self._state.device_lock.acquire(blocking=False):
            # tool が使っている。待つとその tool がこの tick のぶん遅れる
            # ので、次の tick で拾う。
            return "busy"
        try:
            outcome, check = self._probe(target)
        finally:
            self._state.device_lock.release()
        self._report(check)
        self._note(outcome, check)
        return outcome

    def _probe(self, target: str) -> tuple[str, Check]:
        """ロックの下で: 要るなら開き直し、デバイスが今答えるかを見る。"""
        link = self._state.live_link()
        opened = False
        if link is None:
            try:
                link = self._state.get_link(target)
            except Exception as exc:
                # ボードが挿さっていないか、他のプロセスがポートを持って
                # いる。どちらも次の tick で直りうる。
                return "failed", Check("serial", False, f"{target}: not reopened ({_why(exc)})")
            opened = True
        try:
            ack = link.request({"cmd": "status"}, "status", timeout=mcp_health.STATUS_TIMEOUT)
        except Exception as exc:
            # 開いてはいるが答えない。アプリが走っておらず REPL で止まって
            # いるときの姿で、chatter から見れば喋れないのと同じ。
            return "mute", Check("serial", False, f"{link.port}: no status ack ({_why(exc)})")
        detail = f"{link.port}: {mcp_health.describe_status(ack)}"
        return ("reopened" if opened else "ok"), Check("serial", True, detail)

    def _note(self, outcome: str, check: Check) -> None:
        """変わったときだけ log に出す。"""
        if outcome == self.outcome:
            return
        self.outcome = outcome
        (log.info if check.ok else log.warning)("%s: %s", outcome, check.detail)

    # ----- スレッド

    def start(self) -> None:
        """回し始める。冪等。"""
        if self._thread is not None:
            return
        self._stop.clear()
        thread = threading.Thread(target=self._run, name="buddy-supervisor", daemon=True)
        thread.start()
        self._thread = thread

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None

    def _run(self) -> None:
        # 起動直後は確認したばかりなので、まず待ってから 1 周目に入る。
        while not self._stop.wait(self._interval):
            try:
                self.tick()
            except Exception as exc:
                # スレッドが自分のバグより長生きすることに意味がある: ここが
                # 死ぬと、拾い直す係が居ないことに誰も気づけない。
                log.warning("tick failed: %s", _why(exc))


# ----- プロセスに 1 つ
#
# chatter と同じで、daemon にひとつだけ。`buddy_mcp_serve` が起こして
# `_shutdown` が止める。

current: Supervisor | None = None


def start(state: DeviceState, baseline: list[Check], *, interval: float = INTERVAL) -> Supervisor:
    """起動時の Check を土台にして回し始める。"""
    global current
    if current is None:
        current = Supervisor(state, health_writer(baseline, mcp_health.save), interval=interval)
    current.start()
    return current


def stop() -> None:
    if current is not None:
        current.stop()
