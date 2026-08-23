"""関数ごとの複雑度が増えていくのを止めるラチェット。仕掛けは `ratchet.py`。

しきい値 (10) を超えた関数は `complexity-baseline.json` に現在値で載り、
そこから増やせなくなる。減らしたぶんは `--update` が取り込む。

    uv run poe complexity            # 検査する
    uv run poe complexity --update   # 減らしたぶんを baseline へ取り込む

ruff にも C901 があるが、`per-file-ignores` は有効か無効かの二値で、抑えた
ファイルの中では複雑度がいくら増えても黙る。判定をこちらへ移したのはその
ため。ruff 側は引数や文の数 (PLR09xx) を見る係として残っている。

キーは `path::function`。関数の名前が変わると baseline からは消えたように
見えるので、リネームすると stale として一度落ちる。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from arch_analysis import analyze_module
from ratchet import Ratchet
from repo_files import git_ls_files

# ruff の C901 が使っていた既定値と同じ。
DEFAULT_THRESHOLD = 10

BASELINE_NAME = "complexity-baseline.json"

ListFiles = Callable[[Path], "Sequence[Path]"]


def measure(root: Path, *, list_files: ListFiles = git_ls_files) -> dict[str, int]:
    """全関数の循環的複雑度。tests/ も含める — あちらも読めなくなる。"""
    values: dict[str, int] = {}
    for path in list_files(root):
        relative = path.relative_to(root).as_posix()
        module = analyze_module(relative, path.read_text(encoding="utf-8"))
        for function in module.function_metrics:
            values[f"{relative}::{function.name}"] = function.complexity
    return values


def main(argv: Sequence[str] | None = None, *, list_files: ListFiles = git_ls_files) -> int:
    parser = argparse.ArgumentParser(
        description="しきい値を超えた関数の複雑度が増えていないかを見る。"
    )
    _ = parser.add_argument("--root", default=".", help="リポジトリのルート")
    _ = parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    _ = parser.add_argument(
        "--update",
        action="store_true",
        help="減らしたぶんを baseline へ取り込む (下げるだけで、上げない)",
    )
    _ = parser.add_argument(
        "--adopt",
        action="store_true",
        help="新しくしきい値を超えた関数を baseline へ載せる (ラチェットを緩める)",
    )
    args = parser.parse_args(argv)

    root = Path(cast("str", args.root)).resolve()
    ratchet = Ratchet(root / BASELINE_NAME, cast("int", args.threshold), fixer="poe complexity")
    return ratchet.run(
        measure(root, list_files=list_files),
        update=cast("bool", args.update),
        adopt=cast("bool", args.adopt),
    )


if __name__ == "__main__":
    sys.exit(main())
