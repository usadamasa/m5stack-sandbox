# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 MAS Event + Design, LLC
# Copyright 2026 usadamasa
#
# Modified from moremas/build-with-claude: `Range: bytes=N-` resumption in
# `fetch_manifest()` and `download()`. See the docstring.
"""Pull a UIFlow 2.0 firmware binary from M5Burner's manifest API.

The manifest endpoint returns the full catalog; we filter by device
family and flash size, then download the newest UIFlow 2.x release.
Binaries are cached under a per-user XDG cache dir (mode 0700) so
repeated runs don't re-download — and so another local user can't
pre-seed a malicious blob at the predictable cache key.

### Why this lives here

Adapted from the m5-onboard skill's `scripts/fetch_firmware.py` in
moremas/build-with-claude (Apache-2.0, see NOTICE). Upstream reads both
the manifest and the binary in one shot, which fails behind a proxy that
drops long tunnels: `http.client.IncompleteRead` on the ~2.5 MB manifest
and a Content-MD5 mismatch on the ~8.4 MB image, at a byte offset that
moves between runs. The local change is resumption — keep what arrived
and re-ask with `Range: bytes=N-` — in `fetch_manifest()` and
`download()`. Carrying that as an uncommitted diff inside somebody
else's clone lost it on every re-clone, so the file is owned here.

It writes into the same cache directory the skill reads from, so the
usual recovery is to run this first and let the skill's flash step find
the file already cached:

    uv run python host/fetch_firmware.py --device cardputer-adv
"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import hashlib
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from http.client import HTTPResponse, IncompleteRead
from typing import IO, NotRequired, Protocol, TypedDict


class _Digest(Protocol):
    """`hashlib.md5()` の実体。型は private なので、使う口だけ写す。"""

    def update(self, data: bytes, /) -> None: ...


MANIFEST_URL = "https://m5burner-api.m5stack.com/api/firmware"
BINARY_BASE = "https://m5burner.m5stack.com/firmware/"

# Allow-list for the manifest's `file` field, which gets interpolated into
# both a URL and a filesystem path. Everything we've ever seen from
# m5burner-api is 32 hex chars + ".bin", so this is plenty permissive.
# Disallowing slashes, dots-in-isolation, and URL-meaningful chars stops
# path traversal, URL smuggling, and CRLF header injection at the source.
# 256-char cap so a hostile manifest can't ship a multi-megabyte filename.
_FILE_FIELD_RE = re.compile(r"^[A-Za-z0-9._-]{1,256}$")


def _cache_dir() -> str:
    """Per-user firmware cache directory, mode 0700.

    Lives under XDG_CACHE_HOME (or ~/.cache as the fallback) instead of
    the system temp dir. Two reasons:

      1. /tmp on Linux is world-writable with the sticky bit. The cache
         filename is deterministically derived from a public manifest
         field, so before we owned the file, any other local user could
         have pre-seeded /tmp/uiflow2_<key>.bin with malicious bytes,
         which the cache-hit shortcut would then have flashed to the
         device. Per-user 0700 dir closes that vector.
      2. Cache survives reboots, which the system tmp dir does not — so
         repeated provisioning of multiple boards skips re-downloads.

    Created with mode 0700 if missing; tightened to 0700 on every call
    in case it pre-existed at looser perms (chmod is a no-op on
    Windows, which treats the bits as advisory).
    """
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    path = os.path.join(base, "m5-onboard")
    os.makedirs(path, mode=0o700, exist_ok=True)
    # chmod is advisory on Windows and can fail on odd filesystems; the
    # makedirs mode above is the part that matters.
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
    return path


def _open_https(url: str | urllib.request.Request, timeout: float = 30.0) -> HTTPResponse:
    """Open an HTTPS URL with verified TLS.

    There is no unverified fallback. We are flashing firmware to a device
    the user is about to plug into their machine; silently disabling
    cert verification on this path would let any on-path attacker swap
    in arbitrary firmware. If the system trust store is empty (common
    on macOS python.org installs), we try certifi as a second attempt
    and otherwise fail with a clear hint.

    Ladder:
      1. Default context. Works on Homebrew Python / Linux / macOS
         system Python with the OS trust store populated.
      2. certifi bundle if importable. Works if certifi was pulled in
         by any other pip install (very common).
      3. Hard fail with the Install-Certificates hint.
    """

    def _is_cert_error(exc: BaseException) -> bool:
        # urllib wraps the SSL error in URLError; inspect .reason to unwrap.
        if isinstance(exc, ssl.SSLCertVerificationError):
            return True
        return isinstance(exc, urllib.error.URLError) and isinstance(
            exc.reason, ssl.SSLCertVerificationError
        )

    try:
        return urllib.request.urlopen(url, timeout=timeout)
    except Exception as e:
        if not _is_cert_error(e):
            raise
    try:
        import certifi  # pyright: ignore[reportMissingImports]
    except ImportError as e:
        raise SystemExit(
            "TLS verification failed and certifi is not installed.\n"
            "Fix one of:\n"
            "  - macOS python.org install: run "
            "/Applications/Python\\ 3.x/Install\\ Certificates.command\n"
            "  - any platform: pip install --user certifi\n"
            "Refusing to fetch firmware over an unverified connection."
        ) from e
    # Untyped for the checker for the same reason the import is
    # ignored above: certifi is an optional runtime fallback and is not
    # a declared dependency, so there are no stubs to resolve.
    ctx = ssl.create_default_context(
        cafile=certifi.where()  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    )
    return urllib.request.urlopen(url, timeout=timeout, context=ctx)


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
            with _open_https(req, timeout=30) as r:
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


_PRERELEASE = ("rc", "alpha", "beta", "hotfix")


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


# A dropped tunnel is resumed by asking for the tail; this bounds how
# many times we are willing to do that before giving up.
_MAX_ATTEMPTS = 8


def _md5_file(path: str) -> bytes:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.digest()


def _target(version: FirmwareVersion, dest_dir: str) -> tuple[str, str]:
    """Return (url, dest) for a manifest version, or exit.

    Everything the manifest can influence is checked here, before the
    value flows into a URL or a filesystem path.
    """
    file_field = version.get("file")
    if not file_field:
        raise SystemExit(f"Manifest version has no `file` field: {version}")
    # A hostile or buggy manifest cannot make us reach an arbitrary URL,
    # write outside the cache dir, or inject CRLF into the request line.
    if not _FILE_FIELD_RE.match(file_field):
        raise SystemExit(
            f"Manifest `file` field {file_field!r} is not in the allowed "
            f"shape {_FILE_FIELD_RE.pattern}; refusing to use it in a URL "
            "or filesystem path."
        )
    # The `file` field may or may not include a .bin suffix depending
    # on when the entry was added; normalize both sides.
    url = BINARY_BASE + file_field + ("" if file_field.endswith(".bin") else ".bin")
    base = file_field[:-4] if file_field.endswith(".bin") else file_field
    dest = os.path.join(dest_dir, f"uiflow2_{base}.bin")
    # Belt-and-suspenders containment check: if the regex above were ever
    # loosened, this still catches anything that would write outside
    # dest_dir. realpath collapses any "." / ".." / symlink games.
    real_dest = os.path.realpath(dest)
    real_root = os.path.realpath(dest_dir) + os.sep
    if not real_dest.startswith(real_root):
        raise SystemExit(
            f"Refusing to write outside cache dir: dest={real_dest!r} is not under {real_root!r}."
        )
    return url, dest


def _cache_hit(dest: str, sidecar: str) -> bool:
    """Re-hash the cached binary and compare to the sidecar.

    The sidecar lives in a 0700 cache dir, so only this uid could have
    placed it there — an attacker dropping a binary without a matching
    sidecar falls straight through to the cache-miss path, which then
    runs the live Content-MD5 check against the CDN. Any error here
    (missing sidecar, malformed hex, hash mismatch) is a cache miss;
    we never raise from the hit path.
    """
    if not (os.path.exists(dest) and os.path.exists(sidecar)):
        return False
    try:
        with open(sidecar) as f:
            expected = bytes.fromhex(f.read().strip())
        return len(expected) == 16 and _md5_file(dest) == expected
    except (OSError, ValueError):
        return False


def _expected_md5(response: HTTPResponse, url: str) -> bytes:
    """The digest the CDN claims for the object, or exit."""
    expected_b64 = response.headers.get("Content-MD5")
    if not expected_b64:
        raise SystemExit(
            f"CDN response for {url} did not include a "
            "Content-MD5 header; refusing to install "
            "unverifiable firmware."
        )
    try:
        expected = base64.b64decode(expected_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise SystemExit(f"Malformed Content-MD5 header {expected_b64!r}: {e}") from e
    if len(expected) != 16:
        raise SystemExit(f"Content-MD5 wrong length ({len(expected)} bytes, want 16) for {url}")
    return expected


def _read_body(response: HTTPResponse, handle: IO[bytes], digest: _Digest) -> tuple[int, bool]:
    """Drain one response into handle. Returns (bytes read, ran to the end)."""
    got = 0
    try:
        while True:
            chunk = response.read(65536)
            if not chunk:
                return got, True
            digest.update(chunk)
            _ = handle.write(chunk)
            got += len(chunk)
    except IncompleteRead as e:
        if e.partial:
            digest.update(e.partial)
            _ = handle.write(e.partial)
            got += len(e.partial)
        return got, False


def _stream(handle: IO[bytes], url: str) -> bytes:
    """Write the object into handle and return its verified MD5 digest.

    Aliyun OSS sets Content-MD5 (base64'd MD5 of the stored object) on
    every blob response. We stream-hash the body and compare so that a
    storage-layer corruption or manifest/binary drift is caught before
    we hand the bytes to esptool.

    This is integrity-only. MD5 is broken for collision attacks, so it
    is NOT a substitute for TLS — it complements the verified-TLS
    connection enforced by _open_https(). A CDN that can rewrite both
    bytes and headers in tandem is not stopped by this check; pinned
    constants would be needed for that, and M5Stack does not publish
    signed releases to pin against.
    """
    digest = hashlib.md5()
    expected: bytes | None = None
    got = 0
    for _ in range(_MAX_ATTEMPTS):
        # A CONNECT proxy between us and the CDN (Claude Code's sandbox
        # runs one) can drop a long tunnel mid-transfer. That arrives as
        # IncompleteRead, and re-pulling a multi-megabyte image from
        # byte 0 each time rarely converges. Ask for the tail instead.
        # OSS omits Content-MD5 on a 206, so the digest captured from
        # the initial 200 is what the reassembled object is checked
        # against — the integrity check still covers every byte.
        req: str | urllib.request.Request = url
        if got:
            req = urllib.request.Request(url, headers={"Range": f"bytes={got}-"})
        with _open_https(req, timeout=120) as r:
            if got and r.status != 206:
                # Range ignored: the body is the whole object again, so
                # drop what we have and start over.
                _ = handle.seek(0)
                _ = handle.truncate()
                digest = hashlib.md5()
                got = 0
            if expected is None:
                expected = _expected_md5(r, url)
            read, complete = _read_body(r, handle, digest)
        got += read
        if complete:
            break
        sys.stderr.write(f"Download cut short at {got} bytes; resuming.\n")
    else:
        raise SystemExit(
            f"Firmware download from {url} kept getting cut short; "
            f"gave up after {_MAX_ATTEMPTS} attempts ({got} bytes)."
        )

    # ここまで来たなら break 済みで、その反復で `expected` は必ず埋まっている。
    if digest.digest() != expected:
        raise SystemExit(
            f"MD5 mismatch on firmware download from {url}: "
            f"expected {expected.hex()}, got {digest.hexdigest()}. "
            "Aborting; partial file removed."
        )
    return digest.digest()


def _commit(tmp: str, dest: str, sidecar_tmp: str, sidecar: str, hexdigest: str) -> None:
    """Atomic rename.

    The binary appears at its cache key only after verification passes,
    and the sidecar appears only after the binary is in place. A crash
    anywhere in this sequence leaves a recoverable state (no
    half-verified blob, no orphan sidecar pointing at a missing file).
    """
    os.replace(tmp, dest)
    with open(sidecar_tmp, "w") as f:
        _ = f.write(hexdigest + "\n")
    os.replace(sidecar_tmp, sidecar)


def download(entry: ManifestEntry, version: FirmwareVersion, dest_dir: str | None = None) -> str:
    del entry  # kept in the signature so callers read as (what, which).
    if dest_dir is None:
        dest_dir = _cache_dir()
    url, dest = _target(version, dest_dir)
    sidecar = dest + ".md5"
    if _cache_hit(dest, sidecar):
        return dest

    tmp = dest + ".part"
    sidecar_tmp = sidecar + ".part"
    try:
        with open(tmp, "wb") as handle:
            digest = _stream(handle, url)
        _commit(tmp, dest, sidecar_tmp, sidecar, digest.hex())
    except BaseException:
        for path in (tmp, sidecar_tmp):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
        raise
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch UIFlow 2.0 firmware.")
    ap.add_argument(
        "--variant",
        required=True,
        choices=sorted(VARIANTS),
        help="Which device variant to fetch firmware for.",
    )
    ap.add_argument(
        "--dest",
        default=None,
        help=(
            "Cache directory. Default: $XDG_CACHE_HOME/m5-onboard/ "
            "(or ~/.cache/m5-onboard/), created at mode 0700 if missing. "
            "Override only if you know you need a different location — "
            "we don't tighten permissions on a path you name explicitly."
        ),
    )
    args = ap.parse_args()

    manifest = fetch_manifest()
    entry, version = pick_firmware(manifest, args.variant)
    path = download(entry, version, args.dest)
    sys.stderr.write(
        f"Picked: {entry.get('name', '?')} "
        f"version={version.get('version', '?')} "
        f"({version.get('published_at', '?')})\n"
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
