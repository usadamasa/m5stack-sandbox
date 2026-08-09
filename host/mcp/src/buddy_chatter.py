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
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any, Protocol

from buddy_bridge import DEFAULT_RATE, ZUNDAMON, Message, say, speak, voicevox_url

# host/mcp/src/buddy_chatter.py -> repo root. The MCP server is launched
# from an arbitrary cwd, so the socket path cannot be relative and the
# hook has to be able to derive the same answer from its own location.
REPO = Path(__file__).resolve().parents[3]
DEFAULT_SOCKET = REPO / "tmp" / "buddy-chatter.sock"

# How often the worker wakes when no event arrives. Only bounds how
# promptly an idle line lands, so a second is plenty.
_TICK = 1.0

# Events the hook can report. Anything else is dropped rather than
# passed to the model, which keeps a stray sender from steering it.
KINDS = frozenset({"tool", "error", "stop", "notify", "prompt", "session", "idle"})

# One datagram is a couple of hundred bytes of JSON; this is slack, not
# a target. Oversized sends are truncated by the kernel and then fail to
# parse, which is the correct outcome for a malformed sender.
_MAX_DATAGRAM = 8192

# Bounded so a burst of tool calls cannot grow the queue without limit
# while the device is busy. Old events are the ones worth losing.
_QUEUE_DEPTH = 64

# What the model is told about, and what a line is generated from.
_HISTORY_DEPTH = 12

# Said when generation fails — no ADC credentials, no network, Vertex
# refusing. Rare, but the alternative is a device that goes silent for
# the rest of the session with no visible reason.
_FALLBACK_LINES = (
    "ぼくは元気にしているのだ",
    "ふぅん、そういうものなのだ",
    "見ているだけなのだ",
    "まだ終わらないのだ",
    "ちょっと眠たくなってきたのだ",
)

# The persona lives in a file, not in this module. Changing how the
# device talks is editing prose, and prose in a string literal invites
# nobody to edit it — least of all the person who wants a different
# character but does not want to touch Python.
DEFAULT_PROMPT_PATH = Path(__file__).with_name("chatter_prompt.md")


class ChatLink(Protocol):
    """The slice of `ResidentLink` the chatter needs.

    Narrow so the tests can drive a stub, and so this module does not
    depend on how the link opens or closes — it only ever borrows one.
    """

    @property
    def connected(self) -> bool: ...

    def request(self, obj: Message, expect: str, timeout: float = 5.0) -> Message: ...

    def await_ack(self, expect: str, timeout: float = 5.0) -> Message: ...


@dataclass(frozen=True, slots=True)
class Event:
    """Something the hook saw, reduced to what a line can be built from."""

    kind: str
    detail: str = ""


class LineSource(Protocol):
    """Where the next thing to say comes from."""

    def next_line(self, context: Sequence[Event]) -> str | None: ...


def _float_env(env: Mapping[str, str], name: str, fallback: float) -> float:
    """Read a float, treating anything unparseable as absent.

    A typo in an environment variable must not stop the server from
    starting; the chatter is decoration and its configuration should
    degrade the same way it does.
    """
    try:
        return float(env[name])
    except (KeyError, ValueError):
        return fallback


def _int_env(env: Mapping[str, str], name: str, fallback: int) -> int:
    try:
        return int(env[name])
    except (KeyError, ValueError):
        return fallback


