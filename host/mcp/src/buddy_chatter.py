"""Idle chatter — the device mutters to itself while Claude Code works.

Dog-fooding. Claude Code's hooks fire a datagram at this process on every
tool call, notification and stop; a worker thread turns those into
utterances on the Cardputer-Adv. The point is to keep the MCP server and
the whole speech path under continuous real use instead of only being
exercised when someone remembers to call `buddy_speak`.

### Nothing here is on the critical path

Two rules make that true, and both matter more than anything the chatter
actually says:

- The hook does one `sendto` on an unconnected datagram socket and
  returns. No connect, no handshake, no reply, and no listener needed —
  if this process is not running the send fails and the hook still
  exits 0. A tool call never waits on it.
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

### Pacing

A fixed interval reads as a metronome and grates within minutes. Every
gap is drawn fresh from a range instead, and so is the silence that
counts as idle, so the device is quiet for a while and then says two
things close together — the shape an actual person in the room has.

The range itself is not fixed either. The hook events already arriving
say how hard the session is working, and a companion that talks at the
same rate through a long compile and through a burst of edits is not
following along. So the window a gap is drawn from slides across the
configured range: busy sessions draw from the short end, quiet ones from
the long end. The window keeps its width at both extremes, which is what
stops the busy case from collapsing back into a metronome.

Activity is read live rather than at draw time. A burst that starts
after a long gap was already drawn shortens the wait under way — which
is the case that matters, because it is the one anybody notices. The
jitter is what is fixed per utterance; where it lands moves.

### Where the lines come from

`claude -p`, spawned for one turn per batch. Not an API client: the CLI
is the one thing the machine running this is certain to have installed
and logged in, and it inherits whatever model and credentials the user
is already set up with. See `ClaudeCliLineSource`.
"""

from __future__ import annotations

import json
import os
import random
import socket
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import replace
from queue import Empty, Full, Queue
from typing import Any

from buddy_verbs import say, speak, voicevox_url
from chatter_core import SAID, ChatLink, ChatterConfig, Event, LineSource, parse_event
from chatter_lines import ClaudeCliLineSource

# How often the worker wakes when no event arrives. Only bounds how
# promptly an idle line lands, so a second is plenty.
_TICK = 1.0

# One datagram is a couple of hundred bytes of JSON; this is slack, not
# a target. Oversized sends are truncated by the kernel and then fail to
# parse, which is the correct outcome for a malformed sender.
_MAX_DATAGRAM = 8192

# Bounded so a burst of tool calls cannot grow the queue without limit
# while the device is busy. Old events are the ones worth losing.
_QUEUE_DEPTH = 64

# What the model is told about, and what a line is generated from.
_HISTORY_DEPTH = 12

# How many past lines come back. Long enough to cover more than one
# batch — a repeat inside a batch is the model's to avoid, a repeat
# across batches is what it cannot see without this.
_SAID_DEPTH = 10

# How far back the worker looks when judging how busy the session is.
# Long enough that one slow tool call does not read as silence, short
# enough that the device notices a burst starting.
_ACTIVITY_WINDOW = 120.0

# Ceiling on remembered event times, so a runaway sender cannot grow the
# deque between prunes. Well above what saturates the tempo anyway.
_ACTIVITY_DEPTH = 256

