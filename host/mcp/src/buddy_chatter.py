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

### Two agents, two models

Claude Code and Codex both drive this. The device does not care which,
but the lines have to come from a model the machine can actually reach,
and that differs: each agent's own CLI is the one thing its machine is
certain to have installed and logged in. So both backends spawn a CLI —
`claude -p` and `codex exec` — and `RoutingLineSource` picks by whoever
is connected. See `buddy_agent` for how that is decided.
"""

from __future__ import annotations

import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any, Protocol, cast

from buddy_agent import CLAUDE_CODE, CODEX, AgentIdentity
from buddy_verbs import DEFAULT_RATE, ZUNDAMON, say, speak, voicevox_url
from buddy_wire import Message

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

# The device's own past utterances, fed back so the generator has
# something to differ from. Deliberately not one of `KINDS`: the socket
# is open to anything on the machine, and a sender that could forge a
# `said` would be writing the device's memory of itself.
SAID = "said"

# How many past lines come back. Long enough to cover more than one
# batch — a repeat inside a batch is the model's to avoid, a repeat
# across batches is what it cannot see without this.
_SAID_DEPTH = 10

# Drawn two at a time and pasted into the prompt. The event log is the
# same shape all session — `tool: Bash` over and over — so without this
# every batch is generated from near-identical input and comes back
# near-identical. These are not topics to cover; they are a nudge off
# whatever the previous batch settled into.
_ANGLES: tuple[str, ...] = (
    "目の前の画面で起きていること",
    "自分の体調や気分",
    "ずんだ餅や食べものの話",
    "外の天気や季節の想像",
    "むかしのことの思い出",
    "ふと浮かんだどうでもいい疑問",
    "退屈のまぎらわしかた",
    "プログラマの手つきの観察",
    "眠気やうとうとした感じ",
    "机の上やケーブルのようす",
    "自分が住んでいる小さい機械のこと",
    "数えたり、くらべたりしてみること",
)

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

# Said when generation fails — the CLI missing, not logged in, no
# network. Rare, but the alternative is a device that goes silent for
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
    """Something the hook saw, reduced to what a line can be built from.

    `agent` is who saw it, as the hook named itself. Empty when the
    sender did not say, which is not a problem — it only means this
    event is not a witness and the identity stands as it was.
    """

    kind: str
    detail: str = ""
    agent: str = ""


class LineSource(Protocol):
    """Where the next thing to say comes from."""

    def next_line(self, context: Sequence[Event]) -> str | None: ...


# What both generators ask their model for. One object, one array of
# strings — small enough that either backend's structured-output support
# is enough to enforce it, which is what keeps the parsing trivial.
LINES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"lines": {"type": "array", "items": {"type": "string"}}},
    "required": ["lines"],
    "additionalProperties": False,
}


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
    # Where in the range the draw happens is set by how busy the session
    # is; the bounds are the extremes, not the usual case.
    gap_min: float = 40.0
    gap_max: float = 150.0
    # Hook events per minute at which the session counts as fully busy
    # and the gap sits at the short end. A tool call is registered on
    # both its Pre and Post hook, so this is roughly six calls a minute
    # — a pace a session actually working passes, and one it falls well
    # short of while waiting on a build.
    busy_rate: float = 12.0
    # Silence after which the device says something unprompted, likewise
    # redrawn so it does not tick.
    idle_min: float = 60.0
    idle_max: float = 180.0
    # Speak aloud on every Nth utterance; the rest go to the panel only.
    voice_every: int = 1
    # Lines produced per generation. One call covers several minutes,
    # which is what keeps this cheap.
    batch: int = 6
    # Who to assume is driving before anyone says. Only used until the
    # MCP handshake or a hook datagram settles it.
    agent: str = CLAUDE_CODE
    # ----- the Claude CLI, used when Claude Code is driving
    #
    # An alias rather than a pinned id, so this follows whatever the
    # current Sonnet is. Writing one line of muttering is not work that
    # needs the largest model, and this runs all session.
    claude_bin: str = "claude"
    model: str = "sonnet"
    # Passed as `--effort`. Empty leaves the CLI's own default alone.
    effort: str = "low"
    # A `claude -p` turn with no tools is a few seconds; this is a
    # stuck-process guard, not a latency budget. Nothing waits on it but
    # the chatter's own thread.
    claude_timeout: float = 120.0
    # ----- the Codex CLI, used when Codex is driving
    #
    # An empty model means "whatever `~/.codex/config.toml` already
    # says", which is the right default: the point of going through the
    # CLI is to inherit the setup Codex is already running on.
    codex_bin: str = "codex"
    codex_model: str = ""
    # Generous. A `codex exec` turn is a whole agent loop and nothing is
    # waiting on it, so the timeout is a stuck-process guard rather than
    # a latency budget.
    codex_timeout: float = 180.0
    speaker: int = ZUNDAMON
    rate: int = DEFAULT_RATE
    engine: str = ""
    # One panel of Japanese is 32 characters; leave a little headroom so
    # a line is never split across two sends.
    max_chars: int = 30

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ChatterConfig:
        """Build a config from the process environment.

        Every knob has an environment variable so a session can be
        retuned by restarting the server rather than by editing this,
        and the ones worth changing mid-session (`model`, `effort`,
        `batch`, the pacing) are also arguments to
        `buddy_chatter_start`.
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
            busy_rate=_float_env(env, "BUDDY_CHATTER_BUSY_RATE", 12.0),
            voice_every=max(1, _int_env(env, "BUDDY_CHATTER_VOICE_EVERY", 1)),
            batch=max(1, _int_env(env, "BUDDY_CHATTER_BATCH", 6)),
            agent=env.get("BUDDY_CHATTER_AGENT", CLAUDE_CODE),
            claude_bin=env.get("BUDDY_CHATTER_CLAUDE_BIN", "claude"),
            model=env.get("BUDDY_CHATTER_MODEL", "sonnet"),
            effort=env.get("BUDDY_CHATTER_EFFORT", "low"),
            claude_timeout=_float_env(env, "BUDDY_CHATTER_CLAUDE_TIMEOUT", 120.0),
            codex_bin=env.get("BUDDY_CHATTER_CODEX_BIN", "codex"),
            codex_model=env.get("BUDDY_CHATTER_CODEX_MODEL", ""),
            codex_timeout=_float_env(env, "BUDDY_CHATTER_CODEX_TIMEOUT", 180.0),
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
        raw: Any = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    # `isinstance` alone narrows `raw` to `dict[Unknown, Unknown]`; the
    # cast gives `.get` below a concrete value type to work with.
    obj = cast("dict[str, Any]", raw)
    kind = obj.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        return None
    detail = obj.get("detail", "")
    if not isinstance(detail, str):
        detail = ""
    agent = obj.get("agent", "")
    if not isinstance(agent, str):
        agent = ""
    # The detail is pasted into a prompt, so it is clamped here rather
    # than trusting the sender to have been reasonable about it. The
    # agent name never reaches a prompt, but it is a stranger's string
    # all the same and only the first few characters can ever matter.
    return Event(kind, " ".join(detail.split())[:120], agent[:40])


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


