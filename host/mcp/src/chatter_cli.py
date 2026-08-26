"""chatter をそれ自身のプロセスで走らせる口。`--once` の煙試験もここ。

普段 chatter は MCP server の中の 2 本のスレッドで、ポートを持っているのは
server の方。こちらは同じ `ChatterService` を自分のプロセスで走らせ、ポートも
自分で持つ。

在る理由は 2 つ。daemon は起動時にホストのコードを import 済みなので、
`buddy_chatter.py` を直しても走っている daemon には届かない — restart せずに
変更を試すのがここ。そしてこの機能より前に始まったセッションの最中に
デバイスを喋らせる道も、ここしかない。ポートの持ち主は 1 つきりであることを
忘れないこと: 先に `buddy_disconnect` するか `buddy-mcpd stop` し、これが
終わるまで MCP の tool はデバイスに届かなくなる。

喋る側 (`buddy_chatter`) からここを import することは無い。依存は
cli → service の一方向。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from buddy_chatter import ChatterService
from chatter_core import ChatterConfig, Event


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
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
    return parser.parse_args(argv)


def _tuned(args: argparse.Namespace) -> ChatterConfig:
    """環境からの設定に、コマンドラインで言われたぶんだけ上書きを重ねる。"""
    cfg = ChatterConfig.from_env()
    if args.gap_min is not None:
        cfg = replace(cfg, gap_min=float(args.gap_min))
    if args.gap_max is not None:
        cfg = replace(cfg, gap_max=float(args.gap_max))
    if args.busy_rate is not None:
        cfg = replace(cfg, busy_rate=float(args.busy_rate))
    if args.voice_every is not None:
        cfg = replace(cfg, voice_every=max(1, int(args.voice_every)))
    return cfg


def _silence_service_log() -> None:
    """`buddy.chatter` の行を捨てる。ここでは `report` が同じことを言うから。

    daemon には誰も見ていない log しか無いので service は自分で喋ったことを
    書く。こちらには stderr に立つ人が居て、その人向けの表示が下の `report`
    になっている。両方出すと 1 回の発話が 2 行になる。
    """
    log = logging.getLogger("buddy.chatter")
    log.handlers = [logging.NullHandler()]
    log.propagate = False


def report(status: dict[str, Any]) -> None:
    note = f"  ! {status['last_error']}" if status["last_error"] else ""
    print(
        f"[{status['spoken']:3d}] {status['last_line'] or '-'}"
        f"  (busy {status['skipped_busy']}, offline {status['skipped_offline']},"
        f" tempo {status['tempo']}, next {status['next_gap_s']}s){note}",
        file=sys.stderr,
        flush=True,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the chatter against a device this process owns."""
    from resident_link import ResidentLink

    args = _parse(argv)
    cfg = _tuned(args)
    _silence_service_log()

    link = ResidentLink(args.port)
    link.connect()
    if args.start:
        link.start_app()

    if args.once:
        # しきい値を 0 にして、いちばん最初のターンから発話が来るようにする。
        service = ChatterService(
            replace(cfg, gap_min=0.0, gap_max=0.0, idle_min=0.0, idle_max=0.0),
            lambda: link,
            threading.Lock(),
        )
        service.step(Event("session", "smoke test"))
        print(json.dumps(service.status(), ensure_ascii=False, indent=2))
        link.disconnect()
        return 0 if service.spoken else 1

    service = ChatterService(cfg, lambda: link, threading.Lock())
    service.start()
    print(f"chatter listening on {cfg.socket_path} (device {args.port})", file=sys.stderr)
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
