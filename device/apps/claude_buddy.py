# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 MAS Event + Design, LLC
# Copyright 2026 usadamasa
#
# Modified from moremas/build-with-claude: the USB-serial transport, the
# chat and speech verbs intercepted in `on_line`, and the removal of the
# BLE and keyboard paths. See the docstring.
"""Claude Buddy for the M5 Cardputer-Adv.

Derived from moremas/build-with-claude (Apache-2.0), `buddy/device/apps/
claude_buddy.py`. Two things differ. The transport is the USB CDC
console rather than a BLE peripheral, which is what lets Claude Code
drive the device instead of Claude Desktop's Hardware Buddy (see
`buddy/serial.py`). And the paths upstream drove from hardware buttons
— permission responses, unpair confirmation, quit-to-launcher — are
gone, along with the BLE branches that were kept beside them only so a
diff against upstream stayed readable.

This is a port of the Basic's `buddy_app.py` to a device with a 240x135
LCD instead of 320x240 and no accessible battery IC (Cardputer-Adv
ships with a different power rail that we don't bother reading here).
The wire protocol, persistent state, character-receive logic and the
dashboard itself are upstream and unmodified — `buddy_protocol`,
`buddy_state`, `buddy_chars` and `buddy_ui_cp` are read off flash and
never carried in this repository. What is local is the transport, the
chat panel, and speech.

### Install layout

UIFlow 2.0's default sys.path on this build is roughly:
  ['', '.frozen', '/lib', '/system', '/flash/libs']
Notably /flash itself is NOT on the path, even though that is where
main.py lives. The peer modules sit at /flash/ root and this file in
/flash/apps/, so both are prepended on entry. `buddy_deploy.py` is what
puts them there.

### Input

There is none. The keyboard is not read; the only input is the host,
over serial. Upstream mapped Y / N / Q to approve / deny / quit, but
this build has no permission prompt to answer — the host never sends
one — and no launcher to quit back to. Issue #33 is where device-side
input comes back, and the direction there is voice rather than keys.

The hint strip along the bottom of the dashboard still reads Y/N/Q.
`buddy_ui_cp` paints it, and that module is upstream and lives only on
flash, so correcting the text means taking ownership of it. Left alone
until #33 decides what it should say.

### Exit

Ctrl-C. It arrives as a KeyboardInterrupt in the main loop below, which
tears the transport down and returns onto a live REPL — see the
`to_repl` branches in the `finally`. That is the only deliberate way
out. The other way the loop ends is an unhandled exception, and that
one reboots: UIFlow 2.0 has no return-to-launcher API, so when a user
app's `run()` ends the launcher does not repaint and the screen stays
frozen on whatever the app drew last. `machine.reset()` lands the board
back at the launcher instead.
"""

import sys