class BatchedLineSource:
    """Batching, caching, cleanup and failure handling for a generator.

    Generating one line per utterance would be a round trip every time
    the device opens its mouth. A batch covers several minutes, and the
    latency of producing it is invisible because the only thread waiting
    on it is the chatter's own.

    The batch is filled from the context that happened to be current
    when it ran dry, so later lines in a batch lag what is going on.
    That is the trade being made deliberately: this is muttering, not
    commentary.

    Subclasses supply `_generate`. Everything it can raise is caught
    here and counted: a chatter that goes silent is acceptable, a
    chatter that takes a thread down with it is not.
    """

    # Which model family answered. Reported in `status()` so that "the
    # device is saying odd things" can be traced to a backend.
    backend = "none"

    def __init__(self, cfg: ChatterConfig, rng: random.Random | None = None) -> None:
        self._cfg = cfg
        self._rng = rng or random.Random()
        self._cache: deque[str] = deque()
        self._prompt: str | None = None
        self.generated = 0
        self.failures = 0
        self.last_error = ""

    @property
    def model(self) -> str:
        """The model this will ask, for reporting. May be empty."""
        return ""

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

    def _angle(self) -> str:
        """Two hints from `_ANGLES`, drawn fresh for every batch.

        Two rather than one because a single hint reads as an
        instruction to talk about it, and this is muttering: a pair the
        model can lean on either side of leaves it room to ignore both.
        """
        first, second = self._rng.sample(_ANGLES, 2)
        return f"{first}と{second}"

    def _user_prompt(self, context: Sequence[Event]) -> str:
        """What happened, what was already said, and where to look next.

        The two halves are kept apart rather than rendered as one event
        log: they are read differently. The events are what to talk
        about, the past lines are what not to say again.
        """
        spoken = [ev.detail for ev in context if ev.kind == SAID and ev.detail]
        happened = [ev for ev in context if ev.kind != SAID]
        parts = [f"直近の出来事:\n{describe(happened)}"]
        if spoken:
            said = "\n".join(f"- {line}" for line in spoken)
            parts.append(f"すでに言ったこと。話題も言い回しも繰り返さない:\n{said}")
        parts.append(f"今回は{self._angle()}のあたりから。独り言を {self._cfg.batch} 個。")
        return "\n\n".join(parts)

    def next_line(self, context: Sequence[Event]) -> str | None:
        if not self._cache:
            self._fill(context)
        if not self._cache:
            return _clean(self._rng.choice(_FALLBACK_LINES), self._cfg.max_chars)
        return self._cache.popleft()

    def _fill(self, context: Sequence[Event]) -> None:
        try:
            lines = self._generate(context)
        except Exception as exc:  # the chatter degrades, it does not raise
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            return

        # The prompt asks for something new; this is what happens when
        # the answer only partly obliges. Exact matches only — deciding
        # that two different sentences are "too similar" needs a
        # threshold, and a wrong threshold silently eats good lines.
        seen = {ev.detail for ev in context if ev.kind == SAID}
        for raw in lines:
            cleaned = _clean(raw, self._cfg.max_chars)
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            self._cache.append(cleaned)
            self.generated += 1

    def _generate(self, context: Sequence[Event]) -> Sequence[object]:
        """One batch of raw lines. Free to raise; `_fill` counts it."""
        raise NotImplementedError