@dataclass(frozen=True)
class ChatterConfig:
    """Everything tunable, resolved once at start."""

    socket_path: Path = DEFAULT_SOCKET
    # Where the persona is written. Point it elsewhere for a different
    # character; nothing here reads its contents but the generator.
    prompt_path: Path = DEFAULT_PROMPT_PATH
    enabled: bool = True
    # Seconds between utterances, drawn fresh each time from this range.
    gap_min: float = 40.0
    gap_max: float = 150.0
    # Silence after which the device says something unprompted, likewise
    # redrawn so it does not tick.
    idle_min: float = 60.0
    idle_max: float = 180.0
    # Speak aloud on every Nth utterance; the rest go to the panel only.
    voice_every: int = 1
    # Lines produced per generation. One call covers several minutes,
    # which is what keeps this cheap.
    batch: int = 6
    model: str = "claude-opus-5"
    project_id: str = ""
    region: str = "us"
    speaker: int = ZUNDAMON
    rate: int = DEFAULT_RATE
    engine: str = ""
    # One panel of Japanese is 32 characters; leave a little headroom so
    # a line is never split across two sends.
    max_chars: int = 30

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ChatterConfig:
        """Build a config from the process environment.

        The Vertex project and region default to the ones Claude Code is
        already using, so a machine that can run Claude Code can run the
        chatter without being told anything twice.
        """
        env = os.environ if env is None else env
        raw_socket = env.get("BUDDY_CHATTER_SOCKET", "")
        raw_prompt = env.get("BUDDY_CHATTER_PROMPT", "")
        return cls(
            socket_path=Path(raw_socket) if raw_socket else DEFAULT_SOCKET,
            prompt_path=Path(raw_prompt) if raw_prompt else DEFAULT_PROMPT_PATH,
            enabled=env.get("BUDDY_CHATTER", "1") not in ("0", "false", "no"),
            gap_min=_float_env(env, "BUDDY_CHATTER_GAP_MIN", 40.0),
            gap_max=_float_env(env, "BUDDY_CHATTER_GAP_MAX", 150.0),
            idle_min=_float_env(env, "BUDDY_CHATTER_IDLE_MIN", 60.0),
            idle_max=_float_env(env, "BUDDY_CHATTER_IDLE_MAX", 180.0),
            voice_every=max(1, _int_env(env, "BUDDY_CHATTER_VOICE_EVERY", 1)),
            batch=max(1, _int_env(env, "BUDDY_CHATTER_BATCH", 6)),
            model=env.get("BUDDY_CHATTER_MODEL", "claude-opus-5"),
            project_id=env.get("ANTHROPIC_VERTEX_PROJECT_ID", ""),
            region=env.get("CLOUD_ML_REGION", "us"),
            speaker=_int_env(env, "BUDDY_CHATTER_SPEAKER", ZUNDAMON),
            rate=_int_env(env, "BUDDY_CHATTER_RATE", DEFAULT_RATE),
            engine=env.get("VOICEVOX_URL", ""),
        )


def parse_event(payload: bytes) -> Event | None:
    """Turn one datagram into an event, or None if it is not one.

    Anything malformed is dropped in silence. The sender is a hook that
    cannot be told about the failure and must not be slowed down finding
    out, so there is nowhere for an error to go.
    """
    try:
        obj = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    kind = obj.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        return None
    detail = obj.get("detail", "")
    if not isinstance(detail, str):
        detail = ""
    # The detail is pasted into a prompt, so it is clamped here rather
    # than trusting the sender to have been reasonable about it.
    return Event(kind, " ".join(detail.split())[:120])


def _clean(line: object, limit: int) -> str:
    """Reduce one generated line to something the panel can hold.

    Takes `object` because the model's output is JSON: the schema asks
    for strings, and a non-string that arrives anyway should become a
    short line rather than a TypeError on a background thread.
    """
    text = " ".join(str(line).split())
    return text[:limit]


def describe(context: Sequence[Event]) -> str:
    """Render recent events as the prompt's view of what is going on."""
    if not context:
        return "まだ何も起きていない。"
    return "\n".join(f"- {ev.kind}: {ev.detail}" if ev.detail else f"- {ev.kind}" for ev in context)


