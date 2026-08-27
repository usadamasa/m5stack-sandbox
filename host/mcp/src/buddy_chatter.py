"""Idle chatter — the device mutters to itself while Claude Code works.

Dog-fooding. Claude Code's hooks fire a datagram at this process on every
tool call, notification and stop; a worker thread turns those into
utterances on the Cardputer-Adv. The point is to keep the MCP server and
the whole speech path under continuous real use instead of only being
exercised when someone remembers to call `buddy_speak`.

### Nothing here is on the critical path

Two rules make that true, and both matter more than anything the chatter
actually says:

- The hook does one `sendto` and returns, with nothing to wait on. See
  `chatter_inbox`.
- The worker takes the device lock with `blocking=False`. If a real tool
  call owns the device, the chatter keeps its line and tries again on a
  later tick rather than making anyone queue behind it.

### Why it lives inside the MCP server

The serial port takes exactly one owner and the MCP server already is
it. A separate daemon would have to take the port away from the tools it
is meant to be decorating. So the chatter is two threads in this process
and reaches the device through the same `ResidentLink`, serialised by a
lock the tools hold too — `ResidentLink.await_ack` matches acks by name
and pops the first one that fits, so two overlapping requests of the
same kind would hand each other's answers back.

It never opens the port itself. Speaking only happens when a link is
already connected, which is what keeps `buddy_deploy.py` and `esptool`
able to claim the port between sessions.

### 何をどこへ出したか

ここに残る `ChatterService` は喋る側 — 何に向かって喋るかを決め、台詞を
デバイスへ載せ、結果を報告する。いつ喋るかは `chatter_pace.Pacer`、hook の
データグラムを受ける側は `chatter_inbox.Inbox`、台詞そのものを書くのは
`chatter_lines`、自分のプロセスで走らせる口は `chatter_cli`。依存は
cli → service → inbox / pace / lines → core の一方向で、逆向きは無い。
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from buddy_verbs import say, speak, voicevox_url
from chatter_core import SAID, ChatLink, ChatterConfig, Event, LineSource
from chatter_inbox import Inbox
from chatter_lines import ClaudeCliLineSource
from chatter_pace import Pacer
from chatter_sessions import SessionRegistry

# daemon の log へ。喋ったこと・喋れなかった理由が残るのはここだけで、
# `status()` は「今どうなっているか」しか答えられない — 30 分前に何を言った
# かも、なぜ 10 分黙っていたかも、後から訊く先が無い。
log = logging.getLogger("buddy.chatter")

# What the model is told about, and what a line is generated from.
_HISTORY_DEPTH = 12

# How many past lines come back. Long enough to cover more than one
# batch — a repeat inside a batch is the model's to avoid, a repeat
# across batches is what it cannot see without this.
_SAID_DEPTH = 10


class ChatterService:
    """Listens for hook events and speaks, on its own two threads.

    `link_provider` returns the live link or None; it is called on every
    utterance rather than held, because the MCP server drops and rebuilds
    its link across a reconnect and the chatter must not pin a dead one.

    `device_lock` is the MCP server's — held blocking by tool calls, and
    taken here only if it is free.
    """

    def __init__(
        self,
        cfg: ChatterConfig,
        link_provider: Callable[[], ChatLink | None],
        device_lock: threading.Lock,
        source: LineSource | None = None,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cfg = cfg
        self._link = link_provider
        self._lock = device_lock
        # 台帳はここが持つ。hook のイベントを見ているのはこの側だけで、
        # 台詞を書く側はバッチを作る瞬間にそれを覗く。同じものを渡すのは、
        # 2 つあれば片方だけが更新されて、読まれるのはもう片方になるから。
        self._sessions = (
            SessionRegistry(cfg.projects_path, ttl=cfg.session_ttl, limit=cfg.session_limit)
            if cfg.sessions
            else None
        )
        self._source = (
            source if source is not None else ClaudeCliLineSource(cfg, rng, sessions=self._sessions)
        )
        self._rng = rng or random.Random()
        self._clock = clock

        self._inbox = Inbox(cfg.socket_path)
        self._history: deque[Event] = deque(maxlen=_HISTORY_DEPTH)
        # Kept apart from `_history` rather than appended to it: a run
        # of utterances would otherwise push the events that prompted
        # them out of the window, and the generator would be left
        # talking about nothing but itself.
        self._said: deque[str] = deque(maxlen=_SAID_DEPTH)
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        # Kept across ticks so a line already paid for is not thrown
        # away when the device turns out to be busy.
        self._pending: str | None = None
        self._pace = Pacer(cfg, self._rng, clock)

        self.spoken = 0
        self.skipped_busy = 0
        self.skipped_offline = 0
        self.last_line = ""
        self.last_error = ""

    @property
    def cfg(self) -> ChatterConfig:
        """The settings in force. Frozen — retune by rebuilding the service."""
        return self._cfg

    @property
    def sessions(self) -> SessionRegistry | None:
        """動いているセッションの台帳。読まない設定なら None。"""
        return self._sessions

    # ----- deciding

    def _due(self, ev: Event | None) -> Event | None:
        """Fold in one event, or a quiet tick, and say what to talk about.

        Returns the event worth speaking to, or None to stay quiet. The
        gap is checked before the idle threshold so a burst of tool calls
        cannot talk over the previous utterance.
        """
        now = self._clock()
        if ev is not None:
            self._history.append(ev)
            self._pace.note_activity(now)
            if self._sessions is not None and ev.session:
                # 喋るかどうかとは無関係に記録する。黙っている間も
                # セッションは動いていて、次に口を開くときの材料になる。
                self._sessions.note_activity(ev.session)
        if self._pace.waiting(now):
            return None
        if ev is not None:
            return ev
        if not self._pace.idle_due(now):
            return None
        # Not counted as activity. The chatter talking to itself would
        # otherwise read as a busy session and talk faster for it.
        idle = Event("idle")
        self._history.append(idle)
        return idle

    def _context(self) -> list[Event]:
        """What the generator is shown: what happened, then what was said."""
        return [*self._history, *(Event(SAID, line) for line in self._said)]

    # ----- speaking

    def _transmit(self, link: ChatLink, line: str, *, voice: bool) -> bool:
        """Put one line on the device. Never raises.

        A silent device is the correct failure here: VOICEVOX may be
        down, the WiFi may have dropped, the device may have been reset
        out from under the link. None of that is worth interrupting the
        work the chatter exists to accompany.

        声に出すかどうかは呼び出し側が決める。ここで数え直すと、log に
        「声付き」と書いた行と実際に鳴らした行がずれうるため。
        """
        try:
            say(link, line, pace=0)
            if voice:
                speak(
                    link,
                    line,
                    url=voicevox_url(self._cfg.engine or None),
                    speaker=self._cfg.speaker,
                    rate=self._cfg.rate,
                )
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            # デバイスが受け取らなかった。喋らない chatter の理由がここに
            # 出るので、status の `last_error` だけに残して黙らない。
            log.warning("not said: %s (%s)", line, self.last_error)
            return False
        return True

    def step(self, ev: Event | None) -> None:
        """One turn of the worker. Public so the tests can drive it."""
        trigger = self._due(ev)
        if trigger is None:
            return

        link = self._link()
        if link is None or not link.connected:
            # Nothing to talk to. Back off as though it had spoken,
            # rather than re-deciding every second for the whole time
            # the device is unplugged.
            self.skipped_offline += 1
            # DEBUG: デバイスを抜いている間ずっと出続ける行なので、既定の
            # INFO では log がこれで埋まる。数えた分は status にある。
            log.debug("skipped: no link (offline %d)", self.skipped_offline)
            self._pace.rearm(self._clock())
            return

        if self._pending is None:
            # Deliberately outside the lock: generation is a network
            # round trip, and holding the device across it would make a
            # real tool call wait on the chatter's API latency.
            self._pending = self._source.next_line(self._context())
        if not self._pending:
            self._pending = None
            self._pace.rearm(self._clock())
            return

        if not self._lock.acquire(blocking=False):
            # A real tool call owns the device. Keep the line and come
            # back for it; do not rearm, this is not a turn spent.
            self.skipped_busy += 1
            log.debug("skipped: device busy (busy %d)", self.skipped_busy)
            return
        voice = self.spoken % self._cfg.voice_every == 0
        try:
            ok = self._transmit(link, self._pending, voice=voice)
        finally:
            self._lock.release()

        if ok:
            self.last_line = self._pending
            # Only what the device actually took. A line lost to a dead
            # link was never said, and remembering it would spend one of
            # the few slots the generator has to differ from.
            self._said.append(self._pending)
            self.spoken += 1
        said = self._pending
        self._pending = None
        self._pace.rearm(self._clock())
        if ok:
            # rearm の後に読む。次の待ち時間はここで引き直されるので、前に
            # 読むと 1 回前の抽選を「次」として書くことになる。
            log.info(
                'said "%s" (voice=%s, next %.0fs)',
                said,
                "yes" if voice else "no",
                self._pace.gap_now(),
            )

    # ----- threads

    def start(self) -> None:
        """Bind the socket and start listening. Idempotent."""
        if not self._cfg.enabled or self._worker is not None:
            return
        self._inbox.start()
        self._stop.clear()
        worker = threading.Thread(target=self._work, name="buddy-chatter", daemon=True)
        worker.start()
        self._worker = worker

    def stop(self) -> None:
        self._stop.set()
        self._inbox.stop()
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None

    @property
    def running(self) -> bool:
        return self._worker is not None

    @property
    def listening(self) -> bool:
        """socket が bind できていて、hook のデータグラムが届く状態か。

        `running` とは別に持つ。bind に失敗した chatter は worker だけが
        回っている状態になり、外からは「独り言を言わない」としか見えない
        — 起動時の疏通確認がそこを名指しできるように分けてある。
        """
        return self._inbox.running

    def _work(self) -> None:
        while not self._stop.is_set():
            ev = self._inbox.get()
            try:
                self.step(ev)
            except Exception as exc:
                # The worker outliving its own bugs is the whole point:
                # a crashed chatter would silently stop dog-fooding and
                # nobody would notice until the device went quiet.
                self.last_error = f"{type(exc).__name__}: {exc}"

    # ----- introspection

    def wait(self, on_change: Callable[[dict[str, Any]], None] | None = None) -> None:
        """Block until the worker stops, reporting whenever it did something.

        For the standalone runner. Reports failures as well as lines: a
        chatter whose device stopped answering says nothing, and "said
        nothing" is also what working correctly looks like from outside.

        Polled rather than pushed from `step()` so the worker keeps its
        one job — a logging callback that threw would be one more way for
        the chatter to take the device down with it.
        """
        seen: tuple[int, int, int, str] | None = None
        while self._worker is not None and self._worker.is_alive():
            if on_change is not None:
                current = (self.spoken, self.skipped_busy, self.skipped_offline, self.last_error)
                if current != seen:
                    seen = current
                    on_change(self.status())
            time.sleep(0.5)

    def status(self) -> dict[str, Any]:
        source = self._source
        return {
            "running": self.running,
            "socket": str(self._cfg.socket_path),
            "spoken": self.spoken,
            "skipped_busy": self.skipped_busy,
            "skipped_offline": self.skipped_offline,
            "dropped_events": self._inbox.dropped,
            "queued": self._inbox.queued,
            # 台帳に載っているセッションの数。読まない設定なら None。
            # デバイスがよその話をしないときに、届いていないのか読めて
            # いないのかがここで分かれる。
            "sessions": self._sessions.tracked if self._sessions is not None else None,
            # The wait in force right now, not a number settled earlier:
            # it moves as the session speeds up and slows down.
            "next_gap_s": round(self._pace.gap_now(), 1),
            "next_idle_s": round(self._pace.idle_s, 1),
            "tempo": round(self._pace.tempo(), 2),
            "busy_rate": self._cfg.busy_rate,
            "voice_every": self._cfg.voice_every,
            "backend": getattr(source, "backend", ""),
            "model": getattr(source, "model", "") or self._cfg.model,
            "effort": self._cfg.effort,
            "batch": self._cfg.batch,
            "last_line": self.last_line,
            "last_error": self.last_error,
            "generated": getattr(source, "generated", None),
            "generation_failures": getattr(source, "failures", None),
            "generation_error": getattr(source, "last_error", ""),
        }


# ----- standalone
#
# 自分のプロセスで走らせる口は `chatter_cli` にある。ここから import する
# ことは無い — サービスは CLI を知らない。
