"""run 全体の予算。

`deploy_spec` にあるものはほとんどが表と定数で、振る舞いを持つのは
`Deadline` だけ。時計を差し込んで、残りがあるうちは段を通し、尽きたら
どの段で尽きたかを名前で言うことを見る。
"""

from __future__ import annotations

import unittest

from deploy_spec import Deadline, DeployTimeout


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


if __name__ == "__main__":
    unittest.main()