class ClaudeCliLineSource(BatchedLineSource):
    """Generates lines by running one `claude -p` turn. Claude Code's.

    ### Why the CLI and not the SDK

    Same reason as the Codex side: the credentials are the CLI's. Claude
    Code authenticates however the person running it authenticated — a
    subscription in the keychain, an API key, a third-party provider —
    and reproducing that resolution here would mean reimplementing it
    and then keeping up with it. Spawning the CLI inherits it by
    construction. A machine that can run Claude Code can run this, with
    nothing configured twice.

    ### Why the turn is stripped down

    `claude -p` is a whole agent and this wants a sentence.

    - `--safe-mode` turns off hooks, MCP servers, skills and CLAUDE.md
      discovery. Hooks are the one that would actually bite: this
      repository registers a hook that datagrams the chatter, so a turn
      that loaded it would have the chatter generating from its own
      generation.
    - `--tools ""` leaves only the structured-output tool, so the turn
      cannot read or write anything.
    - `--no-session-persistence` keeps a transcript from accumulating on
      disk once per mutter.
    - The cwd is an empty temporary directory, so no project config is
      anywhere above it.

    `--json-schema` gets the same `{"lines": [...]}` the Codex path
    asks for, so both sides parse identically.
    """

    backend = "claude-cli"

    def __init__(
        self,
        cfg: ChatterConfig,
        rng: random.Random | None = None,
        run: Callable[[str, str], str] | None = None,
    ) -> None:
        super().__init__(cfg, rng)
        # Injectable so the tests do not spawn a CLI, and so a different
        # launcher can be dropped in without touching parsing.
        self._run = run if run is not None else self._run_claude

    @property
    def model(self) -> str:
        return self._cfg.model

    def _generate(self, context: Sequence[Event]) -> Sequence[object]:
        stdout = self._run(self._system_prompt(), self._user_prompt(context))
        return cast("Sequence[object]", _cli_answer(stdout)["lines"])

    def _run_claude(self, system: str, prompt: str) -> str:
        """Run one turn and return its stdout. Raises on trouble."""
        argv = [
            self._cfg.claude_bin,
            "-p",
            "--model",
            self._cfg.model,
            "--safe-mode",
            "--no-session-persistence",
            "--tools",
            "",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(LINES_SCHEMA),
            "--system-prompt",
            system,
        ]
        if self._cfg.effort:
            argv += ["--effort", self._cfg.effort]
        with tempfile.TemporaryDirectory(prefix="buddy-chatter-") as tmp:
            # The prompt goes on stdin rather than the argument list: the
            # events and the past lines grow with the session.
            proc = subprocess.run(
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self._cfg.claude_timeout,
                # An empty directory rather than wherever the server was
                # launched from, so nothing above it steers the turn.
                cwd=tmp,
                check=False,
            )
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            # stderr is where a missing install, an expired login and a
            # bad flag all land. Truncated because this ends up in a
            # status field.
            detail = " ".join((proc.stderr or proc.stdout or "").split())[-300:]
            raise RuntimeError(f"claude -p wrote no answer (rc {proc.returncode}): {detail}")
        return proc.stdout


