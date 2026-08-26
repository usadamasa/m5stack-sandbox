"""chatter を操る tool のテスト。

見ているのは chatter 本体ではなく、start / stop / status が
`mcp_state.chatter` をどう組み替えるか。socket は一時ディレクトリへ逃がして
あるので、このマシンで本物の chatter が走っていても巻き添えにしない。
"""

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import mcp_chatter_tools
import mcp_state


class ChatterToolTest(unittest.TestCase):
    def setUp(self) -> None:
        # A temp socket, so a chatter that happens to be running for real
        # on this machine does not have its own unlinked out from under
        # it by `start()`.
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.socket_path = Path(tmp.name) / "chatter.sock"
        env = mock.patch.dict(os.environ, {"BUDDY_CHATTER_SOCKET": str(self.socket_path)})
        env.start()
        self.addCleanup(env.stop)
        mcp_state.chatter = None
        self.addCleanup(setattr, mcp_state, "chatter", None)
        self.addCleanup(lambda: mcp_chatter_tools.buddy_chatter_stop())

    def test_status_before_start_reports_it_is_not_running(self) -> None:
        status = mcp_chatter_tools.buddy_chatter_status()
        self.assertFalse(status["running"])
        self.assertEqual(status["spoken"], 0)

    def test_start_binds_and_stop_releases(self) -> None:
        self.assertTrue(mcp_chatter_tools.buddy_chatter_start()["running"])
        self.assertTrue(self.socket_path.exists())
        self.assertFalse(mcp_chatter_tools.buddy_chatter_stop()["running"])
        self.assertFalse(self.socket_path.exists())

    def test_retuning_rebuilds_the_service_with_the_new_pacing(self) -> None:
        mcp_chatter_tools.buddy_chatter_start()
        status = mcp_chatter_tools.buddy_chatter_start(gap_min=5.0, gap_max=5.0, voice_every=4)
        self.assertTrue(status["running"])
        self.assertEqual(status["voice_every"], 4)
        self.assertEqual(status["next_gap_s"], 5.0)

    def test_arguments_left_alone_keep_their_value(self) -> None:
        mcp_chatter_tools.buddy_chatter_start(voice_every=7)
        cfg = mcp_state.chatter_service().cfg
        self.assertEqual(cfg.voice_every, 7)
        self.assertEqual(cfg.gap_min, 40.0)

    def test_the_model_and_the_effort_can_be_retuned_without_a_restart(self) -> None:
        status = mcp_chatter_tools.buddy_chatter_start(model="haiku", effort="high", batch=3)
        cfg = mcp_state.chatter_service().cfg
        self.assertEqual(cfg.model, "haiku")
        self.assertEqual(cfg.effort, "high")
        self.assertEqual(cfg.batch, 3)
        # Reported, or "which model is writing this" is unanswerable
        # from outside the process.
        self.assertEqual(status["model"], "haiku")
        self.assertEqual(status["effort"], "high")

    def test_an_empty_model_keeps_the_configured_one(self) -> None:
        mcp_chatter_tools.buddy_chatter_start(model="haiku")
        mcp_chatter_tools.buddy_chatter_start(effort="high")
        cfg = mcp_state.chatter_service().cfg
        self.assertEqual(cfg.model, "haiku")

    def test_the_default_model_is_sonnet(self) -> None:
        self.assertEqual(mcp_chatter_tools.buddy_chatter_status()["model"], "sonnet")


if __name__ == "__main__":
    unittest.main()
