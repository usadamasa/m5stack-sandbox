"""run 全体の予算と、何を載せるかの表。

`deploy_spec` にあるものはほとんどが表と定数で、振る舞いを持つのは
`Deadline` だけ。時計を差し込んで、残りがあるうちは段を通し、尽きたら
どの段で尽きたかを名前で言うことを見る。

表のほうは 1 つだけ見る。`OVERLAY` からモジュールが漏れても host 側では
何も起きず、実機で ImportError になって初めて分かるので、`device/buddy/`
の現物と突き合わせる。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from deploy_spec import OVERLAY, Deadline, DeployTimeout

# host/tools/tests/test_deploy_spec.py から 3 つ上がリポジトリのルート。
# `deploy_spec.DEVICE_ROOT` を借りないのは、それを間違えたときにこの
# テストまで一緒に間違えるため。
BUDDY_ROOT = Path(__file__).resolve().parents[3] / "device" / "buddy"


class DeadlineTest(unittest.TestCase):
    def test_a_budget_with_time_left_lets_the_step_run(self) -> None:
        now = [0.0]
        deadline = Deadline(10.0, clock=lambda: now[0])
        now[0] = 9.0
        deadline.check("pushing buddy_tts.mpy")
        self.assertAlmostEqual(deadline.remaining(), 1.0)

    def test_an_exhausted_budget_names_the_step(self) -> None:
        now = [0.0]
        deadline = Deadline(10.0, clock=lambda: now[0])
        now[0] = 10.0
        with self.assertRaises(DeployTimeout) as caught:
            deadline.check("pushing buddy_tts.mpy")
        self.assertIn("buddy_tts.mpy", str(caught.exception))


class OverlayTest(unittest.TestCase):
    def test_every_module_under_device_buddy_is_pushed(self) -> None:
        found = sorted("buddy/" + p.name for p in BUDDY_ROOT.glob("*.py"))
        # 何にもマッチしない glob だと以下が素通りする。
        self.assertTrue(found, f"no modules under {BUDDY_ROOT}")
        missing = [name for name in found if name not in OVERLAY]
        self.assertEqual(
            missing,
            [],
            f"{missing} が OVERLAY に無い。載せ忘れは host 側の何にも捕まらず、"
            "実機の ImportError でしか分からない。",
        )


if __name__ == "__main__":
    unittest.main()
