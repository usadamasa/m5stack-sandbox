"""A stand-in for mpremote's SerialTransport, for tests with no board.

Implements the `device_repl.Repl` protocol against dictionaries. The
point is not to emulate MicroPython — it is to let the host-side logic
be exercised: which statements get sent, in what order, and what the
caller does with the answers.

`answers` maps an expression to what `eval` should return for it. Wrap a
value in `Sequenced` to have it consumed one element per call, which is
how a poll loop is given something that changes underneath it — the
wrapper is explicit because plenty of real answers *are* lists.
"""

from __future__ import annotations

import errno
from collections.abc import Callable
from typing import Any


class Sequenced:
    """An answer that changes between calls. The last value repeats.

    Canned device output, so the values really are arbitrary — that is
    what makes them stand in for what a device returned.
    """

    def __init__(self, *values: Any) -> None:  # noqa: ANN401
        if not values:
            raise ValueError("Sequenced needs at least one value")
        self._values = list(values)

    def next(self) -> Any:  # noqa: ANN401
        return self._values.pop(0) if len(self._values) > 1 else self._values[0]


class FakePort:
    """The open serial port a transport hands over after a launch.

    Enough of pyserial to satisfy both `run_and_release`, which
    reconfigures the timeouts, and the links, which read and write.
    """

    def __init__(self) -> None:
        self.timeout: float | None = None
        self.inter_byte_timeout: float | None = 1.0
        self.closed = False
        self._inbound = bytearray()
        self.written = bytearray()

    @property
    def in_waiting(self) -> int:
        return len(self._inbound)

    def feed(self, data: bytes) -> None:
        self._inbound.extend(data)

    def read(self, size: int = 1, /) -> bytes:
        data = bytes(self._inbound[:size])
        del self._inbound[:size]
        return data

    def write(self, data: bytes, /) -> int:
        self.written += data
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeStat:
    """Just the field `push_file` reads off `fs_stat`."""

    def __init__(self, size: int) -> None:
        self.st_size = size
        self.st_mode = 0o100644


class FakeRepl:
    def __init__(
        self,
        answers: dict[str, Any] | None = None,
        *,
        dirs: set[str] | None = None,
        on_exec: Callable[[str], None] | None = None,
    ) -> None:
        self.answers: dict[str, Any] = dict(answers or {})
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = set(dirs or ())
        self.execs: list[str] = []
        self.evals: list[str] = []
        self.made_dirs: list[str] = []
        self.in_raw_repl = False
        self.closed = False
        self.soft_resets = 0
        # Set to report a size other than what was written, which is
        # what a truncated transfer looks like from here.
        self.report_size: int | None = None
        self._on_exec = on_exec
        # The port a launch hands over, as mpremote's transport exposes
        # it.
        self.serial = FakePort()
        # Whatever was started with exec_raw_no_follow, which is the
        # distinction that matters: `exec` would wait for an app that
        # never ends.
        self.launched: list[str] = []

    # ----- device_repl.Repl

    def enter_raw_repl(self, soft_reset: bool = True, timeout_overall: int = 10) -> None:
        if soft_reset:
            self.soft_resets += 1
        self.in_raw_repl = True

    def exit_raw_repl(self) -> None:
        self.in_raw_repl = False

    def exec(self, command: str, data_consumer: Callable[[bytes], None] | None = None) -> bytes:
        self.execs.append(command)
        if self._on_exec is not None:
            self._on_exec(command)
        return b""

    def exec_raw_no_follow(self, command: str) -> None:
        self.execs.append(command)
        self.launched.append(command)

    def eval(self, expression: str, parse: bool = True) -> Any:  # noqa: ANN401
        # A dictionary lookup, not an evaluation: the expression is a
        # key. Nothing here compiles or runs the string, and on real
        # hardware mpremote runs it on the device and literal_evals the
        # reply.
        self.evals.append(expression)
        if expression == "1":
            # device_repl's liveness check.
            return 1
        if expression not in self.answers:
            raise AssertionError(f"FakeRepl has no answer for {expression!r}")
        value = self.answers[expression]
        return value.next() if isinstance(value, Sequenced) else value

    def fs_writefile(
        self,
        dest: str,
        data: bytes,
        chunk_size: int = 256,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        self.files[dest] = data
        if progress_callback is not None:
            progress_callback(len(data), len(data))

    def fs_stat(self, src: str) -> FakeStat:
        if src in self.dirs:
            return FakeStat(0)
        if src not in self.files:
            raise OSError(2, "No such file or directory", src)
        size = self.report_size if self.report_size is not None else len(self.files[src])
        return FakeStat(size)

    def fs_isdir(self, src: str) -> bool:
        return src in self.dirs

    def fs_mkdir(self, path: str) -> None:
        if path in self.dirs:
            raise OSError(errno.EEXIST, "File exists", path)
        self.dirs.add(path)
        self.made_dirs.append(path)

    def close(self) -> None:
        self.closed = True

    # ----- helpers for assertions

    @property
    def source(self) -> str:
        """Everything that was sent to the device, concatenated."""
        return "\n".join(self.execs)
