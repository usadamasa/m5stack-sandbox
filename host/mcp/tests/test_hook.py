"""Contract tests for the hook that feeds the chatter.

The hook lives in the shared `.agents/hooks/` rather than in this package.
It is loaded here by path anyway:
what it puts on the wire and what `parse_event` takes off it are two
halves of one format, and nothing else checks that they still agree.

The socket path is the same kind of pair: the hook computes it with the
standard library alone and `buddy_paths` computes it for the daemon, and
a disagreement means a device that has simply gone quiet.
"""

import importlib.util
import json
import sys
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

import buddy_paths
from buddy_chatter import Event, parse_event

HOOK_PATH = Path(__file__).resolve().parents[3] / ".agents" / "hooks" / "buddy_chatter_notify.py"


def _load_hook() -> tuple[
    Callable[[Mapping[str, Any]], tuple[str, str] | None],
    Callable[[dict[str, str]], str],
]:
    """Import the hook by path and pull out the functions tested.

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
        cast("Callable[[Mapping[str, Any]], tuple[str, str] | None]", module.classify),
        cast("Callable[[dict[str, str]], str]", module.socket_path),
    )


classify, hook_socket_path = _load_hook()


class HookExistsTests(unittest.TestCase):
    def test_the_registered_path_is_the_one_tested(self) -> None:
        # `.claude/settings.json` names this path; a rename that misses
        # it is silent.
        self.assertTrue(HOOK_PATH.is_file(), HOOK_PATH)


class SocketPathTests(unittest.TestCase):
    """The hook and the daemon have to name the same file.

    They cannot share code — the hook runs on the system `python3` with
    nothing of this repository importable — so the agreement is checked
    here instead, environment by environment.
    """

    ENVS = (
        {"HOME": "/home/u"},
        {"HOME": "/home/u", "XDG_STATE_HOME": "/x/state"},
        {"HOME": "/home/u", "XDG_STATE_HOME": "not/absolute"},
        {"HOME": "/home/u", "BUDDY_CHATTER_SOCKET": "/tmp/explicit.sock"},
    )

    def test_both_sides_agree(self) -> None:
        for env in self.ENVS:
            with self.subTest(env=env):
                self.assertEqual(hook_socket_path(dict(env)), str(buddy_paths.socket_path(env)))


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

    def _round_trip(self, payload: Mapping[str, Any]) -> Event | None:
        classified = classify(payload)
        assert classified is not None
        kind, detail = classified
        message: dict[str, str] = {"kind": kind, "detail": detail}
        return parse_event(json.dumps(message, ensure_ascii=False).encode())

    def test_a_stop_event_survives_the_trip(self) -> None:
        event = self._round_trip({"hook_event_name": "Stop"})
        assert event is not None
        self.assertEqual(event.kind, "stop")

    def test_a_prompt_survives_with_its_text(self) -> None:
        event = self._round_trip({"hook_event_name": "UserPromptSubmit", "prompt": "デプロイして"})
        assert event is not None
        self.assertEqual(event.kind, "prompt")
        self.assertEqual(event.detail, "デプロイして")


if __name__ == "__main__":
    unittest.main()