def _as_object(value: object) -> Mapping[str, Any] | None:
    """A decoded JSON object, or None if it decoded to anything else."""
    return cast("Mapping[str, Any]", value) if isinstance(value, dict) else None


def _cli_answer(stdout: str) -> Mapping[str, Any]:
    """Pull the structured answer out of `claude -p --output-format json`.

    Two shapes are accepted because two shapes have been seen: the
    documented single result object, and the whole stream as a list with
    the result last. Taking the last `result` entry covers both without
    having to know which the installed CLI does.

    `structured_output` is preferred over `result`; the latter is the
    same JSON as text and is the fallback for a CLI that does not fill
    the former in.
    """
    parsed: object = json.loads(stdout)
    if isinstance(parsed, list):
        objects = [_as_object(m) for m in cast("list[object]", parsed)]
        results = [obj for obj in objects if obj is not None and obj.get("type") == "result"]
        if not results:
            raise RuntimeError("claude -p emitted no result message")
        result = results[-1]
    else:
        found = _as_object(parsed)
        if found is None:
            raise TypeError(f"claude -p answered with {type(parsed).__name__}, not an object")
        result = found
    if result.get("is_error"):
        detail = " ".join(str(result.get("result", "")).split())[-300:]
        raise RuntimeError(f"claude -p reported an error: {detail}")
    structured = result.get("structured_output")
    if isinstance(structured, dict):
        return cast("Mapping[str, Any]", structured)
    return cast("Mapping[str, Any]", json.loads(result["result"]))


class CodexLineSource(BatchedLineSource):
    """Generates lines by running one `codex exec` turn. Codex's backend.

    ### Why the CLI and not an SDK

    "The model Codex uses" is not a fixed endpoint. It is whatever
    `~/.codex/config.toml` resolves to — a `model_provider` that may be
    a local proxy, and credentials that may be a rotating ChatGPT token
    rather than an API key. Reproducing that resolution here would mean
    reimplementing Codex's config and auth and then keeping up with
    them. Spawning the CLI inherits both by construction, and costs a
    process on a thread that has minutes to spare.

    ### Why a sandboxed, ephemeral turn

    `codex exec` is a whole agent, tools included, and this wants a
    sentence. `--sandbox read-only` and `--ephemeral` reduce it to what
    is actually needed: no writes, no session file, nothing left behind.
    `--output-schema` gets the same `{"lines": [...]}` the Claude path
    asks for, so both sides parse identically.

    The prompt is one blob rather than a system/user pair — `codex exec`
    has no system slot. The persona goes first, which is the same order
    the API would have applied it in.
    """

    backend = "codex"

    def __init__(
        self,
        cfg: ChatterConfig,
        rng: random.Random | None = None,
        run: Callable[[str], str] | None = None,
    ) -> None:
        super().__init__(cfg, rng)
        # Injectable so the tests do not need a Codex install, and so a
        # different launcher can be dropped in without touching parsing.
        self._run = run if run is not None else self._run_codex

    @property
    def model(self) -> str:
        return self._cfg.codex_model or "codex-default"

    def _generate(self, context: Sequence[Event]) -> Sequence[object]:
        prompt = f"{self._system_prompt()}\n\n---\n\n{self._user_prompt(context)}"
        return cast("Sequence[object]", json.loads(self._run(prompt))["lines"])

    def _run_codex(self, prompt: str) -> str:
        """Run one turn and return the final message. Raises on trouble."""
        with tempfile.TemporaryDirectory(prefix="buddy-chatter-") as tmp:
            work = Path(tmp)
            schema = work / "schema.json"
            schema.write_text(json.dumps(LINES_SCHEMA), encoding="utf-8")
            answer = work / "lines.json"
            argv = [
                self._cfg.codex_bin,
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--color",
                "never",
                "--output-schema",
                str(schema),
                "--output-last-message",
                str(answer),
            ]
            if self._cfg.codex_model:
                argv += ["--model", self._cfg.codex_model]
            # `-` reads the prompt from stdin, which keeps a persona of
            # any length off the argument list.
            argv.append("-")
            proc = subprocess.run(
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self._cfg.codex_timeout,
                # The turn is read-only and ephemeral; give it the empty
                # directory rather than wherever the server was launched
                # from, so no project config or AGENTS.md steers it.
                cwd=tmp,
                check=False,
            )
            if not answer.is_file():
                # stderr is where a missing login, an unreachable
                # provider and a config error all land. Truncated
                # because this ends up in a status field.
                detail = " ".join((proc.stderr or proc.stdout or "").split())[-300:]
                raise RuntimeError(f"codex exec wrote no answer (rc {proc.returncode}): {detail}")
            return answer.read_text(encoding="utf-8")


