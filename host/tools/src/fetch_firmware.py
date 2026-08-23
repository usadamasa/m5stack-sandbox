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

### 分かれ方

依存は下から上への一方向で、ここが一番上にいる。

    firmware_http.py      検証済み TLS の口と、キャッシュ置き場
    firmware_manifest.py  カタログを読んで variant から 1 つ選ぶ
    fetch_firmware.py     バイナリを落とし、検証し、置く + CLI

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
import os
import re
import sys
import urllib.request
from http.client import HTTPResponse, IncompleteRead
from typing import IO, Protocol

from firmware_http import cache_dir, open_https
from firmware_manifest import (
    VARIANTS,
    FirmwareVersion,
    ManifestEntry,
    fetch_manifest,
    pick_firmware,
)


class _Digest(Protocol):
    """`hashlib.md5()` の実体。型は private なので、使う口だけ写す。"""

    def update(self, data: bytes, /) -> None: ...


BINARY_BASE = "https://m5burner.m5stack.com/firmware/"

# Allow-list for the manifest's `file` field, which gets interpolated into
# both a URL and a filesystem path. Everything we've ever seen from
# m5burner-api is 32 hex chars + ".bin", so this is plenty permissive.
# Disallowing slashes, dots-in-isolation, and URL-meaningful chars stops
# path traversal, URL smuggling, and CRLF header injection at the source.
# 256-char cap so a hostile manifest can't ship a multi-megabyte filename.
_FILE_FIELD_RE = re.compile(r"^[A-Za-z0-9._-]{1,256}$")

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
    connection enforced by open_https(). A CDN that can rewrite both
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
        with open_https(req, timeout=120) as r:
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
        dest_dir = cache_dir()
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
