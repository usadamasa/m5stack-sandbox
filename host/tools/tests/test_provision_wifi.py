# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 MAS Event + Design, LLC
# Copyright 2026 usadamasa
#
# The `UPSTREAM` fixture below is a trimmed excerpt of wifi_event.py from
# moremas/build-with-claude.
"""Rewriting the credentials the device auto-connects with at boot.

The interesting part is not the transfer — that is `mpremote`'s, and
`test_push.py` covers what this repository adds to it. It is the edit:
two assignments in a 3.8 KB upstream file have to be replaced and
nothing else may move, on a device whose only recovery from a corrupt
`wifi_event.py` is running this again.

So the edit is a pure function over bytes, and these tests pin the
things that would fail silently: matching a line that is not the
assignment, matching two and taking the first, and emitting source that
does not parse because the passphrase contained a quote.
"""

from __future__ import annotations

import unittest

# buddy-host-link は workspace 内の同居パッケージで py.typed が無いため
# stub 未整備扱いになる。py.typed の追加は host/link 側の担当範囲。
from fake_repl import FakeRepl
from provision_wifi import (
    DEST,
    ProvisionError,
    patch_credentials,
    provision,
    read_credentials,
    reset,
)

# Trimmed from the real /flash/wifi_event.py. The docstring lines are
# kept verbatim because they mention SSID and PASSWORD in prose, which
# is exactly what a careless pattern would rewrite.
UPSTREAM = b'''"""Connect to the event WiFi network on boot.

The credentials below are intentionally part of the public repo for
the event-bundle case. To use this bundle elsewhere:

  - Replace ``SSID`` / ``PASSWORD`` with your own, OR
  - Remove the ``wifi_event.connect_with_splash(...)`` call.
"""

# --- EVENT WIFI ---------------------------------------------------------
# Public broadcast at the venue. Replace for use elsewhere.
SSID = "cardputer"
PASSWORD = "cardconnect"
# -----------------------------------------------------------------------

CONNECT_TIMEOUT_MS = 8000


def connect(timeout_ms=CONNECT_TIMEOUT_MS):
    import network

    sta = network.WLAN(network.STA_IF)
    sta.connect(SSID, PASSWORD)
    return {"ok": True, "ssid": SSID}
'''


class PatchCredentialsTest(unittest.TestCase):
    def test_replaces_both_assignments(self) -> None:
        out = patch_credentials(UPSTREAM, "MyNet", "hunter2")
        self.assertEqual(read_credentials(out), ("MyNet", "hunter2"))

    def test_leaves_the_prose_alone(self) -> None:
        out = patch_credentials(UPSTREAM, "MyNet", "hunter2")
        self.assertIn(b"Replace ``SSID`` / ``PASSWORD`` with your own", out)
        self.assertIn(b"sta.connect(SSID, PASSWORD)", out)

    def test_changes_only_the_two_lines(self) -> None:
        before = UPSTREAM.decode().splitlines()
        after = patch_credentials(UPSTREAM, "MyNet", "hunter2").decode().splitlines()
        self.assertEqual(len(before), len(after))
        differing = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
        self.assertEqual([before[i].split(" =")[0] for i in differing], ["SSID", "PASSWORD"])

    def test_a_quote_in_the_passphrase_still_parses(self) -> None:
        # The reason this is repr and not concatenation: an apostrophe
        # would otherwise close the literal and leave the rest of the
        # line as code, on a file the device imports at boot.
        out = patch_credentials(UPSTREAM, "My'Net", 'it"s a \\ secret')
        self.assertEqual(read_credentials(out), ("My'Net", 'it"s a \\ secret'))
        compile(out, "wifi_event.py", "exec")

    def test_non_ascii_ssid_survives(self) -> None:
        out = patch_credentials(UPSTREAM, "うちのWiFi", "パスワード")
        self.assertEqual(read_credentials(out), ("うちのWiFi", "パスワード"))
        compile(out, "wifi_event.py", "exec")

    def test_is_idempotent(self) -> None:
        once = patch_credentials(UPSTREAM, "MyNet", "hunter2")
        self.assertEqual(patch_credentials(once, "MyNet", "hunter2"), once)

    def test_rejects_a_file_without_the_assignment(self) -> None:
        with self.assertRaises(ProvisionError):
            patch_credentials(b"PASSWORD = 'x'\n", "MyNet", "hunter2")

    def test_rejects_a_second_assignment(self) -> None:
        # Taking the first and leaving the second would write a file
        # whose credentials are not the ones reported back.
        doubled = UPSTREAM + b'\nSSID = "other"\n'
        with self.assertRaises(ProvisionError):
            patch_credentials(doubled, "MyNet", "hunter2")

    def test_rejects_an_empty_ssid(self) -> None:
        with self.assertRaises(ProvisionError):
            patch_credentials(UPSTREAM, "", "hunter2")


