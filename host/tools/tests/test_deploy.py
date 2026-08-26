"""The bytecode deploy, minus the board.

What is left here is the run as a whole and the confirmation it ends
with, which is a launch and an utterance. No speaker: the acks a device
would send are fed to a fake port, and what is asserted is that the app
is started the one-way way, that a refusal is reported as a failed
deploy rather than a successful one, and that the port the link took
over is not closed twice.

The compile is in `test_deploy_build`, the flash rewriting in
`test_deploy_device`, and the bench both of them share in
`deploy_stubs`.
"""

from __future__ import annotations

import unittest
import unittest.mock
from tempfile import TemporaryDirectory

from buddy_deploy import (
    engine_url,
    main,
    verify_by_speech,
)
from buddy_link import DEFAULT_READ_TIMEOUT, LAUNCH_SOURCE
from buddy_wire import Message
from deploy_spec import (
    DEST_ROOT,
    LAUNCHER,
    OVERLAY,
    REPO,
    SERIAL_READ_TIMEOUT_S,
    UPSTREAM,
    VERIFY_TEXT,
    DeployError,
)
from deploy_stubs import Bench, Replies, TalkingPort
from fake_repl import FakeRepl

ENGINE = "http://192.168.0.156:50021"


class SpeechConfirmationTest(unittest.TestCase):
    """The last step: launch what was installed and listen to it.

    A directory listing cannot tell a bundle that imports from one that
    does not, and neither can a successful transfer. This is the step
    that can, so what matters is that a device which will not speak ends
    the run as a failure — and that whatever it printed on the way is
    said out loud in the output rather than being flattened into a
    timeout.
    """

    def setUp(self) -> None:
        self.repl = FakeRepl()
        self.port = TalkingPort()
        self.repl.serial = self.port
        self.said: list[str] = []

    def answers(self, replies: Replies) -> None:
        self.port.replies = replies

    def run_it(self, text: str = VERIFY_TEXT) -> Message:
        return verify_by_speech(self.repl, "/dev/null", text, ENGINE, self.said.append, settle=0.0)

    @property
    def sent(self) -> list[Message]:
        return self.port.frames

    def test_the_app_is_started_the_one_way_way(self) -> None:
        # exec would wait for a return the app never makes: it takes the
        # console over and speaks its own protocol on the same wire.
        self.run_it()
        self.assertEqual(self.repl.launched, [LAUNCH_SOURCE])

    def test_it_asks_for_the_words_on_the_panel_and_then_out_loud(self) -> None:
        ack = self.run_it("デプロイ完了なのだ")
        self.assertEqual([frame["cmd"] for frame in self.sent], ["chat.say", "speak.say"])
        self.assertEqual([frame["text"] for frame in self.sent], ["デプロイ完了なのだ"] * 2)
        self.assertEqual(self.sent[1]["url"], ENGINE)
        self.assertEqual(ack["blocks"], 40)

    def test_the_port_the_link_took_over_is_closed_once(self) -> None:
        # The transport is not closed by the caller after this, so the
        # link is the only thing that can release the port.
        self.run_it()
        self.assertTrue(self.repl.serial.closed)
        self.assertFalse(self.repl.closed)

    def test_a_device_that_refuses_fails_the_deploy(self) -> None:
        self.answers(
            {
                "chat.say": [{"ack": "chat.say", "ok": True}],
                "speak.say": [{"ack": "speak.say", "ok": False, "err": "ECONNREFUSED"}],
            }
        )
        with self.assertRaises(DeployError) as caught:
            self.run_it()
        self.assertIn("ECONNREFUSED", str(caught.exception))

    def test_playback_that_ends_early_is_not_a_confirmation(self) -> None:
        self.answers(
            {
                "chat.say": [{"ack": "chat.say", "ok": True}],
                "speak.say": [
                    {"ack": "speak.say", "ok": True, "bytes": 81920, "rate": 16000},
                    {"ack": "speak.end", "ok": False, "blocks": 3, "stalls": 0},
                ],
            }
        )
        with self.assertRaises(DeployError) as caught:
            self.run_it()
        self.assertIn("ended early", str(caught.exception))

    def test_what_the_app_printed_while_failing_is_reported(self) -> None:
        # The case this exists for: the bundle does not import. Without
        # the traceback the only symptom is an ack that never came.
        self.answers({})
        self.port.feed(b"Traceback (most recent call last):\r\n")
        self.port.feed(b"ImportError: no module named 'buddy_tts'\r\n")
        with (
            unittest.mock.patch("buddy_deploy.VERIFY_TIMEOUT_S", 0.05),
            self.assertRaises(DeployError),
        ):
            self.run_it()
        self.assertTrue(
            any("ImportError" in line for line in self.said),
            f"the device's own output never made it into the log: {self.said}",
        )

    def test_a_loopback_engine_is_refused_before_the_repl_is_spent(self) -> None:
        # Reachable from this Mac and from nowhere else. Discovered
        # after the launch it costs another round trip to try again.
        with self.assertRaises(DeployError):
            engine_url("http://127.0.0.1:50021")