class VertexLineSource:
    """Generates lines with Claude on Vertex AI, a batch at a time.

    Generating one line per utterance would be a round trip every time
    the device opens its mouth. A batch covers several minutes, and the
    latency of producing it is invisible because the only thread waiting
    on it is the chatter's own.

    The batch is filled from the context that happened to be current
    when it ran dry, so later lines in a batch lag what is going on.
    That is the trade being made deliberately: this is muttering, not
    commentary.
    """

    def __init__(self, cfg: ChatterConfig, rng: random.Random | None = None) -> None:
        self._cfg = cfg
        self._rng = rng or random.Random()
        self._cache: deque[str] = deque()
        self._client: Any = None
        self._prompt: str | None = None
        self.generated = 0
        self.failures = 0
        self.last_error = ""

    def _system_prompt(self) -> str:
        """The persona, read from `cfg.prompt_path` once.

        Read lazily and not at import: a missing or unreadable prompt is
        a generation failure like any other, which the caller already
        counts and falls back from, rather than something that stops the
        MCP server from loading.
        """
        if self._prompt is None:
            self._prompt = self._cfg.prompt_path.read_text(encoding="utf-8")
        return self._prompt

    def _ensure_client(self) -> Any:  # noqa: ANN401 — the SDK ships no public client alias
        """Build the Vertex client on first use.

        Deferred rather than built in `__init__` so importing this
        module never touches credentials: the tests, and any machine
        without application-default credentials, must still be able to
        load the MCP server.

        Credentials come from application-default credentials, the same
        place Claude Code itself gets them, so a machine that can run
        Claude Code needs no extra setup. An absent `project_id` is left
        unset rather than passed as None: the SDK then resolves it from
        ADC, which is a better answer than anything guessed here.
        """
        if self._client is None:
            # From `anthropic.lib.vertex` rather than the package root:
            # the runtime re-export at the top level is not in the type
            # stubs, and this is the path the checker accepts.
            from anthropic.lib.vertex import AnthropicVertex

            if self._cfg.project_id:
                self._client = AnthropicVertex(
                    project_id=self._cfg.project_id, region=self._cfg.region
                )
            else:
                self._client = AnthropicVertex(region=self._cfg.region)
        return self._client

    def next_line(self, context: Sequence[Event]) -> str | None:
        if not self._cache:
            self._fill(context)
        if not self._cache:
            return _clean(self._rng.choice(_FALLBACK_LINES), self._cfg.max_chars)
        return self._cache.popleft()

    def _fill(self, context: Sequence[Event]) -> None:
        try:
            reply = self._ensure_client().messages.create(
                model=self._cfg.model,
                max_tokens=2048,
                # Thinking stays on. Turning it off on this model is a
                # documented way to get `<thinking>` text leaking into
                # the answer, and nothing here is waiting on latency, so
                # the effort knob is the right one to turn down.
                thinking={"type": "adaptive"},
                output_config={
                    "effort": "low",
                    "format": {
                        "type": "json_schema",
                        "schema": {
                            "type": "object",
                            "properties": {"lines": {"type": "array", "items": {"type": "string"}}},
                            "required": ["lines"],
                            "additionalProperties": False,
                        },
                    },
                },
                system=self._system_prompt(),
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"直近の出来事:\n{describe(context)}\n\n独り言を {self._cfg.batch} 個。"
                        ),
                    }
                ],
            )
            body = next(block.text for block in reply.content if block.type == "text")
            lines = json.loads(body)["lines"]
        except Exception as exc:  # the chatter degrades, it does not raise
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            return

        for raw in lines:
            cleaned = _clean(raw, self._cfg.max_chars)
            if cleaned:
                self._cache.append(cleaned)
                self.generated += 1


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
        self._source = source if source is not None else VertexLineSource(cfg, rng)
        self._rng = rng or random.Random()
        self._clock = clock

        self._queue: Queue[Event] = Queue(maxsize=_QUEUE_DEPTH)
        self._history: deque[Event] = deque(maxlen=_HISTORY_DEPTH)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._sock: socket.socket | None = None
        # Kept across ticks so a line already paid for is not thrown
        # away when the device turns out to be busy.
        self._pending: str | None = None

        now = self._clock()
        self._last_event = now
        self._last_utterance = now
        self._gap = self._draw(cfg.gap_min, cfg.gap_max)
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

    def _rearm(self, now: float) -> None:
        self._last_utterance = now
        self._last_event = now
        self._gap = self._draw(self._cfg.gap_min, self._cfg.gap_max)
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
        if now - self._last_utterance < self._gap:
            return None
        if ev is not None:
            return ev
        if now - self._last_event < self._idle:
            return None
        idle = Event("idle")
        self._history.append(idle)
        return idle

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
            self._pending = self._source.next_line(list(self._history))
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
            "next_gap_s": round(self._gap, 1),
            "next_idle_s": round(self._idle, 1),
            "voice_every": self._cfg.voice_every,
            "model": self._cfg.model,
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

    from buddy_bridge import ResidentLink

    parser = argparse.ArgumentParser(description="Run the Buddy idle chatter standalone.")
    parser.add_argument("--port", default=os.environ.get("BUDDY_PORT", "/dev/cu.usbmodem101"))
    parser.add_argument(
        "--start", action="store_true", help="launch the app over the REPL before listening"
    )
    parser.add_argument("--gap-min", type=float, default=None)
    parser.add_argument("--gap-max", type=float, default=None)
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
            f" next {status['next_gap_s']}s){note}",
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