# How much of the gap range one draw spans. The rest of the range is
# what the tempo slides the draw across: at half, a busy session draws
# from the lower half and a quiet one from the upper half, and both
# still have the same amount of jitter.
_TEMPO_WIDTH = 0.5


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
        self._source = source if source is not None else ClaudeCliLineSource(cfg, rng)
        self._rng = rng or random.Random()
        self._clock = clock

        self._queue: Queue[Event] = Queue(maxsize=_QUEUE_DEPTH)
        self._history: deque[Event] = deque(maxlen=_HISTORY_DEPTH)
        # Kept apart from `_history` rather than appended to it: a run
        # of utterances would otherwise push the events that prompted
        # them out of the window, and the generator would be left
        # talking about nothing but itself.
        self._said: deque[str] = deque(maxlen=_SAID_DEPTH)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._sock: socket.socket | None = None
        # Kept across ticks so a line already paid for is not thrown
        # away when the device turns out to be busy.
        self._pending: str | None = None

        now = self._clock()
        self._last_event = now
        self._last_utterance = now
        # When events arrived, newest last. Read to say how busy the
        # session is; see `_tempo`.
        self._activity: deque[float] = deque(maxlen=_ACTIVITY_DEPTH)
        # The gap's jitter, as a fraction of the draw window. Fixed per
        # utterance; the window it lands in moves with the tempo, so the
        # gap itself is only settled when it is compared against.
        self._gap_u = self._rng.random()
        self._idle = self._draw(cfg.idle_min, cfg.idle_max)

        self.spoken = 0
        self.skipped_busy = 0
        self.skipped_offline = 0
        self.dropped = 0
        self.last_line = ""
        self.last_error = ""

    @property
    def cfg(self) -> ChatterConfig:
        """The settings in force. Frozen — retune by rebuilding the service."""
        return self._cfg

    # ----- pacing

    def _draw(self, low: float, high: float) -> float:
        """One interval, jittered. Never negative, even if misconfigured."""
        low = max(0.0, low)
        high = max(low, high)
        return self._rng.uniform(low, high)

    def _tempo(self) -> float:
        """How busy the session is, 0 (silent) to 1 (saturated).

        Prunes on read rather than on arrival: nothing else needs the
        deque trimmed, and doing it here means the answer is current
        even on a tick where no event came in.

        Two threads read this — the worker on every tick, and whichever
        one is answering `buddy_chatter_status` — so two pruners can
        race for the last element and one of them find it already gone.
        Appends only ever happen on the right, so catching that is
        enough; a lock here would be one more thing a tool call could
        end up waiting behind.
        """
        now = self._clock()
        while self._activity:
            try:
                if now - self._activity[0] <= _ACTIVITY_WINDOW:
                    break
                self._activity.popleft()
            except IndexError:
                break
        if not self._activity:
            return 0.0
        rate = self._cfg.busy_rate
        if rate <= 0.0:
            # No rate to divide by. Taking any activity at all as full
            # tempo is the reading that matches "saturates immediately".
            return 1.0
        per_minute = len(self._activity) / (_ACTIVITY_WINDOW / 60.0)
        return min(1.0, per_minute / rate)

    def _gap_now(self) -> float:
        """The wait the current utterance is due after, as of right now.

        Recomputed on every check so a burst can shorten a gap already
        under way. Only the jitter within the draw window is fixed.
        """
        low = max(0.0, self._cfg.gap_min)
        high = max(low, self._cfg.gap_max)
        span = high - low
        if span <= 0.0:
            return low
        width = span * _TEMPO_WIDTH
        start = low + (1.0 - self._tempo()) * (span - width)
        return start + self._gap_u * width

    def _rearm(self, now: float) -> None:
        self._last_utterance = now
        self._last_event = now
        self._gap_u = self._rng.random()
        self._idle = self._draw(self._cfg.idle_min, self._cfg.idle_max)

    def _due(self, ev: Event | None) -> Event | None:
        """Fold in one event, or a quiet tick, and say what to talk about.

        Returns the event worth speaking to, or None to stay quiet. The
        gap is checked before the idle threshold so a burst of tool calls
        cannot talk over the previous utterance.
        """
        now = self._clock()
        if ev is not None:
            self._history.append(ev)
            self._last_event = now
            # Counted before the gap check, not after: an event that
            # lands inside the gap says nothing worth speaking to yet,
            # but it is still what the session being busy is made of.
            self._activity.append(now)
        if now - self._last_utterance < self._gap_now():
            return None
        if ev is not None:
            return ev
        if now - self._last_event < self._idle:
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

    def _transmit(self, link: ChatLink, line: str) -> bool:
        """Put one line on the device. Never raises.

        A silent device is the correct failure here: VOICEVOX may be
        down, the WiFi may have dropped, the device may have been reset
        out from under the link. None of that is worth interrupting the
        work the chatter exists to accompany.
        """
        try:
            say(link, line, pace=0)
            if self.spoken % self._cfg.voice_every == 0:
                speak(
                    link,
                    line,
                    url=voicevox_url(self._cfg.engine or None),
                    speaker=self._cfg.speaker,
                    rate=self._cfg.rate,
                )
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
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
            self._rearm(self._clock())
            return

        if self._pending is None:
            # Deliberately outside the lock: generation is a network
            # round trip, and holding the device across it would make a
            # real tool call wait on the chatter's API latency.
            self._pending = self._source.next_line(self._context())
        if not self._pending:
            self._pending = None
            self._rearm(self._clock())
            return

        if not self._lock.acquire(blocking=False):
            # A real tool call owns the device. Keep the line and come
            # back for it; do not rearm, this is not a turn spent.
            self.skipped_busy += 1
            return
        try:
            ok = self._transmit(link, self._pending)
        finally:
            self._lock.release()

        if ok:
            self.last_line = self._pending
            # Only what the device actually took. A line lost to a dead
            # link was never said, and remembering it would spend one of
            # the few slots the generator has to differ from.
            self._said.append(self._pending)
            self.spoken += 1
        self._pending = None
        self._rearm(self._clock())

    # ----- threads

    def start(self) -> None:
        """Bind the socket and start listening. Idempotent."""
        if not self._cfg.enabled or self._threads:
            return
        path = self._cfg.socket_path
        path.parent.mkdir(parents=True, exist_ok=True)
        # A datagram socket left behind by a killed server keeps the
        # path occupied; nothing is listening on it, so removing it is
        # not destructive.
        with suppress(FileNotFoundError):
            path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        sock.bind(str(path))
        self._sock = sock
        self._stop.clear()
        for name, target in (("buddy-chatter-rx", self._receive), ("buddy-chatter", self._work)):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            with suppress(OSError):
                sock.close()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads.clear()
        with suppress(FileNotFoundError, OSError):
            self._cfg.socket_path.unlink()

    @property
    def running(self) -> bool:
        return bool(self._threads)

    def _receive(self) -> None:
        sock = self._sock
        if sock is None:
            return
        while not self._stop.is_set():
            try:
                data, _ = sock.recvfrom(_MAX_DATAGRAM)
            except TimeoutError:
                continue
            except OSError:
                # The socket was closed under us by stop().
                return
            ev = parse_event(data)
            if ev is None:
                continue
            try:
                self._queue.put_nowait(ev)
            except Full:
                self.dropped += 1

    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                ev: Event | None = self._queue.get(timeout=_TICK)
            except Empty:
                ev = None
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
        while self._threads and any(t.is_alive() for t in self._threads):
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
            "dropped_events": self.dropped,
            "queued": self._queue.qsize(),
            # The wait in force right now, not a number settled earlier:
            # it moves as the session speeds up and slows down.
            "next_gap_s": round(self._gap_now(), 1),
            "next_idle_s": round(self._idle, 1),
            "tempo": round(self._tempo(), 2),
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
# Normally the chatter is threads inside the MCP server. This runs the
# same service as its own process, owning the port itself.
#
# Two reasons it exists. The MCP server imports the host code once at
# session start, so a change here does not reach a running one — this is
# how a change gets tried without restarting Claude Code. And it is the
# only way to have the device muttering during a session that started
# before the feature did. Remember that the port takes one owner: call
# `buddy_disconnect` first, and expect the MCP tools to be unable to
# reach the device until this exits.