class ReadCredentialsTest(unittest.TestCase):
    def test_reads_the_upstream_pair(self) -> None:
        self.assertEqual(read_credentials(UPSTREAM), ("cardputer", "cardconnect"))

    def test_rejects_a_computed_value(self) -> None:
        # literal_eval, not eval: nothing off the device is executed
        # here, so an assignment that is not a literal is an error
        # rather than a code path.
        with self.assertRaises(ProvisionError):
            read_credentials(b'SSID = os.environ["X"]\nPASSWORD = "y"\n')


class ProvisionTest(unittest.TestCase):
    def _repl(self, content: bytes = UPSTREAM) -> FakeRepl:
        repl = FakeRepl()
        repl.files[DEST] = content
        return repl

    def test_writes_the_patched_file_back(self) -> None:
        repl = self._repl()
        provision(repl, "MyNet", "hunter2", quiet=True)
        self.assertEqual(read_credentials(repl.files[DEST]), ("MyNet", "hunter2"))

    def test_reports_the_ssid_and_never_the_passphrase(self) -> None:
        repl = self._repl()
        result = provision(repl, "MyNet", "hunter2", quiet=True)
        self.assertEqual(result["ssid"], "MyNet")
        self.assertNotIn("hunter2", repr(result))

    def test_verifies_by_reading_back(self) -> None:
        # A short write is what a truncated transfer looks like from
        # here, and it must not be reported as provisioned.
        repl = self._repl()
        original = repl.fs_writefile

        def truncating(dest: str, data: bytes, **kw: object) -> None:
            original(dest, data[: len(data) // 2])

        repl.fs_writefile = truncating  # type: ignore[method-assign]
        with self.assertRaises(ProvisionError):
            provision(repl, "MyNet", "hunter2", quiet=True)

    def test_a_missing_file_is_an_error_not_a_new_file(self) -> None:
        # Creating wifi_event.py from scratch would mean carrying a copy
        # of an upstream file in this repository, which is the thing the
        # overlay exists to avoid.
        repl = FakeRepl()
        with self.assertRaises(ProvisionError):
            provision(repl, "MyNet", "hunter2", quiet=True)


class ResetTest(unittest.TestCase):
    """Rebooting is the step whose ordinary outcome is two exceptions.

    Both were seen on hardware: `exec` gets no answer because the device
    is already gone, and `close` then fails clearing RTS on a vanished
    device with `[Errno 6] Device not configured`. Letting either escape
    turns a successful provision into a traceback after the write has
    already landed.
    """

    def test_survives_a_transport_that_dies_mid_statement(self) -> None:
        repl = FakeRepl(on_exec=self._boom)
        reset(repl)

    def test_survives_a_close_that_fails(self) -> None:
        repl = FakeRepl()
        repl.close = self._boom  # type: ignore[method-assign]
        reset(repl)

    def test_asks_for_the_reset(self) -> None:
        repl = FakeRepl()
        reset(repl)
        self.assertIn("machine.reset()", repl.source)
        self.assertTrue(repl.closed)

    @staticmethod
    def _boom(*_args: object) -> None:
        raise OSError(6, "Device not configured")


if __name__ == "__main__":
    unittest.main()
