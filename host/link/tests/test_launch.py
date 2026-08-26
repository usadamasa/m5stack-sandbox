"""`launch_app` の側。REPL からアプリを import して、ポートを手渡すところ。

リンクを 1 本も作らずに済む 3 つをここに置く。起動した先を `ResidentLink` が
引き取るところ (`start_app`) は test_resident_link.py の担当。
"""

import unittest

import buddy_link
from fake_repl import FakeRepl


class LaunchAppTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repl = FakeRepl()
        self.opened: list[str] = []

        def connect_repl(port: str, baud: int, timeout: float = 180.0, **_kw: object) -> FakeRepl:
            self.opened.append(port)
            return self.repl

        real = buddy_link.connect_repl
        buddy_link.connect_repl = connect_repl
        self.addCleanup(setattr, buddy_link, "connect_repl", real)

    def test_imports_the_app_without_waiting_for_it_to_end(self) -> None:
        # `exec` はコマンドが返るまでブロックするが、アプリの仕事は返らない
        # ことそのものなので使えない。
        buddy_link.launch_app("/dev/fake")
        self.assertEqual(self.repl.launched, [buddy_link.LAUNCH_SOURCE])
        self.assertEqual(self.repl.execs, [buddy_link.LAUNCH_SOURCE])

    def test_hands_back_a_port_the_reader_can_poll(self) -> None:
        # mpremote はブロッキングで、バイト間タイムアウト 1 秒でポートを開く。
        # in_waiting を回す reader はそのどちらでも止まってしまう。
        port = buddy_link.launch_app("/dev/fake", read_timeout=0.05)
        self.assertIs(port, self.repl.serial)
        self.assertEqual(self.repl.serial.timeout, 0.05)
        self.assertIsNone(self.repl.serial.inter_byte_timeout)

    def test_writes_nothing_after_the_launch(self) -> None:
        # paste mode で起動していた頃は末尾に改行を送る必要があった。Ctrl-D は
        # 改行を運ばないので、その余りの 1 バイトが次の protocol フレームの頭に
        # 付いてデバイスに捨てられ、リクエストがきっかり 1 本タイムアウトして
        # いた。raw-paste は自分の終端子を自分で ack するので後始末は要らず、
        # ここで何か書けばアプリの入力へ流れ込む。
        buddy_link.launch_app("/dev/fake")
        self.assertEqual(bytes(self.repl.serial.written), b"")


if __name__ == "__main__":
    unittest.main()
