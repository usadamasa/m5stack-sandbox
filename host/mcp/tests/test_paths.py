"""Where the daemon keeps its config, its state and its socket.

The point of moving off repo-relative paths: the daemon is started from
whatever directory the user happens to be in, and the hook fires from
whatever project the session is in. Neither can find the other through a
git root, because they are not in the same repository any more.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import buddy_paths
from buddy_chatter import ChatterConfig


class XdgTests(unittest.TestCase):
    def test_the_xdg_variables_are_honoured(self) -> None:
        env = {"XDG_CONFIG_HOME": "/x/cfg", "XDG_STATE_HOME": "/x/state"}
        self.assertEqual(buddy_paths.config_dir(env), Path("/x/cfg/buddy"))
        self.assertEqual(buddy_paths.state_dir(env), Path("/x/state/buddy"))

    def test_the_defaults_are_the_xdg_defaults(self) -> None:
        env = {"HOME": "/home/u"}
        self.assertEqual(buddy_paths.config_dir(env), Path("/home/u/.config/buddy"))
        self.assertEqual(buddy_paths.state_dir(env), Path("/home/u/.local/state/buddy"))

    def test_a_relative_xdg_value_is_ignored(self) -> None:
        # The spec says a relative value is invalid and the default
        # applies. A daemon that resolved it against its own cwd would
        # put its pid file somewhere different on every start.
        env = {"HOME": "/home/u", "XDG_STATE_HOME": "relative/path"}
        self.assertEqual(buddy_paths.state_dir(env), Path("/home/u/.local/state/buddy"))

    def test_the_socket_lives_under_the_state_directory(self) -> None:
        env = {"XDG_STATE_HOME": "/x/state"}
        self.assertEqual(buddy_paths.socket_path(env), Path("/x/state/buddy/chatter.sock"))

    def test_the_socket_can_be_overridden_outright(self) -> None:
        # The tests and the standalone runner both need a socket that is
        # not the live one.
        env = {"XDG_STATE_HOME": "/x/state", "BUDDY_CHATTER_SOCKET": "/tmp/other.sock"}
        self.assertEqual(buddy_paths.socket_path(env), Path("/tmp/other.sock"))

    def test_the_pid_and_the_log_sit_beside_it(self) -> None:
        env = {"XDG_STATE_HOME": "/x/state"}
        self.assertEqual(buddy_paths.pid_path(env), Path("/x/state/buddy/buddy-mcpd.pid"))
        self.assertEqual(buddy_paths.log_path(env), Path("/x/state/buddy/buddy-mcpd.log"))


class ConfigFileTests(unittest.TestCase):
    """`config.toml` reaches the rest of the code as `BUDDY_*` names.

    Flattening to the environment rather than to a settings object: the
    server and the chatter already read `BUDDY_*`, every one of those
    names is documented, and a config file that maps onto them one for
    one has nothing of its own to explain.
    """

    def _write(self, body: str) -> dict[str, str]:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(body, encoding="utf-8")
            return buddy_paths.config_env(path)

    def test_a_missing_file_is_not_an_error(self) -> None:
        self.assertEqual(buddy_paths.config_env(Path("/nowhere/config.toml")), {})

    def test_top_level_keys_become_buddy_names(self) -> None:
        env = self._write('port = "/dev/cu.usbmodem101"\nhttp_port = 8787\n')
        self.assertEqual(env["BUDDY_PORT"], "/dev/cu.usbmodem101")
        self.assertEqual(env["BUDDY_HTTP_PORT"], "8787")

    def test_a_table_becomes_the_middle_of_the_name(self) -> None:
        env = self._write('[chatter]\ngap_min = 40.0\nmodel = "haiku"\n')
        self.assertEqual(env["BUDDY_CHATTER_GAP_MIN"], "40.0")
        self.assertEqual(env["BUDDY_CHATTER_MODEL"], "haiku")

    def test_booleans_are_written_the_way_the_readers_parse_them(self) -> None:
        # `_bool_env` and `_connect_on_start_wanted` both take "1"/"0",
        # not TOML's "true"/"false".
        env = self._write("connect_on_start = true\n\n[chatter]\nenabled = false\n")
        self.assertEqual(env["BUDDY_CONNECT_ON_START"], "1")
        self.assertEqual(env["BUDDY_CHATTER_ENABLED"], "0")

    def test_a_broken_file_is_reported_rather_than_ignored(self) -> None:
        # Silently falling back would mean a typo in the port shows up
        # as "the wrong device", days later.
        with self.assertRaises(ValueError) as caught:
            self._write("port = [unclosed\n")
        self.assertIn("config.toml", str(caught.exception))


class EnvironmentTests(unittest.TestCase):
    def test_the_environment_wins_over_the_file(self) -> None:
        merged = buddy_paths.merge_env({"BUDDY_PORT": "/dev/env"}, {"BUDDY_PORT": "/dev/file"})
        self.assertEqual(merged["BUDDY_PORT"], "/dev/env")

    def test_the_file_fills_in_what_the_environment_left_out(self) -> None:
        merged = buddy_paths.merge_env({"PATH": "/usr/bin"}, {"BUDDY_PORT": "/dev/file"})
        self.assertEqual(merged["BUDDY_PORT"], "/dev/file")
        self.assertEqual(merged["PATH"], "/usr/bin")

    def test_an_empty_environment_value_does_not_count_as_set(self) -> None:
        # `FOO=` in a shell is how a variable gets unset by accident;
        # letting it shadow the config file would be a puzzle to debug.
        merged = buddy_paths.merge_env({"BUDDY_PORT": ""}, {"BUDDY_PORT": "/dev/file"})
        self.assertEqual(merged["BUDDY_PORT"], "/dev/file")


class ReachesTheReadersTests(unittest.TestCase):
    """A config file with nobody reading it is the failure worth testing."""

    def test_the_chatter_settings_come_off_the_file(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp) / "buddy"
            cfg_dir.mkdir()
            (cfg_dir / "config.toml").write_text(
                '[chatter]\ngap_min = 5.0\nmodel = "haiku"\n', encoding="utf-8"
            )
            env = {"XDG_CONFIG_HOME": tmp, "XDG_STATE_HOME": tmp, "HOME": tmp}
            with mock.patch.dict("os.environ", env, clear=True):
                cfg = ChatterConfig.from_env()
        self.assertEqual(cfg.gap_min, 5.0)
        self.assertEqual(cfg.model, "haiku")

    def test_the_environment_still_overrides_it(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp) / "buddy"
            cfg_dir.mkdir()
            (cfg_dir / "config.toml").write_text('[chatter]\nmodel = "haiku"\n', encoding="utf-8")
            env = {
                "XDG_CONFIG_HOME": tmp,
                "XDG_STATE_HOME": tmp,
                "HOME": tmp,
                "BUDDY_CHATTER_MODEL": "opus",
            }
            with mock.patch.dict("os.environ", env, clear=True):
                cfg = ChatterConfig.from_env()
        self.assertEqual(cfg.model, "opus")


if __name__ == "__main__":
    unittest.main()
