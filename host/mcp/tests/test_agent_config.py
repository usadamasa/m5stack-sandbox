"""Claude Code / Codex 共通設定の配線を固定する契約テスト。"""

import json
import os
import tomllib
import unittest
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[3]


def _json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as src:
        value = json.load(src)
    assert isinstance(value, dict)
    return cast("dict[str, Any]", value)


class CanonicalSourceTests(unittest.TestCase):
    def test_claude_instructions_are_a_link_to_agents(self) -> None:
        claude = ROOT / "CLAUDE.md"
        self.assertTrue(claude.is_symlink())
        self.assertEqual(claude.resolve(), (ROOT / "AGENTS.md").resolve())

    def test_claude_skills_and_hooks_are_links_to_agents(self) -> None:
        for name in ("skills", "hooks"):
            claude = ROOT / ".claude" / name
            agents = ROOT / ".agents" / name
            with self.subTest(name=name):
                self.assertTrue(claude.is_symlink())
                self.assertTrue(claude.samefile(agents))


class AdapterConfigTests(unittest.TestCase):
    def test_mcp_adapters_use_the_same_portable_launcher(self) -> None:
        claude = _json(ROOT / ".mcp.json")["mcpServers"]["buddy"]
        with (ROOT / ".codex" / "config.toml").open("rb") as src:
            codex = tomllib.load(src)["mcp_servers"]["buddy"]

        for key in ("command", "args", "env"):
            with self.subTest(key=key):
                self.assertEqual(claude[key], codex[key])
        self.assertIn(".agents/bin/buddy-mcp", " ".join(claude["args"]))
        self.assertNotIn(str(ROOT), json.dumps(claude))

    def test_hook_adapters_call_the_shared_script(self) -> None:
        claude = _json(ROOT / ".claude" / "settings.json")["hooks"]
        codex = _json(ROOT / ".codex" / "hooks.json")["hooks"]
        common_events = {"PreToolUse", "PostToolUse", "UserPromptSubmit", "SessionStart", "Stop"}

        self.assertLessEqual(common_events, claude.keys())
        self.assertEqual(common_events, codex.keys())
        for agent, hooks in (("claude-code", claude), ("codex", codex)):
            for event in common_events:
                with self.subTest(agent=agent, event=event):
                    command = hooks[event][0]["hooks"][0]["command"]
                    self.assertIn(".agents/hooks/buddy_chatter_notify.py", command)
                    self.assertIn(f"--agent {agent}", command)
                    self.assertNotIn(str(ROOT), command)

    def test_shared_launcher_is_executable(self) -> None:
        launcher = ROOT / ".agents" / "bin" / "buddy-mcp"
        self.assertTrue(launcher.is_file())
        self.assertTrue(os.access(launcher, os.X_OK))


if __name__ == "__main__":
    unittest.main()
