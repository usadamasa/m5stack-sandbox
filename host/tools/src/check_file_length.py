"""行数が増えていくのを止めるラチェット。仕掛けは `ratchet.py`。

しきい値を超えたファイルは baseline に現在の行数で載り、そこから増やせなく
なる。減らしたぶんは `--update` が取り込んで baseline が下がる。

    uv run poe lines            # 検査する
    uv run poe lines --update   # 縮んだぶんを baseline へ取り込む

baseline に並ぶ行数は、そのままリファクタリングの backlog になる。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from ratchet import Ratchet
from repo_files import git_ls_files

# 絶対値そのものより、超えた地点から増やせなくなることの方が効く。
DEFAULT_THRESHOLD = 400

BASELINE_NAME = "file-length-baseline.json"

# テストが列挙を差し替えるための口。CLI からは既定しか通らない。
ListFiles = Callable[[Path], "Sequence[Path]"]


def count_lines(path: Path) -> int:
    """末尾に改行が無い最終行も 1 行と数える。"""
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def discover(root: Path, *, list_files: ListFiles = git_ls_files) -> dict[str, int]:
    return {path.relative_to(root).as_posix(): count_lines(path) for path in list_files(root)}


def main(argv: Sequence[str] | None = None, *, list_files: ListFiles = git_ls_files) -> int:
    parser = argparse.ArgumentParser(
        description="しきい値を超えたファイルの行数が増えていないかを見る。"
    )
    _ = parser.add_argument("--root", default=".", help="リポジトリのルート")
    _ = parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    _ = parser.add_argument(
        "--update",
        action="store_true",
        help="縮んだぶんを baseline へ取り込む (下げるだけで、上げない)",
    )
    _ = parser.add_argument(
        "--adopt",
        action="store_true",
        help="新しくしきい値を超えたファイルを baseline へ載せる (ラチェットを緩める)",
    )
    args = parser.parse_args(argv)

    root = Path(cast("str", args.root)).resolve()
    ratchet = Ratchet(
        root / BASELINE_NAME, cast("int", args.threshold), unit="行", fixer="poe lines"
    )
    return ratchet.run(
        discover(root, list_files=list_files),
        update=cast("bool", args.update),
        adopt=cast("bool", args.adopt),
    )


if __name__ == "__main__":
    sys.exit(main())
