"""Claude Buddy for the M5 Cardputer-Adv.

Derived from moremas/build-with-claude (Apache-2.0), `buddy/device/apps/
claude_buddy.py`. The only local change is transport selection: the BLE
peripheral can be swapped for a USB-serial link so Claude Code can drive
the device instead of Claude Desktop's Hardware Buddy. See
`_make_transport` below and `buddy_serial.py`.

This is a port of the Basic's `buddy_app.py` to a device with a QWERTY
matrix keyboard instead of three face buttons, a 240x135 LCD instead of
320x240, and no accessible battery IC (Cardputer-Adv ships with a
different power rail that we don't bother reading here). The wire
protocol, BLE stack, persistent state, and character-receive logic are
unchanged — we reuse `buddy_ble`, `buddy_protocol`, `buddy_state`, and
`buddy_chars` byte-for-byte from the Basic build. Only the I/O layer
(input → UI) is Cardputer-specific.

### Install layout

UIFlow 2.0's launcher shows any `*.py` inside `/flash/apps/` in its
"App List" menu. The peer modules go alongside this file in the same
directory, and we prepend `/flash/apps/` to sys.path on entry so
`import buddy_ble` etc. resolves. This keeps the whole bundle
self-contained in one folder — no touching /flash/ root, no clobbering
UIFlow's own main.py/boot.py.

### Input mapping

The Cardputer has a full keyboard, so we pick intuitive letters rather
than mimicking BtnA/B/C. The mapping is shown in the hint strip:

  Y / y / Enter   → approve once
  N / n           → deny
  Q / q / ESC     → quit back to the UIFlow App List

MatrixKeyboard.get_key() returns single-character strings for printable
keys and small integer codes for specials. We accept both forms for the
keys that have both — Enter (0x0D) and Escape (0x1B).

### Return-to-menu

UIFlow 2.0 has no return-to-launcher API; when a user app's `run()`
ends, the launcher does not repaint and the screen stays frozen on
whatever the app drew last. The established workaround (see
`hello_cardputer.py`) is to soft-reboot via `machine.reset()` on exit,
which lands the user back at the launcher automatically. We do that
here, in the `finally` block, *after* tearing BLE down cleanly.
"""

import sys

