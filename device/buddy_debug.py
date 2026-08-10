"""On-device inspection, loaded only while someone is looking.

The app owns the USB console for the length of its run, so the REPL that
would normally answer "what is the heap doing right now" is not
available while the interesting state exists. This module is the
substitute: the host sends `dbg.*` frames down the transport that is
already up, and the answers come back as ordinary acks.

### Why this costs nothing when unused

`apps/claude_buddy.py` does not import this module. It imports it the
first time a `dbg.` frame arrives and drops it — `del sys.modules` plus
`gc.collect()` — on `dbg.off`. Until then the whole cost is one
substring test per inbound line, the same pre-filter `chat.` and
`speak.` already pay for.

That matters because the heap here is small enough that the bundle ships
as `.mpy` specifically to keep import-time parse trees out of it (see
`host/tools/src/buddy_deploy.py`). A debug module that sat resident
would be taking the space it exists to measure.

### Why the big output is not in the ack

`buddy_serial` frames protocol lines with a sentinel and the host passes
every other line through as log output. So `print()` already reaches the
host. Anything bulky — `micropython.mem_info(1)`'s heap map, a traceback
— goes out that way instead of being marshalled into a JSON string that
has to be built in the heap being reported on.

### eval / exec

`dbg.eval` and `dbg.exec` compile at runtime, which is exactly the parse
tree the `.mpy` decision was about, so they are the escape hatch and not
the main road: the fixed verbs above cover routine checks without
compiling anything, and the source is capped at `_MAX_SOURCE` with a
`gc.collect()` on each side.

They are also arbitrary code execution, deliberately. The channel is a
USB cable to a device whose normal state is an open REPL — this hands
back what the app took away, and reaching it already requires physical
possession of the board. Nothing here is a barrier to anyone who could
not more easily press BtnRST.
"""

import gc
import json
import sys

try:
    import micropython
except ImportError:  # pragma: no cover - host-side import for inspection
    micropython = None

try:
    import esp32
except ImportError:  # pragma: no cover - host-side import for inspection
    esp32 = None


# Longest source we will compile. Long enough for `speech._sock` or
# `[k for k in dir(chat)]`, short enough that the parse tree it builds is
# noise against a 60 KB heap.
_MAX_SOURCE = 192

# Longest repr we will send back. A truncated answer beats an ack that
# cost more to build than the thing it describes.
_MAX_REPR = 240

# (bound name, attribute) pairs `dbg.state` reads. A fixed table rather
# than a walk over dir(): every entry is a cheap property, the output
# stays small enough to read at a glance, and nothing here can trip over
# an attribute whose getter has side effects.
_PROBES = (
    ("ble", "connected"),
    ("ble", "advertised_name"),
    ("chat", "active"),
    ("speech", "active"),
)

# CPython has no sys.print_exception. Resolved once so the eval path does
# not pay a getattr per failure.
_print_exception = getattr(sys, "print_exception", None)

# The namespace `dbg.eval` runs against and `dbg.state` probes. Populated
# by bind() from the app's live objects, so an expression can reach the
# transport, the chat panel and the speech player by name.
_ns = {}


def bind(ns) -> None:
    """Hand over the app's live objects. Called once after import."""
    global _ns
    _ns = ns


def handle_raw(raw):
    """Parse one wire line and dispatch it. None if it is not ours."""
    try:
        msg = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
    except (ValueError, UnicodeError):
        return None
    if not isinstance(msg, dict):
        return None
    return handle(msg)


def handle(msg):
    """Dispatch one parsed command. None if the cmd is not ours."""
    cmd = msg.get("cmd")
    if cmd == "dbg.mem":
        ack = _mem()
    elif cmd == "dbg.frag":
        ack = _frag()
    elif cmd == "dbg.gc":
        ack = _gc()
    elif cmd == "dbg.state":
        ack = _state()
    elif cmd == "dbg.eval":
        ack = _run(msg.get("src", ""), False)
    elif cmd == "dbg.exec":
        ack = _run(msg.get("src", ""), True)
    elif cmd == "dbg.off":
        ack = _off()
    else:
        return None
    ack["ack"] = cmd
    # Echoed so a host that pipelines several frames can match acks to
    # sends without relying on ordering, same as chat.
    if "id" in msg:
        ack["id"] = msg["id"]
    return ack


# ----- verbs


def _mem() -> dict:
    """Both heaps. They are separate allocators and fail independently.

    `gc.mem_free()` is the MicroPython heap — Python objects. The
    ESP-IDF heap underneath it is where WiFi buffers and sockets come
    from, and the one that has actually been short on this board: a
    `speak.say` that cannot get a socket looks like plenty of free
    memory from Python's side.
    """
    gc.collect()
    out = {"ok": True, "free": gc.mem_free(), "alloc": gc.mem_alloc()}
    if esp32 is not None:
        # Each region is (total, free, largest_free_block, min_free_ever).
        regions = esp32.idf_heap_info(esp32.HEAP_DATA)
        if regions:
            out["idf_free"] = sum(r[1] for r in regions)
            # Largest contiguous block, not the sum: a socket needs one
            # run, and this is the number that says whether it can have
            # one.
            out["idf_largest"] = max(r[2] for r in regions)
    return out


def _frag() -> dict:
    """Dump the heap map. Output goes to the log channel, not the ack."""
    if micropython is None:
        return {"ok": False, "err": "no micropython module on this build"}
    gc.collect()
    micropython.mem_info(1)
    return {"ok": True, "to": "log"}


def _gc() -> dict:
    before = gc.mem_free()
    gc.collect()
    after = gc.mem_free()
    return {"ok": True, "before": before, "after": after, "freed": after - before}


def _off() -> dict:
    # We cannot drop ourselves: the caller holds the other reference, so
    # this flag is the handshake asking it to.
    return {"ok": True, "unload": True}


def _state() -> dict:
    out = {"ok": True}  # type: dict
    for name, attr in _PROBES:
        obj = _ns.get(name)
        if obj is None:
            continue
        try:
            out[name + "." + attr] = getattr(obj, attr)
        except Exception as e:
            out[name + "." + attr] = "err: " + str(e)
    return out


def _run(src, as_statement) -> dict:
    if not isinstance(src, str) or not src:
        return {"ok": False, "err": "no src"}
    if len(src) > _MAX_SOURCE:
        return {"ok": False, "err": "src too long: " + str(len(src)) + " > " + str(_MAX_SOURCE)}
    # Bracketing the compile with collections keeps the parse tree from
    # landing in whatever hole the previous one left.
    gc.collect()
    try:
        if as_statement:
            # Arbitrary code by design — see the module docstring.
            exec(src, _ns)
            out = {"ok": True}
        else:
            out = {"ok": True, "repr": _clip(repr(eval(src, _ns)))}
    except Exception as e:
        # The full traceback is worth more than the one line we can fit
        # in the ack, and print() reaches the host for free.
        if _print_exception is not None:
            _print_exception(e)
        # A SyntaxError from the compile step arrives here too — it is an
        # Exception subclass on both interpreters — so a malformed
        # expression is reported rather than thrown at the main loop.
        out = {"ok": False, "err": type(e).__name__ + ": " + str(e)}
    gc.collect()
    return out


def _clip(text) -> str:
    if len(text) > _MAX_REPR:
        return text[:_MAX_REPR] + "..."
    return text
