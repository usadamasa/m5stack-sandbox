"""検証済み TLS でだけ取りに行く口と、per-user のキャッシュ置き場。

`fetch_firmware` から切り出した。manifest を読む側もバイナリを落とす側も
ここを通る。

未検証のフォールバックは無い。これから人が自分の機械に挿すデバイスへ
ファームウェアを焼く経路で、証明書の検証を黙って切れば、経路上の誰でも
任意のファームウェアに差し替えられる。
"""

from __future__ import annotations

import contextlib
import os
import ssl
import urllib.error
import urllib.request
from http.client import HTTPResponse


def cache_dir() -> str:
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


def open_https(url: str | urllib.request.Request, timeout: float = 30.0) -> HTTPResponse:
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