# Make our peer modules importable *before* the first `import buddy_ble`
# below, otherwise we ImportError at load time and the launcher has no
# graceful way to show it.
#
# UIFlow 2.0's default sys.path on this build is roughly:
#   ['', '.frozen', '/lib', '/system', '/flash/libs']
# Notably /flash itself is NOT on the path, even though that's where
# boot.py and main.py live. We put the buddy_* peer modules at /flash/
# root (to keep them out of the App List, which scans /flash/apps/),
# and claude_buddy.py lives in /flash/apps/. Prepend both so imports
# resolve regardless of which layout a future install lands on.
for _p in ("/flash", "/flash/apps"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import json
import time

import M5
import machine
from hardware import MatrixKeyboard

import buddy_ble
import buddy_chars
import buddy_chat
import buddy_speak
import buddy_protocol
import buddy_state
import buddy_ui_cp as buddy_ui


# ---- transport selection
#
# buddy_protocol only ever calls send_line/disconnect/forget_bonds, and
# this module only touches pairing_supported/advertised_name/deinit, so
# the two transports are interchangeable behind one factory.
#
#   "ble"    — pair with Claude Desktop's Hardware Buddy (upstream default)
#   "serial" — same wire format over the USB CDC console, which is what
#              lets Claude Code drive the device (see buddy_serial.py)
#
# Flip this and re-push to switch.
_TRANSPORT = "serial"


# ---- chat routing
#
# buddy_protocol.py is upstream and unmodified on flash; its dispatcher
# logs any verb it doesn't know as "unknown cmd". So the chat verbs are
# peeled off in on_line() below, before the protocol layer sees them.
# This substring test is a cheap pre-filter so ordinary traffic doesn't
# pay for a second JSON parse — see buddy_chat.py for the commands.
_CHAT_TAG = b'"chat.'
_SPEAK_TAG = b'"speak.'


def _make_transport(**kw):
    # Imported lazily so a bundle that never selects serial still loads
    # when buddy_serial.py is absent — the module docstring's point
    # about import failures leaving the launcher no way to report them
    # applies to this file's peers too.
    if _TRANSPORT == "serial":
        import buddy_serial

        return buddy_serial.BuddySerial(**kw)
    return buddy_ble.BuddyBLE(**kw)


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
    return {"pct": 100, "mV": 0, "mA": 0, "usb": True}


# ---- key adapter
#
# We translate the raw key from MatrixKeyboard into one of three
# intents: APPROVE / DENY / QUIT / None. That keeps the main loop dumb
# — it doesn't care which key was pressed, just what it means. Picking
# the mapping here (rather than sprinkling magic constants through the
# loop) also makes it trivial to add synonyms later (e.g. space = once).
_INTENT_APPROVE = "approve"
_INTENT_DENY = "deny"
_INTENT_QUIT = "quit"


def _intent_for_key(k):
    """Return an intent string or None for an unrecognized key.

    MatrixKeyboard.get_key() on this UIFlow 2.0 build hands back the
    raw ASCII byte value as an **int** — e.g. 0x59 for 'Y', 0x6E for
    'n', 0x1B for Escape. Enter on this firmware reports as 0x0A
    (LF), not 0x0D (CR) — main.py:_intent_for_key in the launcher
    has the same accommodation. We accept both 0x0A and 0x0D so a
    future build that flips back doesn't silently break Enter here.
    Older builds returned a length-1 string instead; accepted too.
    Ints in the printable range 0x20..0x7E are converted to their
    single-char string and fall through to the string matcher below.

    The previous version of this function treated every int except
    0x0D / 0x1B as unknown, which silently dropped every Y/N/Q press
    — that's the "keyboard buttons don't work" symptom we saw on
    hardware. The Enter-key bug behind it (0x0A reports as not-0x0D)
    is what motivates the explicit (0x0A, 0x0D) check here too.
    """
    if k is None:
        return None
    if isinstance(k, int):
        if k in (0x0A, 0x0D):
            return _INTENT_APPROVE
        if k == 0x1B:
            return _INTENT_QUIT
        if 0x20 <= k <= 0x7E:
            k = chr(k)
        else:
            return None
    if isinstance(k, (bytes, bytearray)) and len(k) == 1:
        k = chr(k[0])
    if not isinstance(k, str) or not k:
        return None
    ch = k.lower()
    if ch in ("y", "\r", "\n"):
        return _INTENT_APPROVE
    if ch == "n":
        return _INTENT_DENY
    if ch in ("q", "\x1b"):
        return _INTENT_QUIT
    return None


def run():
    # Per-step prints so a hard fault during init (NimBLE Guru
    # Meditation, LCD driver crash, etc.) leaves a breadcrumb on the
    # serial console pointing at which step faulted. C-level crashes
    # bypass the launcher's try/except and reboot the chip, so the
    # last print before reboot is the only diagnostic we get.
    print("claude_buddy: run() start")

    # Power WiFi down before bringing up BLE. ESP32 shares a single
    # 2.4 GHz radio between WiFi and BLE, with software coexistence
    # arbitrating between them. The launcher (main.py) connects to
    # the event WiFi at boot, which leaves the radio actively
    # servicing beacons/keepalives by the time we get here.
    # `bluetooth.BLE().active(True)` in `_ensure_stack` cold-starts
    # the NimBLE controller, and in busy RF environments — many
    # nearby BLE peers, lots of WiFi traffic — that init
    # intermittently faults at the C layer and reboots the chip with
    # no Python-catchable error. We saw this consistently with the
    # crash log ending at `pre_active= False`. Buddy is BLE-only, so
    # taking WiFi down for the duration of the app is harmless; the
    # launcher reconnects on the next reboot via main.py.
    try:
        import network
        sta = network.WLAN(network.STA_IF)
        if sta.active():
            try:
                sta.disconnect()
            except OSError:
                pass
            sta.active(False)
        print("claude_buddy: wifi off")
    except Exception as e:
        # Defensive — if `network` isn't importable on this build, or
        # the WLAN object behaves unexpectedly, we'd rather continue
        # and risk the original coexistence crash than fail the app
        # outright. The print is enough to investigate later.
        print("claude_buddy: wifi disable warning:", e)
    # Drain the radio scheduler so WiFi tx queues finish before BLE
    # init takes over the controller. 1000 ms (was 200 ms) is the
    # value that finally got us past intermittent NimBLE
    # active(True) C-faults on a busy show floor — ESP32's WiFi
    # tear-down is more leisurely than its connect path, and the
    # 200 ms we tried first wasn't enough to fully release the
    # radio before BLE asks for it.
    time.sleep_ms(1000)

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

    # Protocol needs a handle on the BLE object (for disconnect /
    # forget_bonds), and BLE needs the on_line callback which needs the
    # protocol. Same indirection trick as the Basic: stash the protocol
    # in a 1-slot dict that the callback reads at event time.
    proto_holder = {"p": None}

    # Set when a chat command changed what should be on screen. Drawing
    # happens in the main loop rather than here for the same reason the
    # state/passkey mailboxes below exist: on the BLE transport this
    # callback runs from micropython.schedule context and could land
    # mid-way through someone else's SPI sequence. The ack is a plain
    # write with no LCD involvement, so that goes out immediately.
    chat_dirty = [False]  # type: list[bool]

    def on_line(raw):
        if _CHAT_TAG in raw and ble is not None:
            ack = chat.handle_raw(raw)
            if ack is not None:
                ble.send_line(json.dumps(ack, separators=(",", ":")).encode("utf-8"))
                chat_dirty[0] = True
                return
        # speak.begin puts the transport into bulk mode and its ack is
        # what releases the host to start writing the payload, so this
        # has to answer synchronously — deferring it to the loop would
        # leave the host waiting with the link already switched over.
        if _SPEAK_TAG in raw and ble is not None and speech is not None:
            ack = speech.handle_raw(raw)
            if ack is not None:
                ble.send_line(json.dumps(ack, separators=(",", ":")).encode("utf-8"))
                return
        p = proto_holder["p"]
        if p is not None:
            p.on_line(raw)

    # BLE callbacks dispatch from micropython.schedule context, which
    # runs between bytecodes on the main thread. That means a
    # callback can land *inside* a Python-level UI routine that's
    # mid-way through a sequence of SPI ops to the LCD, interleaving
    # writes and leaving the panel in an inconsistent state. We avoid
    # that by having callbacks only mutate plain Python state and
    # letting the main loop drain it into UI calls. send_hello stays
    # in the callback because it's BLE-only — no LCD bus contention.
    # PEP 484 type comments rather than annotations: MicroPython has no
    # `typing`, and these one-slot mailboxes would otherwise be inferred
    # as list[None] and reject every store.
    pending_state = [None]  # type: list[str | None]
    pending_passkey = [None]  # type: list[int | None]

    def on_passkey(pk):
        pending_passkey[0] = pk

    # Pre-bind so on_state_change's closure can resolve `ble` even if
    # the IRQ fires during BuddyBLE.__init__ (a central that connects
    # mid-init can deliver _IRQ_CENTRAL_CONNECT before the
    # `ble = BuddyBLE(...)` assignment below completes). Without this
    # pre-bind, on_state_change raises NameError in IRQ context and
    # the link is silently lost. The `is None` guard means the very
    # first event during init won't get the pairing-aware remap, but
    # any subsequent event will — and the run loop stays alive.
    ble = None
    # Needs the transport, which does not exist yet. Pre-bound for the
    # same reason `ble` is: on_line resolves it at call time, and a
    # callback that fires during transport init would otherwise raise
    # NameError inside the IRQ.
    speech = None

    def on_state_change(s):
        # The stripped UIFlow 2.0 BLE build doesn't fire
        # _IRQ_ENCRYPTION_UPDATE, so "connected" is terminal. Remap
        # it to "encrypted" so the UI advances past the PAIR... badge
        # and the protocol starts emitting its hello.
        effective = s
        if s == "connected" and ble is not None and not ble.pairing_supported:
            effective = "encrypted"
        print("claude_buddy: state", s, "->", effective)
        pending_state[0] = effective
        if effective == "encrypted":
            p = proto_holder["p"]
            if p is not None:
                p.send_hello()

    # Run a full GC pass before NimBLE init. The controller
    # allocates several large chunks during active(True) — bonding
    # store, advertising buffers, host/controller queues — and a
    # fragmented MicroPython heap at this point has been observed
    # to push allocation onto a path that C-faults instead of
    # raising MemoryError. Cheap insurance to call gc.collect() here
    # since we have no other allocation pressure between launcher
    # exit and BLE init.
    import gc
    gc.collect()
    print("claude_buddy: gc done, free=", gc.mem_free())
    print("claude_buddy: constructing transport:", _TRANSPORT)
    ble = _make_transport(
        on_line=on_line,
        on_passkey=on_passkey,
        on_state=on_state_change,
    )
    print("claude_buddy: transport ready")

    # Only the serial transport has the bulk mode this needs; a BLE
    # build simply has no speech path.
    if hasattr(ble, "bulk_begin"):
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
    print("Claude Buddy up as", ble.advertised_name)

    # Keyboard: debounce 400 ms before polling so the key used to pick
    # this app from App List doesn't count as an intent. Same pattern
    # hello_cardputer.py uses — confirmed by testing there that
    # MatrixKeyboard.get_key() is reliable inside an app context as
    # long as we tick() before reading.
    kb = MatrixKeyboard()
    time.sleep_ms(400)

    last_footer_ms = time.ticks_ms()
    last_toast_ms = 0
    footer_interval = 3000
    toast_dwell_ms = 1500

    # BLE pushes inbound data from an IRQ; the serial transport has no
    # callback and needs this loop to drain stdin for it. Resolve once —
    # BuddyBLE has no poll() and we don't want a getattr every 40 ms.
    pump = getattr(ble, "poll", None)

    try:
        while True:
            if pump is not None:
                pump()

            # Right behind the transport drain and ahead of everything
            # that paints: one 2 KiB block is 64 ms of audio and takes
            # ~11 ms to read, so the 40 ms tick stays ahead of playback
            # only if nothing slow gets in front of it.
            if speech is not None:
                speech.pump()

            # Drain BLE-callback-deferred UI work in main-loop context
            # so LCD writes don't interleave with the periodic footer
            # paint or the prompt rendering kicked off by protocol
            # events. set_connection in particular repaints the whole
            # header strip, which is several SPI transactions long.
            new_state = pending_state[0]
            if new_state is not None:
                pending_state[0] = None
                ui.set_connection(new_state)
                if new_state == "encrypted":
                    ui.clear_passkey()
                if chat.active:
                    # set_connection repaints the main panel, which is
                    # the chat's while a transcript is up.
                    chat.render()
            new_pk = pending_passkey[0]
            if new_pk is not None:
                pending_passkey[0] = None
                ui.show_passkey(new_pk)

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

            kb.tick()
            k = kb.get_key()
            intent = _intent_for_key(k)

            # An active unpair confirmation outranks any permission
            # prompt: pressing Y here means "yes, wipe me", not "yes,
            # approve the pending tool call". The unpair_pending()
            # check is also where the protocol layer rolls over the
            # 30s timeout, so we want it called every loop iteration
            # regardless of what key (if any) was pressed.
            unpair_active = proto.unpair_pending()

            if intent == _INTENT_APPROVE:
                if unpair_active:
                    proto.confirm_unpair()
                elif not proto.send_permission("once"):
                    ui.flash_toast("Y: no prompt", buddy_ui.GRAY_DIM)
                    ui.update_footer(state.stats(), _stub_battery())
                last_toast_ms = time.ticks_ms()
            elif intent == _INTENT_DENY:
                if unpair_active:
                    proto.cancel_unpair()
                elif not proto.send_permission("deny"):
                    ui.flash_toast("N: no prompt", buddy_ui.GRAY_DIM)
                last_toast_ms = time.ticks_ms()
            elif intent == _INTENT_QUIT:
                # Break out so the `finally` block tears BLE down
                # cleanly before we reboot back to the launcher. If
                # an unpair is pending, leaving without confirming
                # cancels it on the device side; the host already has
                # an "ok:false,pending:true" ack and a subsequent
                # disconnect will tell it the request didn't go
                # through.
                return

            now = time.ticks_ms()
            if time.ticks_diff(now, last_footer_ms) >= footer_interval:
                state.tick_nap()
                # The stats footer paints y=96..110, which is inside the
                # chat's region. Skipping it is what keeps a transcript
                # from being punched through every 3 seconds. It is also
                # several SPI transactions long, and during playback the
                # tick has no room to spare — an underrun is audible,
                # a three-second-stale battery reading is not.
                if not chat.active and (speech is None or not speech.active):
                    ui.update_footer(state.stats(), _stub_battery())
                last_footer_ms = now
            if last_toast_ms and time.ticks_diff(now, last_toast_ms) >= toast_dwell_ms:
                ui.restore_button_hints()
                last_toast_ms = 0

            # 40 ms matches buddy_app.py — fast enough for responsive
            # key handling, slow enough that the BLE IRQ gets plenty
            # of room. MatrixKeyboard handles debounce internally on
            # tick(), so no additional delay is needed for the input
            # path specifically.
            time.sleep_ms(40)
    finally:
        # Mirror buddy_app.py's teardown ordering: BLE first so a late
        # async disconnect event can't repaint Buddy chrome on top of
        # the launcher (cf. the comment in BuddyBLE.deinit), then wipe
        # the screen to black, then hand control back to UIFlow.
        # Before the transport goes: stop() drops the transfer through
        # it, and it is the only thing that silences a speaker still
        # working through a second of queued audio.
        if speech is not None:
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
        # UIFlow has no launcher-return API; machine.reset() is the
        # only way back to App List. Same pattern hello_cardputer.py
        # uses. Brief pause so any trailing BLE log doesn't get
        # truncated mid-line on the USB console.
        time.sleep_ms(200)
        machine.reset()


# UIFlow 2.4.x's App List has been observed to invoke apps both as
# __main__ (file run directly) and via import (when picked through
# the menu vs. the file system, depending on version). The previous
# if/else with both arms calling run() was just dispatching to
# itself; collapse it so the empirical behavior is the documented
# behavior. The trade-off is that anyone who imports this module
# from CPython for inspection will trigger a BLE init — but the
# imports above (M5, hardware, bluetooth) already only resolve
# on-device, so that path isn't a real use case.
run()
