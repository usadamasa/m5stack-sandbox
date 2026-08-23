"""Resume behaviour of the firmware fetcher.

The reason this repository owns a copy of upstream's fetcher is the
`Range: bytes=N-` resumption in `fetch_manifest()`. That code only runs
when a connection dies partway through, which never happens on a good
network and always happens behind the sandbox's CONNECT proxy — so it is
exactly the kind of path that silently rots unless a test drives it.

Nothing here touches the network: `open_https` is replaced with a
scripted stand-in. 差し替える先はモジュールごとに違う — 呼ぶ側が
見ているグローバルを差し替えないと、素通りして本物を叩きに行く。
"""

import base64
import hashlib
import json
import os
import unittest
import urllib.request
from http.client import IncompleteRead
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar

import fetch_firmware
import firmware_manifest


class FakeResponse:
    """Serves one canned body, optionally cutting it short."""

    def __init__(
        self,
        body: bytes,
        status: int = 200,
        cut_after: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._body = body
        self.status = status
        self._cut_after = cut_after
        self.headers: dict[str, str] = headers or {}
        self._pos = 0

    def read(self, size: int = -1) -> bytes:
        if self._cut_after is None:
            if size < 0:
                return self._body
            # download() streams in fixed-size chunks.
            chunk = self._body[self._pos : self._pos + size]
            self._pos += len(chunk)
            return chunk
        # What urllib raises when the peer closes before Content-Length
        # is satisfied: the bytes that did arrive ride along on .partial.
        raise IncompleteRead(self._body[: self._cut_after], len(self._body) - self._cut_after)

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class ManifestResumeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest: list[fetch_firmware.ManifestEntry] = [
            {"name": "UIFlow2.0", "category": "cardputer", "versions": []}
        ]
        self.body = json.dumps(self.manifest).encode()
        self.requests: list[str | urllib.request.Request] = []

        # manifest 側が見ているのは firmware_manifest のグローバルなので、
        # 差し替えるのもそちら。fetch_firmware を差し替えても効かない。
        real = firmware_manifest.open_https
        self.addCleanup(setattr, firmware_manifest, "open_https", real)

    def _install(self, responses: list[FakeResponse]) -> None:
        queue = list(responses)

        def fake_open(url: str | urllib.request.Request, timeout: float = 30.0) -> FakeResponse:
            self.requests.append(url)
            return queue.pop(0)

        firmware_manifest.open_https = fake_open  # pyright: ignore[reportAttributeAccessIssue]

    def _range_headers(self) -> list[str | None]:
        return [
            req.headers.get("Range") if isinstance(req, urllib.request.Request) else None
            for req in self.requests
        ]

    def test_clean_read_asks_once(self) -> None:
        self._install([FakeResponse(self.body)])
        self.assertEqual(fetch_firmware.fetch_manifest(), self.manifest)
        self.assertEqual(self._range_headers(), [None])

    def test_truncated_read_resumes_from_the_offset(self) -> None:
        cut = 10
        self._install(
            [
                FakeResponse(self.body, cut_after=cut),
                FakeResponse(self.body[cut:], status=206),
            ]
        )
        self.assertEqual(fetch_firmware.fetch_manifest(), self.manifest)
        # The second request must ask for the tail, not the whole thing:
        # restarting from 0 is what never converges behind the proxy.
        self.assertEqual(self._range_headers(), [None, f"bytes={cut}-"])

    def test_server_ignoring_range_starts_over(self) -> None:
        # A 200 in response to a Range request means the body is the
        # whole document again; appending it to what we kept would
        # produce garbage JSON.
        cut = 10
        self._install(
            [
                FakeResponse(self.body, cut_after=cut),
                FakeResponse(self.body, status=200),
            ]
        )
        self.assertEqual(fetch_firmware.fetch_manifest(), self.manifest)

    def test_gives_up_with_the_underlying_error(self) -> None:
        self._install([FakeResponse(self.body, cut_after=1) for _ in range(3)])
        with self.assertRaises(RuntimeError) as caught:
            fetch_firmware.fetch_manifest(max_attempts=3)
        self.assertIn("after 3 attempts", str(caught.exception))


class PickFirmwareTest(unittest.TestCase):
    MANIFEST: ClassVar[list[fetch_firmware.ManifestEntry]] = [
        {
            "category": "Cardputer",
            "name": "UIFlow2.0 Cardputer-Adv",
            "versions": [
                {"version": "v2.3.0", "file": "a" * 32 + ".bin"},
                {"version": "v2.4.0-rc1", "file": "b" * 32 + ".bin"},
                {"version": "v2.4.0", "file": "c" * 32 + ".bin"},
                {"version": "v2.5.0", "file": "d" * 32 + ".bin", "published": False},
            ],
        }
    ]

    def test_picks_the_newest_stable(self) -> None:
        # Manifest order is chronological, and an rc must not win over a
        # release that shipped after it.
        _entry, version = fetch_firmware.pick_firmware(self.MANIFEST, "cardputer-adv")
        self.assertEqual(version.get("version"), "v2.4.0")

    def test_unpublished_versions_are_skipped(self) -> None:
        _entry, version = fetch_firmware.pick_firmware(self.MANIFEST, "cardputer-adv")
        self.assertNotEqual(version.get("version"), "v2.5.0")

    def test_unknown_variant_lists_the_known_ones(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            fetch_firmware.pick_firmware(self.MANIFEST, "not-a-board")
        self.assertIn("cardputer-adv", str(caught.exception))

    def test_missing_entry_names_what_it_did_see(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            fetch_firmware.pick_firmware(self.MANIFEST, "cardputer")
        self.assertIn("UIFlow2.0 Cardputer-Adv", str(caught.exception))


class DownloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dest_dir = self._tmp.name
        self.body = b"firmware-bytes" * 100
        self.md5 = base64.b64encode(hashlib.md5(self.body).digest()).decode()
        self.version: fetch_firmware.FirmwareVersion = {"file": "e" * 32 + ".bin"}
        self.requests: list[str | urllib.request.Request] = []

        # download 側が見ているのは fetch_firmware のグローバル。
        real = fetch_firmware.open_https
        self.addCleanup(setattr, fetch_firmware, "open_https", real)

    def _install(self, responses: list[FakeResponse]) -> None:
        queue = list(responses)

        def fake_open(url: str | urllib.request.Request, timeout: float = 30.0) -> FakeResponse:
            self.requests.append(url)
            return queue.pop(0)

        fetch_firmware.open_https = fake_open  # pyright: ignore[reportAttributeAccessIssue]

    def test_writes_the_binary_and_its_sidecar(self) -> None:
        self._install([FakeResponse(self.body, headers={"Content-MD5": self.md5})])
        dest = fetch_firmware.download({}, self.version, dest_dir=self.dest_dir)
        self.assertEqual(Path(dest).read_bytes(), self.body)
        self.assertEqual(
            Path(dest + ".md5").read_text().strip(), hashlib.md5(self.body).hexdigest()
        )

    def test_second_call_is_a_cache_hit(self) -> None:
        self._install([FakeResponse(self.body, headers={"Content-MD5": self.md5})])
        dest = fetch_firmware.download({}, self.version, dest_dir=self.dest_dir)
        # No second response is queued: another fetch would pop an empty
        # list and blow up.
        self.assertEqual(fetch_firmware.download({}, self.version, dest_dir=self.dest_dir), dest)
        self.assertEqual(len(self.requests), 1)

    def test_missing_content_md5_refuses_to_install(self) -> None:
        self._install([FakeResponse(self.body)])
        with self.assertRaises(SystemExit) as caught:
            fetch_firmware.download({}, self.version, dest_dir=self.dest_dir)
        self.assertIn("Content-MD5", str(caught.exception))
        self.assertEqual(os.listdir(self.dest_dir), [])

    def test_wrong_digest_leaves_nothing_behind(self) -> None:
        wrong = base64.b64encode(hashlib.md5(b"other").digest()).decode()
        self._install([FakeResponse(self.body, headers={"Content-MD5": wrong})])
        with self.assertRaises(SystemExit) as caught:
            fetch_firmware.download({}, self.version, dest_dir=self.dest_dir)
        self.assertIn("MD5 mismatch", str(caught.exception))
        # A partially written blob left at the cache key would be served
        # as a cache hit on the next run.
        self.assertEqual(os.listdir(self.dest_dir), [])

    def test_hostile_file_field_is_rejected(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            fetch_firmware.download({}, {"file": "../../etc/passwd"}, dest_dir=self.dest_dir)
        self.assertIn("not in the allowed", str(caught.exception))


class FileFieldTest(unittest.TestCase):
    # The manifest's `file` value lands in both a URL and a filesystem
    # path, so the allow-list is load-bearing rather than cosmetic.
    def test_accepts_the_shape_the_cdn_actually_serves(self) -> None:
        # 正規表現そのものを検証するテストなので非公開シンボルに直接触る。
        pattern = fetch_firmware._FILE_FIELD_RE  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(pattern.match("0123456789abcdef" * 2 + ".bin"))

    def test_rejects_traversal_and_url_tricks(self) -> None:
        pattern = fetch_firmware._FILE_FIELD_RE  # pyright: ignore[reportPrivateUsage]
        for bad in ("../etc/passwd", "a/b.bin", "a\r\nHost: evil", "", "x" * 257):
            with self.subTest(bad=bad):
                self.assertIsNone(pattern.match(bad))


if __name__ == "__main__":
    unittest.main()