# Before the first `import buddy_*` below, otherwise we ImportError at
# load time and there is no graceful way to report it. See the
# docstring's "Install layout" for what is on the path already.
for _p in ("/flash", "/flash/apps"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import json
import time

# On flash but not in this repository: the firmware's own modules and the
# upstream peers. `buddy_deploy.py` reads the latter off the board.
import buddy_chars
import buddy_protocol
import buddy_state
import buddy_ui_cp as buddy_ui
import M5
import machine

# Ours, under device/buddy/. Aliased to the flat names the rest of this
# file already used — what moved is where they live on flash, not what
# they are.
from buddy import chat as buddy_chat
from buddy import serial as buddy_serial
from buddy import speak as buddy_speak

# ---- chat routing
#
# buddy_protocol.py is upstream and unmodified on flash; its dispatcher
# logs any verb it doesn't know as "unknown cmd". So the chat verbs are
# peeled off in on_line() below, before the protocol layer sees them.
# This substring test is a cheap pre-filter so ordinary traffic doesn't
# pay for a second JSON parse — see buddy/chat.py for the commands.
_CHAT_TAG = b'"chat.'
_SPEAK_TAG = b'"speak.'
# Same trick again for the debug verbs, and here the pre-filter is the
# entire resident cost of the feature: `buddy.debug` is not imported
# until one of these arrives, and is dropped again on `dbg.off`. See
# that module's docstring for why it is kept off the heap.
_DBG_TAG = b'"dbg.'
# There is deliberately no net.* verb. main.py connects at boot from the
# credentials in wifi_event.py (written by host/provision_wifi.py), and
# this app inherits that link. `connect()` from in here is accepted and
# never completes — the ESP-IDF heap is down to ~12 KB in its largest
# region by the time we run — so nothing here touches `network`.


# ---- battery stub
#
# The Basic's buddy_app.py talks to an IP5306 over I2C(0, sda=21,
# scl=22). The Cardputer-Adv has a completely different power
# architecture — there's no IP5306, and the battery/USB state lives in
# a chip we haven't wired up here. Stub the reader out so the protocol
# and UI layers still see the shape they expect; the footer will show
# "100%  USB" steady-state, which is a deliberate lie but a benign one.
# A follow-up can swap this for the real AXP2101/AW9523 reader once
# someone digs out the register map.
def _stub_battery():
    # type: () -> dict[str, object]
    return {"pct": 100, "mV": 0, "mA": 0, "usb": True}


def run():
    # Per-step prints so a hard fault during init (an LCD driver crash,
    # say) leaves a breadcrumb on the serial console pointing at which
    # step faulted. C-level crashes bypass every try/except and reboot
    # the chip, so the last print before reboot is the only diagnostic
    # we get.
    print("claude_buddy: run() start")

    ui = buddy_ui.BuddyUI()
    print("claude_buddy: ui ready")
    # Owns the main panel (y=22..110) whenever a transcript is up. The
    # header and hint strip stay with BuddyUI, but its footer overlaps
    # us, so update_footer is gated on `chat.active` in the loop below.
    chat = buddy_chat.ChatPanel()
    print("claude_buddy: chat ready", chat.info())
    state = buddy_state.BuddyState()
    print("claude_buddy: state ready")
    ui.update_identity(state.name, state.owner)

    buddy_chars.sweep_partials()
    chars = buddy_chars.CharReceiver()
    print("claude_buddy: chars ready")

    # Protocol needs a handle on the transport (for disconnect /
    # forget_bonds), and the transport needs the on_line callback which
    # needs the protocol. Same indirection trick as the Basic: stash the
    # protocol in a 1-slot dict that the callback reads at event time.
    proto_holder = {"p": None}  # type: dict[str, object]

    # Set when a chat command changed what should be on screen. Drawing
    # happens in the main loop rather than here, for the same reason the
    # state mailbox below exists. Upstream needed that because BLE
    # dispatched from micropython.schedule context and a callback could
    # land mid-way through someone else's SPI sequence; the serial
    # transport delivers from `poll()` in the loop itself, so what is
    # left is ordering discipline — every LCD write in one place, which
    # is what the footer/chat overlap below depends on. The ack is a
    # plain write with no LCD involvement, so that goes out immediately.
    chat_dirty = [False]  # type: list[bool]

    # One slot holding buddy.debug once something has asked for it. A
    # list rather than a `global` so the closure can rebind it, same
    # pattern as proto_holder above.
    dbg_holder = {"m": None}  # type: dict[str, object]

    def on_dbg(raw):
        # type: (bytes | bytearray | str) -> dict[str, object] | None
        # `dbg_holder["m"]` is declared `object` so it can hold either
        # None or the buddy.debug module — the mailbox pattern above
        # loses the concrete module type across calls, so reading it back
        # here is ignored per-line rather than left to cascade.
        mod = dbg_holder["m"]
        # True only on the frame that pulled the module in. The host
        # cannot work this out for itself — a fresh CLI process has no
        # idea whether a previous one already loaded it — so the
        # transition is reported rather than inferred, and the host
        # decides what to do about it (currently: say so out loud).
        entered = mod is None
        if mod is None:
            try:
                from buddy import debug as buddy_debug
            except ImportError as e:
                # A bundle deployed before buddy.debug existed. Say so in
                # an ack rather than letting the ImportError escape into
                # the transport callback and take the loop down.
                return {"ack": "dbg", "ok": False, "err": "buddy.debug not on flash: " + str(e)}
            mod = dbg_holder["m"] = buddy_debug
            # The live objects an expression should be able to name.
            # `speech` and `proto` are read through the enclosing scope,
            # so both are already assigned by the time a frame arrives.
            mod.bind(
                {
                    "ble": ble,
                    "chars": chars,
                    "chat": chat,
                    "proto": proto_holder["p"],
                    "speech": speech,
                    "state": state,
                    "ui": ui,
                }
            )
        ack = mod.handle_raw(raw)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if ack is not None and entered and not ack.get("unload"):  # pyright: ignore[reportUnknownMemberType]
            # A `dbg.off` that had to import the module in order to
            # unload it has not entered anything, so it does not say so.
            ack["entered"] = True
        if ack is not None and ack.get("unload"):  # pyright: ignore[reportUnknownMemberType]
            # Every reference has to go before the collect, or the
            # number in the ack is measured against a heap the module
            # is still sitting in. `mod` is the one that is easy to
            # miss: it outlives the other two until this function
            # returns.
            mod = None
            dbg_holder["m"] = None
            if "buddy.debug" in sys.modules:
                del sys.modules["buddy.debug"]
            # sys.modules is not the only reference. MicroPython also
            # stores a submodule as an attribute of its package, and
            # that one outlives the entry above — measured on the
            # device, where `delattr` on a module object works and a
            # later `from buddy import debug` reads flash again. Without
            # it the module stays on the heap and the ack's `free`
            # counts it as still in use.
            pkg = sys.modules.get("buddy")
            if pkg is not None:
                try:
                    delattr(pkg, "debug")
                except AttributeError:
                    pass
            gc.collect()
            ack["free"] = gc.mem_free()
        return ack  # pyright: ignore[reportUnknownVariableType]

    def on_line(raw):
        # type: (bytes) -> None
        # Always bytes here: buddy.serial._handle_line() decodes nothing
        # before calling on_line, unlike the wider bytes|bytearray|str
        # accepted by chat/speech/dbg's own handle_raw() below.
        if _CHAT_TAG in raw and ble is not None:
            ack = chat.handle_raw(raw)
            if ack is not None:
                ble.send_line(json.dumps(ack, separators=(",", ":")).encode("utf-8"))
                chat_dirty[0] = True
                return
        # speak.say fetches the audio from the engine before it answers,
        # which blocks this loop for the length of synthesis. It has to
        # be synchronous anyway: the ack reports the length and the rate
        # the engine actually used, neither of which is known until the
        # response headers are in.
        if _SPEAK_TAG in raw and ble is not None and speech is not None:
            ack = speech.handle_raw(raw)
            if ack is not None:
                ble.send_line(json.dumps(ack, separators=(",", ":")).encode("utf-8"))
                return
        # Last of the pre-filters: debug traffic is rare, and putting it
        # behind the two that are not keeps the common path unchanged.
        if _DBG_TAG in raw and ble is not None:
            ack = on_dbg(raw)
            if ack is not None:
                ble.send_line(json.dumps(ack, separators=(",", ":")).encode("utf-8"))
                return
        # `proto_holder["p"]` is declared `object` so the mailbox can hold
        # None before the protocol exists; reading it back loses the
        # concrete BuddyProtocol type, so the call below is ignored
        # per-line rather than left to cascade.
        p = proto_holder["p"]
        if p is not None:
            p.on_line(raw)  # pyright: ignore[reportUnknownMemberType]

    # State changes go through a mailbox rather than straight into the
    # UI, so that every LCD write happens at one point in the loop
    # below. `send_hello` is the exception: it is a plain write to the
    # transport with no LCD involvement, so it goes out from the
    # callback.
    # PEP 484 type comments rather than annotations: MicroPython has no
    # `typing`, and a one-slot mailbox would otherwise be inferred as
    # list[None] and reject every store.
    pending_state = [None]  # type: list[str | None]

    # Pre-bound because the callbacks above read them at call time and
    # the transport's constructor takes `on_line` before either name
    # exists. Nothing calls back during construction on this transport —
    # `poll()` is the only way in — so the guards are belt-and-braces,
    # and cheap enough to leave in the rx path.
    ble = None
    speech = None

    def on_state_change(s: str) -> None:
        # BuddySerial has no pairing step, so the "connected" it emits
        # on handshake is terminal. Remap it to "encrypted" so the UI
        # advances past the PAIR... badge and the protocol starts
        # emitting its hello.
        effective = s
        if s == "connected":
            effective = "encrypted"
        print("claude_buddy: state", s, "->", effective)
        pending_state[0] = effective
        if effective == "encrypted":
            p = proto_holder["p"]
            if p is not None:
                p.send_hello()  # pyright: ignore[reportUnknownMemberType]

    # A full pass before the transport and the speech player allocate
    # their buffers. Everything up to here — the UI, the chat panel's
    # font, the state file — has churned the heap, and this is the last
    # quiet moment to hand the fragments back.
    import gc

    gc.collect()
    print("claude_buddy: gc done, free=", gc.mem_free())
    # `ble` is upstream's name for the transport and the name `dbg.eval`
    # binds it under; what it holds here is a BuddySerial.
    ble = buddy_serial.BuddySerial(on_line=on_line, on_state=on_state_change)
    print("claude_buddy: transport ready")

    # Speech reaches the engine over WiFi, which main.py brought up at
    # boot. Nothing in the transport is involved beyond the ack.
    speech = buddy_speak.SpeechPlayer(ble)
    print("claude_buddy: speech ready")

    proto = buddy_protocol.BuddyProtocol(
        state=state,
        ui=ui,
        chars=chars,
        ble=ble,
        battery_reader=_stub_battery,
    )
    proto_holder["p"] = proto

    ui.update_footer(state.stats(), _stub_battery())

    # Everything large is allocated by now, so this is the right moment
    # to tell the collector to run early rather than late. The default
    # is to collect when an allocation fails, which on a heap this
    # fragmented is the point at which it is already too late — the
    # MemoryError that took `import buddy_ui_cp` down had 55 KB free.
    # The docs' recipe: collect once a quarter of the free heap has been
    # handed out.
    gc.collect()
    gc.threshold(gc.mem_alloc() + gc.mem_free() // 4)
    print("claude_buddy: gc threshold set, free=", gc.mem_free())
    print("Claude Buddy up as", ble.advertised_name)

    last_footer_ms = time.ticks_ms()
    footer_interval = 3000

    # Set by the Ctrl-C handler below. It is the one exit that does not
    # reboot: the point of pressing it is to get the REPL, and a reset
    # would spend ten seconds bringing WiFi back up before handing over
    # the same prompt.
    to_repl = False

    try:
        while True:
            # The transport has no callback of its own: this is what
            # drains stdin and turns whole lines into on_line calls.
            ble.poll()

            # Right behind the transport drain and ahead of everything
            # that paints: one 2 KiB block is 64 ms of audio and takes
            # ~11 ms to read, so the 40 ms tick stays ahead of playback
            # only if nothing slow gets in front of it.
            speech.pump()

            # Drain the deferred UI work here so LCD writes don't
            # interleave with the periodic footer paint or the prompt
            # rendering kicked off by protocol events. set_connection in
            # particular repaints the whole header strip, which is
            # several SPI transactions long.
            new_state = pending_state[0]
            if new_state is not None:
                pending_state[0] = None
                ui.set_connection(new_state)
                if chat.active:
                    # set_connection repaints the main panel, which is
                    # the chat's while a transcript is up.
                    chat.render()

            if chat_dirty[0]:
                chat_dirty[0] = False
                if chat.active:
                    chat.render()
                else:
                    # A chat.clear handed the panel back. The chat covers
                    # the header too, so the dashboard needs a full
                    # chrome repaint and not just the main panel.
                    # _redraw_chrome is upstream's own private helper for
                    # exactly this; if a future bundle renames it, fall
                    # back to the public calls that cover everything
                    # except the header.
                    redraw = getattr(ui, "_redraw_chrome", None)
                    if redraw is not None:
                        redraw()
                    else:
                        ui.update_heartbeat({})
                        ui.restore_button_hints()
                    ui.update_footer(state.stats(), _stub_battery())

            now = time.ticks_ms()
            if time.ticks_diff(now, last_footer_ms) >= footer_interval:
                state.tick_nap()
                # The stats footer paints y=96..110, which is inside the
                # chat's region. Skipping it is what keeps a transcript
                # from being punched through every 3 seconds. It is also
                # several SPI transactions long, and during playback the
                # tick has no room to spare — an underrun is audible,
                # a three-second-stale battery reading is not.
                if not chat.active and not speech.active:
                    ui.update_footer(state.stats(), _stub_battery())
                last_footer_ms = now

            # 40 ms matches buddy_app.py. It is what the speech pump
            # above is sized against: one 2 KiB block is 64 ms of audio,
            # so a tick this long stays ahead of playback.
            time.sleep_ms(40)
    except KeyboardInterrupt:
        # Reachable again. `buddy.serial` used to disable Ctrl-C because
        # a JSON payload could carry a 0x03; the host escapes control
        # bytes and the binary bulk mode that needed the raw channel is
        # gone, so the interrupt is back and this is where it lands.
        to_repl = True
        print("claude_buddy: interrupted")
    except Exception as e:
        # The `finally` below reboots, and a reboot is faster than the
        # console: without this the traceback that explains the crash is
        # cut off mid-line, or never printed at all. Swallowed rather
        # than re-raised because the reset ends the process either way,
        # and re-raising only risks a second unprintable exception.
        print("claude_buddy: unhandled exception in the main loop")
        # MicroPython-only; not in CPython's real `sys` (which typeshed
        # models here), so the attribute itself is Unknown to basedpyright.
        sys.print_exception(e)  # pyright: ignore[reportUnknownMemberType]
    finally:
        # Mirror buddy_app.py's teardown ordering: the transport first,
        # then wipe the screen to black, then hand control back. Before
        # the transport goes: stop() drops the transfer through it, and
        # it is the only thing that silences a speaker still working
        # through a second of queued audio.
        try:
            speech.stop()
        except Exception as e:
            print("claude_buddy: speech stop warning:", e)
        try:
            ble.deinit()
        except Exception as e:
            print("claude_buddy: deinit warning:", e)
        try:
            M5.Lcd.fillScreen(buddy_ui.BLACK)
        except Exception as e:
            print("claude_buddy: screen-clear warning:", e)
        if to_repl:
            # A black screen and no reboot is indistinguishable from a
            # bricked board across the desk. One word is enough to say
            # which of the two it is.
            try:
                M5.Lcd.setTextColor(buddy_ui.GRAY_DIM, buddy_ui.BLACK)
                M5.Lcd.drawString("REPL", 8, 8)
            except Exception as e:
                print("claude_buddy: repl banner warning:", e)
        # UIFlow has no launcher-return API; machine.reset() is the
        # only way back to App List. Same pattern hello_cardputer.py
        # uses. Brief pause so a trailing log line doesn't get truncated
        # mid-line on the USB console.
        time.sleep_ms(200)
        if to_repl:
            # deinit() has already put Ctrl-C back and dropped the
            # transport's hold on stdin, so returning from run() lands on
            # a live prompt with the app's objects collectable. That is
            # the whole point of the interrupt — resetting here would
            # throw away a REPL that took a keypress to reach.
            print("claude_buddy: at the REPL. machine.reset() to restart.")
        else:
            machine.reset()


# UIFlow 2.4.x's App List has been observed to invoke apps both as
# __main__ (file run directly) and via import (when picked through
# the menu vs. the file system, depending on version). The previous
# if/else with both arms calling run() was just dispatching to
# itself; collapse it so the empirical behavior is the documented
# behavior. `main.py` and `buddy_link.LAUNCH_SOURCE` both take the
# import path. The trade-off is that importing this module from
# CPython for inspection starts the app — but the imports above (M5,
# machine) already only resolve on-device, so that path isn't a real
# use case.
run()
