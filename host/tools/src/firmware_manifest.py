"""M5Burner のカタログを読んで、variant からファームウェアを 1 つ選ぶ。

`fetch_firmware` から切り出した。ここが答えるのは「どのエントリの、どの
バージョンか」までで、バイナリには触らない。

manifest エンドポイントはカタログ全体を返すので、デバイスファミリと
フラッシュ容量で絞ってから、最新の UIFlow 2.x を採る。
"""

from __future__ import annotations

import json
import urllib.request
from http.client import IncompleteRead
from typing import NotRequired, TypedDict

from firmware_http import open_https

MANIFEST_URL = "https://m5burner-api.m5stack.com/api/firmware"


class FirmwareVersion(TypedDict, total=False):
    """M5Burner の manifest エントリが持つバージョン 1 件分の形。

    レスポンスは緩く、どのフィールドも欠けうるので total=False にしてある。
    """

    version: str
    file: str
    published_at: str
    published: bool


class ManifestEntry(TypedDict, total=False):
    """M5Burner の manifest が返すカタログの 1 エントリ (ボードファミリー単位)。

    `file` の値は Aliyun OSS 上の不透明なオブジェクトキーであり、32桁の
    16進数に見えてもコンテンツハッシュではない。整合性はダウンロード時に
    CDN が返す Content-MD5 ヘッダで検証する。
    """

    name: str
    category: str
    tags: list[str]
    versions: list[FirmwareVersion]


class VariantSpec(TypedDict):
    """VARIANTS の値の形。variant 名から manifest 上のエントリを引く鍵。"""

    category: str
    entry_name: str
    version_suffix: str
    version_must_not: NotRequired[tuple[str, ...]]


# Map each supported variant to the exact (category, entry name, version
# suffix) tuple that identifies its firmware in the M5Burner manifest.
# version_suffix is matched against the `version` field of each published
# version — empty string means "any version, pick the latest stable".
VARIANTS: dict[str, VariantSpec] = {
    "basic-16mb": {
        "category": "core",
        "entry_name": "UIFlow2.0",
        "version_suffix": "-16MB",
    },
    "basic-4mb": {
        "category": "core",
        "entry_name": "UIFlow2.0",
        "version_suffix": "-4MB",
    },
    "fire": {
        "category": "core",
        "entry_name": "UIFlow2.0 Fire",
        "version_suffix": "",
    },
    "core2": {
        "category": "core2 & tough",
        "entry_name": "UIFlow2.0",
        # Core2 versions have no suffix; Tough versions end in -TOUGH.
        "version_suffix": "",
        "version_must_not": ("-TOUGH",),
    },
    "tough": {
        "category": "core2 & tough",
        "entry_name": "UIFlow2.0",
        "version_suffix": "-TOUGH",
    },
    "cores3": {
        "category": "cores3",
        "entry_name": "UIFlow2.0",
        "version_suffix": "",
    },
    "cardputer": {
        "category": "cardputer",
        "entry_name": "UIFlow2.0",
        "version_suffix": "",
    },
    "cardputer-adv": {
        "category": "cardputer",
        "entry_name": "UIFlow2.0 Cardputer-Adv",
        "version_suffix": "",
    },
}

_PRERELEASE = ("rc", "alpha", "beta", "hotfix")


def fetch_manifest(max_attempts: int = 6) -> list[ManifestEntry]:
    """Read the catalog, resuming with Range if the connection is cut short.

    The manifest is ~2.5 MB and the endpoint (or an on-path proxy) will
    sometimes close the connection a few KB before Content-Length is
    satisfied. urllib surfaces that as http.client.IncompleteRead, which
    a single r.read() turns into a hard failure. Keep whatever arrived
    and ask for the remaining byte range instead of restarting from 0;
    fall back to a plain re-read if the server ignores Range.
    """
    buf = b""
    last_err: Exception | None = None
    for _ in range(max_attempts):
        req = MANIFEST_URL
        if buf:
            req = urllib.request.Request(MANIFEST_URL, headers={"Range": f"bytes={len(buf)}-"})
        try:
            with open_https(req, timeout=30) as r:
                chunk = r.read()
                if buf and r.status != 206:
                    # Range ignored: the body is the whole document again.
                    buf = b""
                buf += chunk
            return json.loads(buf.decode())
        except IncompleteRead as e:
            if buf and getattr(e, "partial", b""):
                buf += e.partial
            elif not buf:
                buf = e.partial
            last_err = e
        except ValueError as e:
            # Truncated JSON from a resume that stitched badly — start over.
            buf = b""
            last_err = e
    raise RuntimeError(
        f"could not read the M5Burner manifest after {max_attempts} attempts: {last_err}"
    )


def _find_entry(manifest: list[ManifestEntry], spec: VariantSpec) -> ManifestEntry:
    cat = spec["category"].lower()
    name = spec["entry_name"]
    for e in manifest:
        if (e.get("category") or "").lower() == cat and (e.get("name") or "") == name:
            return e
    seen = [e.get("name") for e in manifest if (e.get("category") or "").lower() == cat]
    raise SystemExit(
        f"No manifest entry with category={cat!r} name={name!r}. Seen in category: {seen}"
    )


def _is_stable(version: FirmwareVersion) -> bool:
    tag = (version.get("version") or "").lower()
    return not any(mark in tag for mark in _PRERELEASE)


def _matches(version: FirmwareVersion, suffix: str, must_not: tuple[str, ...]) -> bool:
    """Whether this version is one the variant would accept."""
    if version.get("published") is False:
        return False
    tag = version.get("version") or ""
    if suffix:
        return tag.endswith(suffix)
    return not any(tag.endswith(bad) for bad in must_not)


def _pick_version(entry: ManifestEntry, spec: VariantSpec) -> FirmwareVersion:
    """Pick the newest stable version matching the variant's suffix.

    Stable = version tag without rc/alpha/beta/hotfix. Falls back to
    the newest non-stable if nothing clean matches, so preview/RC
    releases are still flashable when that's all that exists.
    """
    suffix = spec["version_suffix"]
    candidates = [
        v
        for v in entry.get("versions", [])
        if _matches(v, suffix, spec.get("version_must_not", ()))
    ]
    if not candidates:
        raise SystemExit(
            f"No versions for {entry.get('name')!r} match suffix={suffix!r}. "
            f"Available: {[v.get('version') for v in entry.get('versions', [])]}"
        )
    stable = [v for v in candidates if _is_stable(v)]
    # Manifest order is chronological; last = newest.
    return (stable or candidates)[-1]


def pick_firmware(
    manifest: list[ManifestEntry], variant: str
) -> tuple[ManifestEntry, FirmwareVersion]:
    """Return (entry, version) for the chosen variant."""
    if variant not in VARIANTS:
        raise SystemExit(f"Unknown variant '{variant}'. Known: {list(VARIANTS)}")
    spec = VARIANTS[variant]
    entry = _find_entry(manifest, spec)
    version = _pick_version(entry, spec)
    return entry, version
