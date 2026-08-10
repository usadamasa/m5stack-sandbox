"""Contract tests for the hook that feeds the chatter.

The hook lives in the shared `.agents/hooks/` rather than in this package.
It is loaded here by path anyway:
what it puts on the wire and what `parse_event` takes off it are two
halves of one format, and nothing else checks that they still agree.

Both agents register the same script, so the payloads exercised below
are Claude Code's and Codex's alike — the schemas are the same.
"""

import importlib.util
import json
import sys
import unittest
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from buddy_chatter import Event, parse_event

HOOK_PATH = Path(__file__).resolve().parents[3] / ".agents" / "hooks" / "buddy_chatter_notify.py"


def _load_hook() -> tuple[
    Callable[[Sequence[str], Mapping[str, str]], str],
    Callable[[Mapping[str, Any]], tuple[str, str] | None],
]:
    """Import the hook by path and pull out the two functions tested.

    Returned as typed callables rather than as a module: attributes off
    a `ModuleType` are untyped, and the point of this file is that the
    two sides of the format agree — which needs the checker awake.
    """
    spec = importlib.util.spec_from_file_location("buddy_chatter_notify", HOOK_PATH)
    assert spec is not None and spec.loader is not None, HOOK_PATH
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return (
        cast("Callable[[Sequence[str], Mapping[str, str]], str]", module.agent_from),
        cast("Callable[[Mapping[str, Any]], tuple[str, str] | None]", module.classify),
    )


agent_from, classify = _load_hook()


class HookExistsTests(unittest.TestCase):
    def test_the_registered_path_is_the_one_tested(self) -> None:
        # Both `.claude/settings.json` and `.codex/hooks.json` name
        # this path; a rename that misses one of them is silent.
        self.assertTrue(HOOK_PATH.is_file(), HOOK_PATH)


class AgentFromTests(unittest.TestCase):
    def test_the_flag_is_read(self) -> None:
        self.assertEqual(agent_from(["--agent", "codex"], {}), "codex")

    def test_the_equals_form_is_read_too(self) -> None:
        self.assertEqual(agent_from(["--agent=claude-code"], {}), "claude-code")

    def test_a_dangling_flag_is_not_an_index_error(self) -> None:
        self.assertEqual(agent_from(["--agent"], {}), "")

    def test_the_flag_beats_the_environment(self) -> None:
        self.assertEqual(agent_from(["--agent", "codex"], {"CLAUDECODE": "1"}), "codex")

    def test_the_environment_is_the_fallback(self) -> None:
        self.assertEqual(agent_from([], {"CODEX_HOME": "/x/.codex"}), "codex")
        self.assertEqual(agent_from([], {"CLAUDECODE": "1"}), "claude-code")

    def test_knowing_nothing_says_nothing(self) -> None:
        # Empty, not a guess: the server's default is a better answer
        # than one invented here.
        self.assertEqual(agent_from([], {"PATH": "/usr/bin"}), "")


class ClassifyTests(unittest.TestCase):
    def test_a_tool_call_carries_its_subject(self) -> None:
        classified = classify(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "uv run pytest"},
            }
        )
        self.assertEqual(classified, ("tool", "Bash: uv run pytest"))

    def test_a_failed_call_is_an_error(self) -> None:
        classified = classify(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "tool_response": {"is_error": True},
            }
        )
        assert classified is not None
        self.assertEqual(classified[0], "error")

    def test_an_event_nobody_speaks_to_is_dropped(self) -> None:
        self.assertIsNone(classify({"hook_event_name": "PreCompact"}))


class WireFormatTests(unittest.TestCase):
    """What the hook sends must be what the server can read."""

    def _round_trip(
        self, payload: Mapping[str, Any], argv: Sequence[str], env: Mapping[str, str]
    ) -> Event | None:
        classified = classify(payload)
        assert classified is not None
        kind, detail = classified
        message: dict[str, str] = {"kind": kind, "detail": detail}
        agent = agent_from(argv, env)
        if agent:
            message["agent"] = agent
        return parse_event(json.dumps(message, ensure_ascii=False).encode())

    def test_a_stop_event_survives_the_trip(self) -> None:
        event = self._round_trip({"hook_event_name": "Stop"}, ["--agent", "claude-code"], {})
        assert event is not None
        self.assertEqual(event.kind, "stop")
        self.assertEqual(event.agent, "claude-code")

    def test_codex_names_itself_the_same_way(self) -> None:
        event = self._round_trip(
            {"hook_event_name": "UserPromptSubmit", "prompt": "デプロイして"},
            ["--agent", "codex"],
            {},
        )
        assert event is not None
        self.assertEqual(event.kind, "prompt")
        self.assertEqual(event.detail, "デプロイして")
        self.assertEqual(event.agent, "codex")

    def test_an_unflagged_hook_still_produces_a_usable_event(self) -> None:
        event = self._round_trip({"hook_event_name": "Stop"}, [], {"PATH": "/usr/bin"})
        assert event is not None
        self.assertEqual(event.kind, "stop")
        self.assertEqual(event.agent, "")


if __name__ == "__main__":
    unittest.main()
