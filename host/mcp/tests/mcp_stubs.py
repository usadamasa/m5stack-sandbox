"""MCP server のテストが共有するデバイスの stub。

`mcp_state` がリンクを 1 本だけ持つので、それを差し替える仕掛けも 1 つで足りる。
tool 側・状態側・起動側のテストがどれもここを使う。

差し替え先は `mcp_state.ResidentLink` と `mcp_state.link` であって
`buddy_mcp` の属性ではない。tool は `mcp_state` 経由でしか状態を読まないので、
`buddy_mcp` の側に代入しても素通りする。
"""

import unittest
from typing import Any, ClassVar

import mcp_state
from buddy_wire import Message


class StubLink:
    """Stands in for ResidentLink; records what the server did to it."""

    instances: ClassVar[list["StubLink"]] = []

    def __init__(self, port: str) -> None:
        self.port = port
        self.connected = False
        self.dropped = False
        self.requests: list[tuple[Message, str]] = []
        self.queued_messages: list[Message] = []
        self.queued_logs: list[bytes] = []
        self.started = False
        self.lock_held: list[bool] = []
        self.interrupts = 0
        # Merged into every ack, so a test can make the device claim it
        # just entered debug mode.
        self.ack_extra: Message = {}
        StubLink.instances.append(self)

    def interrupt(self) -> None:
        self.interrupts += 1
        self.lock_held.append(mcp_state.device_lock.locked())

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def request(self, obj: Message, expect: str, timeout: float = 5.0) -> Message:
        self.requests.append((obj, expect))
        # Recorded at the moment the device is being talked to, which is
        # the only place the answer is meaningful.
        self.lock_held.append(mcp_state.device_lock.locked())
        return {"ack": expect, "ok": True, **self.ack_extra}

    def events(self) -> tuple[list[Message], list[bytes]]:
        msgs, logs = self.queued_messages, self.queued_logs
        self.queued_messages, self.queued_logs = [], []
        return msgs, logs

    def start_app(self, settle: float = 8.0, wait: float = 15.0) -> None:
        self.started = True
        self.start_wait = wait


class McpTestCase(unittest.TestCase):
    def setUp(self) -> None:
        StubLink.instances = []
        real_cls: Any = mcp_state.ResidentLink
        mcp_state.ResidentLink = StubLink  # pyright: ignore[reportAttributeAccessIssue]
        self.addCleanup(setattr, mcp_state, "ResidentLink", real_cls)
        # The module holds one link for the life of the server process.
        mcp_state.link = None
        self.addCleanup(setattr, mcp_state, "link", None)