class CliTest(unittest.TestCase):
    def test_a_deploy_without_a_port_is_refused_before_anything_is_built(self) -> None:
        self.assertEqual(main([]), 2)

    def test_compile_only_needs_no_port(self) -> None:
        with TemporaryDirectory() as tmp:
            rc = main(["--compile-only", "--build", tmp, "--vendor", f"{tmp}/vendor"])
        self.assertEqual(rc, 0)


class WholeRunTest(unittest.TestCase):
    """One pass over a device that has never been converted.

    The parts are covered by `test_deploy_build` and
    `test_deploy_device`; what this adds is the order they run in.
    Getting that wrong is how a file gets deleted before it has been
    archived, and no unit test of either half would notice.

    `--no-speak`, so this stays about the flash rewriting; the launch
    and the utterance have their own tests.
    """

    extra_args: tuple[str, ...] = ("--no-speak",)

    def setUp(self) -> None:
        self.bench = Bench(self)
        self.bench.seed_unconverted()
        self.repl = self.bench.repl
        self.rc = self.bench.deploy(*self.extra_args)

    def test_it_succeeds(self) -> None:
        self.assertEqual(self.rc, 0)

    def test_every_module_is_bytecode_and_no_source_is_left_beside_it(self) -> None:
        for rel in OVERLAY:
            stem = rel[: -len(".py")]
            self.assertIn(f"{DEST_ROOT}/{stem}.mpy", self.repl.files)
            self.assertNotIn(f"{DEST_ROOT}/{stem}.py", self.repl.files)
        for name in UPSTREAM:
            self.assertIn(f"{DEST_ROOT}/{name}.mpy", self.repl.files)
            self.assertNotIn(f"{DEST_ROOT}/{name}.py", self.repl.files)

    def test_the_launcher_is_ours_and_still_source(self) -> None:
        # main() takes the launcher from device/, not from the bench:
        # /flash/main.py is executed as source, so what lands has to be
        # the real file byte for byte.
        self.assertEqual(
            self.repl.files[f"{DEST_ROOT}/{LAUNCHER}"], (REPO / "device" / LAUNCHER).read_bytes()
        )
        self.assertNotIn(f"{DEST_ROOT}/main.mpy", self.repl.files)
        self.assertEqual(
            (self.bench.vendor / LAUNCHER).read_text(), "print('upstream menu + NimBLE')\n"
        )

    def test_nothing_was_deleted_that_was_not_archived_first(self) -> None:
        for path in self.repl.removed:
            rel = path[len(f"{DEST_ROOT}/") :]
            if rel.endswith(".py") and rel[: -len(".py")] in {
                job[: -len(".mpy")] for job in (f"{r[:-3]}.mpy" for r in OVERLAY)
            }:
                # Overlay sources are in device/ — the archive is git.
                continue
            self.assertTrue(
                (self.bench.vendor / rel).is_file(), f"{rel} was removed without being archived"
            )

    def test_the_port_gets_a_read_timeout(self) -> None:
        # mpremote leaves it at None, and every read in a transfer goes
        # through it. Without this the run can only be ended from outside.
        self.assertEqual(self.repl.serial.timeout, SERIAL_READ_TIMEOUT_S)


class NoSpeakTest(unittest.TestCase):
    """The escape hatch, and the reason anyone would want it.

    Its own run rather than an assertion on `WholeRunTest`, which the
    confirmation subclass inherits from: what is being asserted here is
    exactly what that subclass inverts.
    """

    def test_it_leaves_the_device_at_the_repl(self) -> None:
        # The REPL is what the next deploy needs. Getting it back once
        # the app has the console costs an interrupt and a teardown.
        bench = Bench(self)
        bench.seed_unconverted()
        self.assertEqual(bench.deploy("--no-speak"), 0)
        self.assertEqual(bench.repl.launched, [])
        self.assertTrue(bench.repl.closed)


class WholeRunWithConfirmationTest(WholeRunTest):
    """The same pass, ending the way a real deploy ends: out loud.

    Everything `WholeRunTest` asserts still has to hold — the ordering
    is inherited rather than restated — and what is added is that the
    run does not stop at a successful transfer.
    """

    extra_args = ("--engine", ENGINE, "--settle", "0")

    def test_it_launched_the_app_and_got_its_utterance_played(self) -> None:
        self.assertEqual(self.repl.launched, [LAUNCH_SOURCE])
        frames = self.bench.port.frames
        self.assertEqual([frame["cmd"] for frame in frames], ["chat.say", "speak.say"])
        self.assertEqual(frames[-1]["text"], VERIFY_TEXT)

    def test_the_transport_is_not_closed_over_the_top_of_the_link(self) -> None:
        # mpremote's close() drops RTS/DTR, which on a port the link has
        # already closed raises out of the finally block.
        self.assertFalse(self.repl.closed)
        self.assertTrue(self.repl.serial.closed)

    def test_the_port_gets_a_read_timeout(self) -> None:
        # Superseded: the launch hands the port to the link, which polls
        # `in_waiting` and wants the short timeout instead.
        self.assertEqual(self.repl.serial.timeout, DEFAULT_READ_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
