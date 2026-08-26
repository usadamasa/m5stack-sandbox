"""自分のプロセスで走らせる口のテスト。実機は開かない。

`main` そのものはシリアルポートを開くので触らない。ここで見るのは、
その手前にある 2 つ — コマンドラインが設定に落ちること、そして stderr へ
出す担当が 1 つに絞られていること。
"""

import logging
import unittest
from tempfile import TemporaryDirectory
from unittest import mock

import chatter_cli


class TuningTests(unittest.TestCase):
    def setUp(self) -> None:
        # 走らせている人の `config.toml` と環境を持ち込まない。ここで見たい
        # のはコマンドラインが設定に落ちるかどうかだけ。
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict("os.environ", {"HOME": self._tmp.name}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_nothing_on_the_command_line_leaves_the_defaults_alone(self) -> None:
        cfg = chatter_cli._tuned(chatter_cli._parse([]))
        self.assertEqual(cfg.gap_min, 40.0)
        self.assertEqual(cfg.voice_every, 1)

    def test_what_was_asked_for_is_what_is_set(self) -> None:
        cfg = chatter_cli._tuned(chatter_cli._parse(["--gap-min", "5", "--voice-every", "3"]))
        self.assertEqual(cfg.gap_min, 5.0)
        self.assertEqual(cfg.voice_every, 3)

    def test_a_voice_every_of_zero_would_be_a_division_by_zero(self) -> None:
        cfg = chatter_cli._tuned(chatter_cli._parse(["--voice-every", "0"]))
        self.assertEqual(cfg.voice_every, 1)


class ReportingTests(unittest.TestCase):
    def test_the_service_log_does_not_double_the_runner_report(self) -> None:
        # daemon には誰も見ていない log しかないので service は自分で書く。
        # こちらには stderr に立つ人が居て、その人向けの表示は `report`。
        # 両方出すと 1 回の発話が 2 行になる。
        seen: list[logging.LogRecord] = []

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                seen.append(record)

        log = logging.getLogger("buddy.chatter")
        handlers, propagate = log.handlers, log.propagate
        self.addCleanup(setattr, log, "handlers", handlers)
        self.addCleanup(setattr, log, "propagate", propagate)
        root = logging.getLogger()
        root.addHandler(Capture())
        self.addCleanup(root.removeHandler, root.handlers[-1])

        chatter_cli._silence_service_log()
        log.warning("not said: something")
        self.assertEqual(seen, [], "the runner's own report is the only line")


if __name__ == "__main__":
    unittest.main()
