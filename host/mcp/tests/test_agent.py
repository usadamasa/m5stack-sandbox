"""Tests for the agent-identity layer.

What matters here is not the mapping table — that is three lines — but
the two rules that keep the wrong backend from being chosen: an
unrecognised witness must not overwrite a recognised one, and with no
witness at all the answer must be the default rather than `unknown`.
"""

import unittest

from buddy_agent import CLAUDE_CODE, CODEX, UNKNOWN, AgentIdentity, identify, identify_env


class IdentifyTests(unittest.TestCase):
    def test_claude_code(self) -> None:
        self.assertEqual(identify("claude-code"), CLAUDE_CODE)

    def test_codex(self) -> None:
        self.assertEqual(identify("codex"), CODEX)

    def test_matching_is_loose_because_client_names_drift(self) -> None:
        self.assertEqual(identify("Claude Code (desktop)"), CLAUDE_CODE)
        self.assertEqual(identify("codex-mcp-client"), CODEX)
        self.assertEqual(identify("CODEX_CLI/0.147.0"), CODEX)

    def test_codex_wins_when_a_name_claims_both(self) -> None:
        self.assertEqual(identify("codex-for-claude"), CODEX)

    def test_an_unknown_client_is_not_guessed_at(self) -> None:
        self.assertEqual(identify("cursor"), UNKNOWN)
        self.assertEqual(identify(""), UNKNOWN)
        self.assertEqual(identify(None), UNKNOWN)


class IdentifyEnvTests(unittest.TestCase):
    def test_codex_marker(self) -> None:
        self.assertEqual(identify_env({"CODEX_HOME": "/home/x/.codex"}), CODEX)

    def test_claude_code_marker(self) -> None:
        self.assertEqual(identify_env({"CLAUDECODE": "1"}), CLAUDE_CODE)

    def test_an_empty_value_does_not_count(self) -> None:
        self.assertEqual(identify_env({"CLAUDECODE": ""}), UNKNOWN)

    def test_nothing_recognisable(self) -> None:
        self.assertEqual(identify_env({"PATH": "/usr/bin"}), UNKNOWN)


class AgentIdentityTests(unittest.TestCase):
    def test_the_default_applies_until_something_is_seen(self) -> None:
        identity = AgentIdentity()
        self.assertEqual(identity.current, CLAUDE_CODE)
        self.assertEqual(identity.observed, UNKNOWN)

    def test_the_default_can_be_the_other_one(self) -> None:
        self.assertEqual(AgentIdentity(default=CODEX).current, CODEX)

    def test_a_nonsense_default_falls_back_rather_than_being_kept(self) -> None:
        # The default comes from an environment variable, and a typo in
        # one must not name a backend that cannot be built.
        self.assertEqual(AgentIdentity(default="gpt-4").current, CLAUDE_CODE)

    def test_observing_switches_the_answer(self) -> None:
        identity = AgentIdentity()
        self.assertEqual(identity.observe("codex-mcp-client"), CODEX)
        self.assertEqual(identity.current, CODEX)
        self.assertEqual(identity.client_name, "codex-mcp-client")

    def test_the_last_witness_wins(self) -> None:
        identity = AgentIdentity()
        identity.observe("codex")
        identity.observe("claude-code")
        self.assertEqual(identity.current, CLAUDE_CODE)

    def test_an_unrecognised_witness_does_not_clear_a_recognised_one(self) -> None:
        identity = AgentIdentity()
        identity.observe("codex")
        identity.observe("some-other-editor")
        self.assertEqual(identity.current, CODEX)
        self.assertEqual(identity.client_name, "codex")

    def test_status_separates_what_was_seen_from_what_is_used(self) -> None:
        identity = AgentIdentity()
        self.assertEqual(
            identity.status(),
            {"agent": CLAUDE_CODE, "observed": UNKNOWN, "client": "", "default": CLAUDE_CODE},
        )
        identity.observe("codex")
        self.assertEqual(
            identity.status(),
            {"agent": CODEX, "observed": CODEX, "client": "codex", "default": CLAUDE_CODE},
        )


if __name__ == "__main__":
    unittest.main()
