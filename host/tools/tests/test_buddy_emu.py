"""エミュレータの上で、ホストの link からアプリを操作して画面を見る。

実機の代わりに `buddy_emu.Emulator` が `buddy.app.run()` を CPython で回し、
TCP で listen する。こちらは実機と同じ `BuddyLink("tcp://...")` で繋ぐので、
verb も framing も本番の経路そのもの。

ack は `ble.poll()` の中で返り、描画はその後の `_serve_ui` で起きる。ack が
返った直後に画面を見ると空振りするので、描かれた文字列を待ってから見る。
"""

import tempfile
import unittest
from pathlib import Path

from buddy_emu import Emulator
from buddy_link import BuddyLink


class EmulatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.emu = Emulator(port=0).start()
        self.addCleanup(self.emu.stop)
        self.link = BuddyLink(f"tcp://127.0.0.1:{self.emu.port}").open()
        self.addCleanup(self.link.close)

    def test_status_answers_like_the_board(self) -> None:
        ack = self.link.request({"cmd": "status"}, "status")
        self.assertEqual(ack["ack"], "status")
        self.assertTrue(ack["ok"])

    def test_chat_say_lands_on_the_panel(self) -> None:
        ack = self.link.request({"cmd": "chat.say", "role": "claude", "text": "hello"}, "chat.say")
        self.assertTrue(ack["ok"])
        self.assertTrue(ack["active"])
        self.emu.wait_drawn("hello")
        # transcript の領域 (y=0..110) に何か描かれている。
        self.assertIsNotNone(self.emu.screen.crop((0, 0, 240, 110)).getbbox())

    def test_chat_clear_hands_the_panel_back(self) -> None:
        _ = self.link.request({"cmd": "chat.say", "role": "claude", "text": "hello"}, "chat.say")
        self.emu.wait_drawn("hello")
        ack = self.link.request({"cmd": "chat.clear"}, "chat.clear")
        self.assertFalse(ack["active"])
        # dashboard の chrome が描き直される。header の "Claude Buddy" が戻る。
        self.emu.wait_drawn("Claude Buddy")

    def test_japanese_wraps_on_the_wide_face(self) -> None:
        text = "ずんだもんなのだ。" * 3
        ack = self.link.request({"cmd": "chat.say", "role": "claude", "text": text}, "chat.say")
        self.assertTrue(ack["cjk"])
        self.assertGreater(ack["wrapped"], 1)

    def test_speak_without_an_engine_fails_in_the_ack_not_the_loop(self) -> None:
        ack = self.link.request(
            {"cmd": "speak.say", "text": "hi", "url": "http://127.0.0.1:1/"}, "speak.say"
        )
        self.assertFalse(ack["ok"])
        # ループは生きている。
        self.assertTrue(self.link.request({"cmd": "status"}, "status")["ok"])

    def test_screenshot_is_a_png(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "screen.png"
            self.emu.save(out)
            self.assertEqual(out.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")


if __name__ == "__main__":
    unittest.main()