def main(argv: Sequence[str] | None = None) -> int:
    """Run the chatter against a device this process owns."""
    import argparse

    from resident_link import ResidentLink

    parser = argparse.ArgumentParser(description="Run the Buddy idle chatter standalone.")
    parser.add_argument("--port", default=os.environ.get("BUDDY_PORT", "/dev/cu.usbmodem101"))
    parser.add_argument(
        "--start", action="store_true", help="launch the app over the REPL before listening"
    )
    parser.add_argument("--gap-min", type=float, default=None)
    parser.add_argument("--gap-max", type=float, default=None)
    parser.add_argument("--busy-rate", type=float, default=None)
    parser.add_argument("--voice-every", type=int, default=None)
    parser.add_argument(
        "--once", action="store_true", help="say one line, report, and exit — a smoke test"
    )
    args = parser.parse_args(argv)

    cfg = ChatterConfig.from_env()
    if args.gap_min is not None:
        cfg = replace(cfg, gap_min=args.gap_min)
    if args.gap_max is not None:
        cfg = replace(cfg, gap_max=args.gap_max)
    if args.busy_rate is not None:
        cfg = replace(cfg, busy_rate=args.busy_rate)
    if args.voice_every is not None:
        cfg = replace(cfg, voice_every=max(1, args.voice_every))

    link = ResidentLink(args.port)
    link.connect()
    if args.start:
        link.start_app()

    service = ChatterService(cfg, lambda: link, threading.Lock())
    if args.once:
        # Zero thresholds so the very first turn is due.
        service = ChatterService(
            replace(cfg, gap_min=0.0, gap_max=0.0, idle_min=0.0, idle_max=0.0),
            lambda: link,
            threading.Lock(),
        )
        service.step(Event("session", "smoke test"))
        print(json.dumps(service.status(), ensure_ascii=False, indent=2))
        link.disconnect()
        return 0 if service.spoken else 1

    service.start()
    print(f"chatter listening on {cfg.socket_path} (device {args.port})", file=sys.stderr)

    def report(status: dict[str, Any]) -> None:
        note = f"  ! {status['last_error']}" if status["last_error"] else ""
        print(
            f"[{status['spoken']:3d}] {status['last_line'] or '-'}"
            f"  (busy {status['skipped_busy']}, offline {status['skipped_offline']},"
            f" tempo {status['tempo']}, next {status['next_gap_s']}s){note}",
            file=sys.stderr,
            flush=True,
        )

    try:
        service.wait(report)
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
        link.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
