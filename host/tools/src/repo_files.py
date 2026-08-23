"""追跡下の .py を挙げる。

自前の除外リストを持たずに済ませるための委譲。`.gitignore` に入っている
`vendor/` も `.venv/` も `tmp/` も、git が知っているぶんだけで正しく落ちる。
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def git_ls_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", "*.py"],
        capture_output=True,
        check=True,
        text=True,
    )
    return [root / name for name in result.stdout.split("\0") if name]
