"""Command-line front end for the Claude Buddy protocol over USB serial.

One process, one command: take the port, ask, print what came back, let
go. `--help` has the flags. Everything it drives lives in the sibling
modules — the framing, the text that fits on the panel, the verbs, and
the link that holds the port open.
"""

from __future__ import annotations

import argparse
import json
import sys

from buddy_link import BuddyLink, launch_app
from buddy_text import DEFAULT_PACE
from buddy_verbs import (
    DEBUG_ENTER_TEXT,
    DEBUG_OPS,
    DEFAULT_RATE,
    ZUNDAMON,
    announce_debug_entry,
    debug,
    say,
    speak,
    voicevox_url,
)
from buddy_wire import Message
from device_repl import ReplError


def _dump(msgs: list[Message], logs: list[bytes]) -> None:
    for line in logs:
        print("  log |", line.decode("utf-8", errors="replace"))
    for msg in msgs:
        print("  <-- ", json.dumps(msg, ensure_ascii=False))


def _nudge_repl() -> None:
    print("waiting for the REPL — press BtnRST on the device...")


def _request(link: BuddyLink, label: str, payload: Message, timeout: float) -> None:
    ack = link.request(payload, label, timeout=timeout)
    print(f"{label}:", json.dumps(ack, ensure_ascii=False))


def _run_debug(link: BuddyLink, args: argparse.Namespace) -> None:
    for op in args.dbg:
        ack = debug(link, op, src=args.dbg_src, timeout=args.timeout)
        print(f"dbg.{op}:", json.dumps(ack, ensure_ascii=False))
        if not args.dbg_silent and announce_debug_entry(link, ack, url=args.engine):
            print(f"  (said {DEBUG_ENTER_TEXT!r})")
        # frag's heap map and a failed eval's traceback come back as log
        # lines, not in the ack. Give them a moment to arrive so they
        # print next to the ack they belong to.
        _dump(*link.pump(0.3))


def _run_requests(link: BuddyLink, args: argparse.Namespace) -> None:
    """One-shot verbs that take nothing but a flag."""
    if args.status:
        _request(link, "status", {"cmd": "status"}, args.timeout)
    if args.name is not None:
        _request(link, "name", {"cmd": "name", "name": args.name}, args.timeout)
    if args.owner is not None:
        _request(link, "owner", {"cmd": "owner", "owner": args.owner}, args.timeout)
    if args.chat_info:
        _request(link, "chat.info", {"cmd": "chat.info"}, args.timeout)
    if args.chat_clear:
        _request(link, "chat.clear", {"cmd": "chat.clear"}, args.timeout)


def _run_speech(link: BuddyLink, args: argparse.Namespace) -> None:
    for text in args.say:
        for ack in say(link, text, role=args.role, timeout=args.timeout, pace=args.pace):
            print("chat.say:", json.dumps(ack, ensure_ascii=False))

    engine: str | None = None
    if args.speak:
        # Resolved once, and before the first request, so a bad engine
        # address is reported here rather than after the device has
        # already been told to say something.
        engine = voicevox_url(args.engine)
        print(f"engine: {engine}")

    for text in args.speak:
        if not args.no_show:
            # Sent first so the words are on screen while the engine
            # synthesises, not after playback has ended.
            for ack in say(link, text, timeout=args.timeout, pace=0):
                print("chat.say:", json.dumps(ack, ensure_ascii=False))
        ack = speak(
            link,
            text,
            url=engine,
            speaker=args.speaker,
            rate=args.rate,
            timeout=args.timeout,
        )
        print("speak.end:", json.dumps(ack, ensure_ascii=False))


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("--port", required=True)
    ap.add_argument("--start", action="store_true", help="Launch the app over the REPL first.")
    ap.add_argument("--status", action="store_true", help="Request a status ack.")
    ap.add_argument("--name", help="Set the device name.")
    ap.add_argument("--owner", help="Set the owner string.")
    ap.add_argument(
        "--say",
        action="append",
        default=[],
        metavar="TEXT",
        help="Put TEXT on the device's chat panel. Repeatable.",
    )
    ap.add_argument("--role", default="claude", choices=("claude", "user", "sys"))
    ap.add_argument(
        "--pace",
        type=float,
        default=DEFAULT_PACE,
        help="Seconds between the parts of a split --say. 0 sends flat out.",
    )
    ap.add_argument("--chat-clear", action="store_true", help="Wipe the chat panel.")
    ap.add_argument("--chat-info", action="store_true", help="Report the panel's font/geometry.")
    ap.add_argument(
        "--speak",
        action="append",
        default=[],
        metavar="TEXT",
        help="Have the device fetch TEXT from VOICEVOX and play it. Repeatable.",
    )
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument(
        "--wait",
        type=float,
        default=180.0,
        help="Seconds to wait for the REPL when --start cannot interrupt its way there.",
    )
    ap.add_argument(
        "--engine",
        metavar="URL",
        help="VOICEVOX engine. Defaults to $VOICEVOX_URL, then this machine's LAN address.",
    )
    ap.add_argument(
        "--speaker",
        type=int,
        default=ZUNDAMON,
        help=f"VOICEVOX style id. {ZUNDAMON} is Zundamon (normal).",
    )
    ap.add_argument("--rate", type=int, default=DEFAULT_RATE)
    ap.add_argument(
        "--no-show",
        action="store_true",
        help="Do not also put spoken text on the chat panel.",
    )
    ap.add_argument(
        "--interrupt",
        action="store_true",
        help="Ctrl-C a running app back to the REPL. Runs before everything else.",
    )
    ap.add_argument(
        "--dbg",
        action="append",
        default=[],
        metavar="OP",
        help=f"Ask the app about itself. One of: {', '.join(DEBUG_OPS)}. Repeatable.",
    )
    ap.add_argument(
        "--dbg-src",
        default="",
        metavar="SRC",
        help="Expression or statement for --dbg eval / --dbg exec.",
    )
    ap.add_argument(
        "--dbg-silent",
        action="store_true",
        help="Do not have the device announce that it entered debug mode.",
    )
    ap.add_argument("--watch", type=float, default=0.0, help="Read traffic for N seconds and exit.")
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--settle", type=float, default=4.0, help="Seconds to wait after --start.")
    return ap


def main() -> int:
    args = _parser().parse_args()

    link = BuddyLink(args.port, baud=args.baud)
    if args.start:
        print("starting app over REPL...")
        try:
            link.open(
                adopt=launch_app(
                    args.port, args.baud, link.read_timeout, wait=args.wait, on_wait=_nudge_repl
                )
            )
        except ReplError as e:
            sys.stderr.write(f"{e}\n")
            return 1
    else:
        link.open()

    with link:
        # Whatever happens, print what the device said first. On a failed
        # launch the buffered logs are the diagnostic.
        try:
            if args.start:
                # The reader is already on the port the launch handed
                # over, so this is just letting the device talk.
                # `pump` drains as it returns; a second `drain()` would
                # get an empty batch and the startup output — including
                # the traceback from a failed import — would be gone.
                _dump(*link.pump(args.settle))

            if args.interrupt:
                # Ahead of everything else: whatever follows wants the
                # REPL, and this is what frees it.
                link.interrupt()
                _dump(*link.pump(1.0))

            _run_debug(link, args)
            _run_requests(link, args)
            _run_speech(link, args)

            if args.watch:
                print(f"watching for {args.watch:.1f}s...")
                _dump(*link.pump(args.watch))
        finally:
            _dump(*link.drain())
            if link.dropped:
                print("  !! device dropped off USB (reset)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
