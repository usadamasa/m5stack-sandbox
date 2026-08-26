"""chatter を操る tool。

Claude が働いている間、デバイスが独り言を言う仕掛け。駆動するのは tool
呼び出しではなく Claude Code の hook が投げるデータグラムで、作業に何の
コストも掛けないことが眼目になっている。本体は `buddy_chatter`。

ここが持つのは start / stop / status の 3 つだけで、走らせている
`ChatterService` は `mcp_state` が持つ。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import mcp_state
from buddy_chatter import ChatterService
from mcp_state import server


@server.tool()
def buddy_chatter_start(
    gap_min: float = -1.0,
    gap_max: float = -1.0,
    voice_every: int = -1,
    busy_rate: float = -1.0,
    model: str = "",
    effort: str = "",
    batch: int = -1,
) -> dict[str, Any]:
    """Start the idle chatter, optionally retuning how often it talks.

    Nothing is said until a link is up (`buddy_start_app` or
    `buddy_connect`); the chatter never opens the port itself, so that
    `buddy_deploy.py` and `esptool` can still have it. With
    `BUDDY_CONNECT_ON_START=1` the server has already made one such
    opening on its own behalf as it started, which is why a session
    normally arrives here with a link already up.

    Each interval is drawn fresh from `gap_min`..`gap_max` seconds rather
    than being fixed, because a metronome is what makes this annoying.
    Where in that range it is drawn follows how busy the session is:
    `busy_rate` is the hook events per minute that count as fully busy
    and put the gap at the short end of the range. Raise it to make the
    device harder to excite. `voice_every` speaks aloud on every Nth utterance and shows
    the rest on the panel only — raise it when the room has other people
    in it.

    `model` and `effort` are what writes the lines, when Claude Code is
    the one connected: a model alias or id (`sonnet`, `haiku`,
    `claude-opus-5`) and one of `low`/`medium`/`high`/`xhigh`/`max`.
    Turn them up when the muttering has gone flat and down when it is
    costing more than it is worth. `batch` is how many lines one
    generation produces — a larger batch is cheaper per line and lags
    the session further, since later lines were written from what was
    happening when the batch was filled.

    Any numeric argument left at -1, and any string left empty, keeps
    its current value. Passing one while the chatter is already running
    restarts it with the new setting.
    """
    service = mcp_state.chatter_service()
    # 番兵が 2 種類あるのは、設定が 2 種類あるから。数値は -1 — どれも個数か
    # 時間で、負の値は意味を持たない。文字列は空 — "" は既に `effort` の
    # 「CLI の既定に任せる」という意味そのものだから。
    overrides: dict[str, Any] = {
        name: value
        for name, value in (
            ("gap_min", gap_min),
            ("gap_max", gap_max),
            ("voice_every", voice_every),
            ("busy_rate", busy_rate),
            ("batch", batch),
        )
        if value >= 0
    }
    overrides.update(
        {name: value for name, value in (("model", model), ("effort", effort)) if value}
    )
    if overrides:
        cfg = replace(service.cfg, **overrides)
        service.stop()
        service = ChatterService(cfg, mcp_state.live_link, mcp_state.device_lock)
        mcp_state.chatter = service
    service.start()
    return service.status()


@server.tool()
def buddy_chatter_stop() -> dict[str, Any]:
    """Stop the idle chatter and release its socket."""
    service = mcp_state.chatter_service()
    service.stop()
    return service.status()


@server.tool()
def buddy_chatter_status() -> dict[str, Any]:
    """Report what the chatter has been doing, and why it has not.

    `skipped_offline` counts turns where no link was up, `skipped_busy`
    counts turns where a real tool call held the device — both are
    normal. `generation_failures` with a `generation_error` means it has
    fallen back to canned lines: usually the agent's CLI missing from
    the server's PATH, or not logged in.

    `backend`, `model` and `effort` say who is writing the lines.

    `connect_on_start` appears when the server was asked to open the
    port for itself (`BUDDY_CONNECT_ON_START=1`) and says how that one
    attempt went. Absent means it was never asked.
    """
    status = mcp_state.chatter_service().status()
    if mcp_state.startup_connect is not None:
        status["connect_on_start"] = mcp_state.startup_connect
    return status
