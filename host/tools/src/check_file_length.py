"""行数が増えていくのを止めるラチェット。

しきい値を超えたファイルは baseline に現在の行数で載り、そこから増やせなく
なる。減らしたぶんは `--update` が取り込んで baseline が下がる。上がる方向へ
動かないので、一度縮めた行数は戻せない。baseline に無い新顔がしきい値を
超えたら、その場で落ちる。

baseline に並ぶ行数は、そのままリファクタリングの backlog になる。

    uv run poe lines            # 検査する
    uv run poe lines --update   # 縮んだぶんを baseline へ取り込む

`--update` は baseline を下げるか消すかしかしないので、迷ったら走らせてよい。
新しく超えたファイルを意図して受け入れるときだけ `--adopt` を使う。こちらは
ラチェットを緩めるぶん、差分が PR に出て人の目に触れる。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

# 絶対値そのものより、超えた地点から増やせなくなることの方が効く。
DEFAULT_THRESHOLD = 400

BASELINE_NAME = "file-length-baseline.json"

Kind = Literal["new", "regression", "stale"]

# テストが列挙を差し替えるための口。CLI からは既定しか通らない。
ListFiles = Callable[[Path], "Sequence[Path]"]


@dataclass(frozen=True)
class Finding:
    kind: Kind
    path: str
    lines: int | None  # ファイルが消えているときだけ None
    baseline: int | None  # baseline に載っていないときだけ None
    threshold: int

    def message(self) -> str:
        if self.kind == "new":
            return f"{self.lines} 行。しきい値 {self.threshold} 行を超えた"
        if self.kind == "regression":
            return f"{self.baseline} 行から {self.lines} 行へ増えた"
        if self.lines is None:
            return f"baseline に {self.baseline} 行で残っているが、ファイルが無い"
        return f"baseline は {self.baseline} 行だが、実際は {self.lines} 行"


def count_lines(path: Path) -> int:
    """末尾に改行が無い最終行も 1 行と数える。"""
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def git_ls_files(root: Path) -> list[Path]:
    """追跡下の .py を挙げる。自前の除外リストを持たずに済ませるための委譲。"""
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", "*.py"],
        capture_output=True,
        check=True,
        text=True,
    )
    return [root / name for name in result.stdout.split("\0") if name]


def discover(root: Path, *, list_files: ListFiles = git_ls_files) -> dict[str, int]:
    return {path.relative_to(root).as_posix(): count_lines(path) for path in list_files(root)}


def load_baseline(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    parsed: object = json.loads(path.read_text(encoding="utf-8"))
    entries = cast("Mapping[str, object]", parsed)
    return {name: int(cast("int", value)) for name, value in entries.items()}


def save_baseline(path: Path, entries: Mapping[str, int]) -> None:
    """キー順で書く。並び順だけが変わった差分を PR に出さないため。"""
    payload = json.dumps(dict(sorted(entries.items())), indent=2, ensure_ascii=False)
    _ = path.write_text(payload + "\n", encoding="utf-8")


def next_baseline(
    counts: Mapping[str, int],
    baseline: Mapping[str, int],
    threshold: int,
    *,
    adopt: bool = False,
) -> dict[str, int]:
    """あるべき baseline。

    ここが緩まないことが全体の前提になっている。既存のエントリは `min` を
    通るので上がらず、`adopt` が増やせるのは新規エントリだけ。
    """
    result: dict[str, int] = {}
    for path, current in counts.items():
        if current <= threshold:
            continue
        previous = baseline.get(path)
        if previous is None:
            if adopt:
                result[path] = current
            continue
        result[path] = min(current, previous)
    return result


def classify(
    counts: Mapping[str, int],
    baseline: Mapping[str, int],
    threshold: int,
) -> list[Finding]:
    findings: list[Finding] = []
    updated = next_baseline(counts, baseline, threshold)
    for path in sorted(set(counts) | set(baseline)):
        current = counts.get(path)
        previous = baseline.get(path)
        if previous is None:
            if current is not None and current > threshold:
                findings.append(Finding("new", path, current, None, threshold))
            continue
        if current is not None and current > previous:
            findings.append(Finding("regression", path, current, previous, threshold))
            continue
        if updated.get(path) != previous:
            findings.append(Finding("stale", path, current, previous, threshold))
    return findings


_LABELS: dict[Kind, str] = {
    "new": "長すぎる",
    "regression": "増えた",
    "stale": "baseline が古い",
}


def _report(findings: Sequence[Finding], baseline_name: str) -> None:
    width = max(len(f.path) for f in findings)
    for finding in findings:
        print(f"{finding.path:<{width}}  [{_LABELS[finding.kind]}] {finding.message()}")

    kinds = {finding.kind for finding in findings}
    print()
    if "new" in kinds:
        print("分割して行数を減らすか、意図して受け入れるなら:")
        print("    uv run poe lines --adopt   # ラチェットを緩める。差分を残すこと")
    if "regression" in kinds:
        # `--adopt` でも既存エントリは上がらない。緩めるには手で書く。
        print(f"増えたぶんを戻す。意図して受け入れるなら {baseline_name} を手で書き換える")
    if "stale" in kinds:
        print(f"縮んだぶんを {baseline_name} へ取り込む:")
        print("    uv run poe lines --update")


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
    threshold = cast("int", args.threshold)
    adopt = cast("bool", args.adopt)
    write = cast("bool", args.update) or adopt

    baseline_path = root / BASELINE_NAME
    counts = discover(root, list_files=list_files)
    baseline = load_baseline(baseline_path)

    if write:
        updated = next_baseline(counts, baseline, threshold, adopt=adopt)
        if updated != baseline:
            save_baseline(baseline_path, updated)
            print(f"{BASELINE_NAME} を書き直した ({len(updated)} 件)")
        baseline = updated

    findings = classify(counts, baseline, threshold)
    if not findings:
        return 0
    _report(findings, BASELINE_NAME)
    return 1


if __name__ == "__main__":
    sys.exit(main())