def line_source_for(agent: str, cfg: ChatterConfig, rng: random.Random | None = None) -> LineSource:
    """The generator that goes with an agent. The pairing is fixed.

    Deliberately not a 2x2: each agent gets the model its machine is
    already set up and paying for, and offering the cross combinations
    would mean supporting credentials nobody has.
    """
    if agent == CODEX:
        return CodexLineSource(cfg, rng)
    return ClaudeCliLineSource(cfg, rng)


class RoutingLineSource:
    """Delegates to whichever backend matches the agent now connected.

    Built once and kept for the life of the chatter, because the
    identity can change under it: the standalone runner starts with no
    witness at all, and the MCP server learns who its client is at the
    handshake rather than at import.

    Backends are constructed on first use and then kept. Building one
    is cheap; spawning either CLI is not, so a session that never speaks
    as Codex never looks for a Codex install.
    """

    def __init__(
        self,
        cfg: ChatterConfig,
        identity: AgentIdentity,
        rng: random.Random | None = None,
        factory: Callable[[str, ChatterConfig, random.Random | None], LineSource] | None = None,
    ) -> None:
        self._cfg = cfg
        self._identity = identity
        self._rng = rng
        self._factory = factory if factory is not None else line_source_for
        self._sources: dict[str, LineSource] = {}
        self._active: LineSource | None = None

    @property
    def agent(self) -> str:
        return self._identity.current

    @property
    def backend(self) -> str:
        active = self._active
        if active is not None:
            return getattr(active, "backend", "")
        # Nothing built yet: name what would be, without building it.
        return "codex" if self.agent == CODEX else "claude-cli"

    @property
    def model(self) -> str:
        return getattr(self._active, "model", "") if self._active is not None else ""

    @property
    def generated(self) -> int:
        return sum(int(getattr(s, "generated", 0)) for s in self._sources.values())

    @property
    def failures(self) -> int:
        return sum(int(getattr(s, "failures", 0)) for s in self._sources.values())

    @property
    def last_error(self) -> str:
        return str(getattr(self._active, "last_error", "")) if self._active is not None else ""

    def next_line(self, context: Sequence[Event]) -> str | None:
        agent = self.agent
        source = self._sources.get(agent)
        if source is None:
            source = self._factory(agent, self._cfg, self._rng)
            self._sources[agent] = source
        self._active = source
        return source.next_line(context)


class ChatterService:
    """Listens for hook events and speaks, on its own two threads.

    `link_provider` returns the live link or None; it is called on every
    utterance rather than held, because the MCP server drops and rebuilds
    its link across a reconnect and the chatter must not pin a dead one.

    `device_lock` is the MCP server's — held blocking by tool calls, and
    taken here only if it is free.

    `identity` is who is driving. The MCP server writes to it from the
    handshake and the chatter writes to it from any event that names a
    sender; the default source reads it to pick a model. Passed in
    rather than owned so the server and the chatter share one.
    """

    def __init__(
        self,
        cfg: ChatterConfig,
        link_provider: Callable[[], ChatLink | None],
        device_lock: threading.Lock,
        source: LineSource | None = None,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
        identity: AgentIdentity | None = None,
    ) -> None:
        self._cfg = cfg
        self._link = link_provider
        self._lock = device_lock
        self._identity = identity if identity is not None else AgentIdentity(cfg.agent)
        self._source = source if source is not None else RoutingLineSource(cfg, self._identity, rng)
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

    @property
    def identity(self) -> AgentIdentity:
        """Who is driving. Shared with the MCP server, which also writes it."""
        return self._identity

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
        if ev is not None and ev.agent:
            # Before the due check, not after: an event that arrives
            # inside the gap says nothing worth speaking to, but it
            # still tells us who is at the keyboard.
            self._identity.observe(ev.agent)
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
            "agent": self._identity.current,
            "client": self._identity.client_name,
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

    from buddy_link import ResidentLink

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
        "--agent",
        choices=(CLAUDE_CODE, CODEX),
        default=None,
        help="which backend writes the lines; there is no MCP handshake to ask",
    )
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
    if args.agent is not None:
        cfg = replace(cfg, agent=args.agent)

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
