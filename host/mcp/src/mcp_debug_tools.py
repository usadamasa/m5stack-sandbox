"""走っているアプリを覗くための tool。

アプリは走っている間コンソールを占有するので、「ヒープはどうなっている」に
答えるはずの REPL は、見る価値のある状態が存在している間だけ存在しない。この
2 つがその入口で、片方は走っているアプリに訊き、もう片方はアプリを終わらせて
プロンプトを返させる。

状態は持たない。`mcp_state` の `server` に登録するだけのモジュール。
"""

from __future__ import annotations

import time
from typing import Any

import mcp_state
from buddy_verbs import DEBUG_OPS, announce_debug_entry, debug
from device_repl import ReplError
from mcp_state import server


@server.tool()
def buddy_debug(
    op: str = "mem",
    src: str = "",
    timeout: float = 8.0,
    settle: float = 0.4,
    announce: bool = True,
) -> dict[str, Any]:
    """Inspect the running app in place, without stopping it.

    `op` is one of:

      mem    both heaps. `free`/`alloc` are MicroPython's; `idf_free` and
             `idf_largest` are the ESP-IDF heap that sockets come out of,
             and the one a failing `buddy_speak` is usually short of.
      frag   dump the heap map. Arrives in `logs`, not in `ack`.
      gc     collect, and report the free heap either side of it.
      state  the transport, chat panel and speech player at a glance.
      eval   evaluate `src` against the app's live objects (`ble`, `chat`,
             `speech`, `state`, `ui`, `proto`, `chars`). Capped at 192
             characters — it compiles on the device.
      exec   run `src` as a statement. Output goes to `logs`.
      off    unload the debug module and report the heap it gave back.

    The device imports its debug module on the first of these and drops
    it on `off`, so a long inspection session is worth closing out. Bulky
    answers and tracebacks come back in `logs`; `settle` is how long we
    wait for them after the ack.

    That first call is also said out loud, so the room knows the device
    is being poked at rather than working. `announce=False` skips it —
    the announcement costs a VOICEVOX round trip and a second of
    playback, which is a long time to add to a tight measurement loop.
    """
    if op not in DEBUG_OPS:
        return {"ok": False, "error": f"unknown op {op!r}; expected one of {', '.join(DEBUG_OPS)}"}
    with mcp_state.device() as link:
        try:
            ack = debug(link, op, src=src, timeout=timeout)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        # ロックの内側で: アナウンスもデバイスとの往復なので、その間に
        # chatter を入れると ack が入れ違う。
        announced = announce and announce_debug_entry(link, ack)
        if settle:
            time.sleep(settle)
        _msgs, logs = link.events()
    return {"ok": True, "ack": ack, "announced": announced, "logs": mcp_state.decode_logs(logs)}


@server.tool()
def buddy_interrupt(settle: float = 1.0) -> dict[str, Any]:
    """Ctrl-C the running app back to the REPL. Does not reboot.

    The app tears its transport down and stops at a live prompt with the
    screen reading "REPL"; the port stays open and this link keeps
    reading it. Use it before `buddy_start_app`, or before handing the
    port to `buddy_deploy.py` — though that one needs `buddy_disconnect`
    as well, since only one process can hold the port.

    Refuses to open a port of its own: with nothing connected there is no
    app to interrupt, and claiming the port to find that out would lock
    out the tool that does need it.
    """
    with mcp_state.device_lock:
        link = mcp_state.live_link()
        if link is None:
            return {"ok": False, "error": "not connected; nothing to interrupt"}
        try:
            link.interrupt()
        except ReplError as e:
            # `tcp://` の link。console が無いので Ctrl-C の届く先が無い。
            return {"ok": False, "error": str(e)}
        if settle:
            time.sleep(settle)
        _msgs, logs = link.events()
    return {"ok": True, "logs": mcp_state.decode_logs(logs)}
