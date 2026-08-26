"""デプロイのテストが共有する、答えを返すポートと台。

`Bench` は 1 台のデバイスを仕込んだ `FakeRepl` で、flash を書き換える側
(deploy_device) のテストと、run 全体 (buddy_deploy) のテストがどちらもこれを使う。

`connect_repl` の差し替え先が `buddy_deploy` なのは、それを呼ぶのが
`buddy_deploy.main` だから。名前の出どころは `device_repl` だが、
呼ぶ側が見ているグローバルを差し替えないと素通りして本物のポートを開きに行く。
"""

from __future__ import annotations

import json
import unittest
import unittest.mock
from pathlib import Path
from tempfile import TemporaryDirectory

from buddy_deploy import main
from buddy_wire import SENTINEL, Message, encode
from deploy_spec import DEST_ROOT, LAUNCHER, OVERLAY, REMOVE, UPSTREAM
from fake_repl import FakePort, FakeRepl

# デバイスが返す答え。答えている cmd をキーにする。
Replies = dict[str, list[Message]]

# 正常なデバイス。`speak.say` には 2 回答える — 再生が始まったときと、
# 最後のブロックを鳴らし終えたとき。確認が実際に待つのは後者。
_HAPPY: Replies = {
    "chat.say": [{"ack": "chat.say", "ok": True}],
    "speak.say": [
        {"ack": "speak.say", "ok": True, "bytes": 81920, "rate": 16000},
        {"ack": "speak.end", "ok": True, "blocks": 40, "stalls": 0},
    ],
}


class TalkingPort(FakePort):
    """答えを持たされたポートではなく、答えを返すポート。

    ack を先に積んでおく手は効かない。その理由は覚えておく価値がある —
    起動の後には settle の窓があって、アプリが立ち上がりざまに言うものを
    読み捨てる。すでにバッファに座っているものはそこで飲まれて消える。
    """

    def __init__(self, replies: Replies | None = None) -> None:
        super().__init__()
        self.replies = _HAPPY if replies is None else replies

    def write(self, data: bytes, /) -> int:
        written = super().write(data)
        for frame in _frames(data):
            for ack in self.replies.get(frame["cmd"], []):
                self.feed(encode(ack))
        return written

    @property
    def frames(self) -> list[Message]:
        """ホストが送ったものを全部、パースして返す。`encode` は非 ASCII を
        エスケープするので、生のバイト列で照合すると本文を毎回取り落とす。"""
        return _frames(self.written)


def _frames(data: bytes | bytearray) -> list[Message]:
    return [
        json.loads(line[len(SENTINEL) :])
        for line in bytes(data).split(b"\n")
        if line.startswith(SENTINEL)
    ]


class Bench:
    """デバイスを仕込んだ FakeRepl と、その周りのディレクトリ。"""

    def __init__(self, cleanup: unittest.TestCase) -> None:
        tmp = TemporaryDirectory()
        cleanup.addCleanup(tmp.cleanup)
        self.case = cleanup
        root = Path(tmp.name)
        self.vendor = root / "vendor"
        self.build = root / "build"
        self.src = root / "device"
        self.src.mkdir()
        (self.src / LAUNCHER).write_text("print('our launcher')\n")
        self.repl = FakeRepl(dirs={DEST_ROOT, f"{DEST_ROOT}/apps"})
        # トランスポートから読み戻さず、自分の参照として持つ。
        # `FakeRepl.serial` はただのポートで、答えを返すのはこちら。
        self.port = TalkingPort()

    def put(self, path: str, text: str) -> None:
        self.repl.files[f"{DEST_ROOT}/{path}"] = text.encode()

    def seed_unconverted(self) -> None:
        """最初のデプロイ前の flash。まだ全部ソース。"""
        self.repl.answers["sys.implementation._mpy"] = 0x2806
        self.repl.serial = self.port
        for name in UPSTREAM:
            self.put(f"{name}.py", f"print('{name}')\n")
        for rel in OVERLAY:
            self.put(rel, "print('the source this replaces')\n")
        for rel in REMOVE:
            self.put(rel, f"print('{rel}')\n")
        self.put(LAUNCHER, "print('upstream menu + NimBLE')\n")

    def deploy(self, *extra: str) -> int:
        """このデバイスに main() を 1 回通す。その終了コードを返す。"""
        patch = unittest.mock.patch("buddy_deploy.connect_repl", return_value=self.repl)
        patch.start()
        self.case.addCleanup(patch.stop)
        return main(
            [
                "--port",
                "/dev/null",
                "--build",
                str(self.build),
                "--vendor",
                str(self.vendor),
                *extra,
            ]
        )
