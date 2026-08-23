"""アーキテクチャのメトリクスを並べて見せる。測る係は `arch_analysis.py`。

行数のラチェット (`check_file_length.py`) が止めるのは大きさだけで、形は
見ない。こちらは形を見る — 関数がどれだけ枝分かれしているか、モジュールの
中身が一つの話にまとまっているか、コンポーネントがどちらを向いて依存して
いるか。

    uv run poe metrics          # 表で読む
    uv run poe metrics --json   # 機械で読む

**落ちるのは循環依存があるときだけ。** 残りは数字を出すだけで判定しない。
凝集度が低いモジュールには低いなりの理由がある (CLI の dispatcher は本来
ばらばらだ) し、instability に「正しい値」は無い。並びを worst-first に
してあるので、読む側が上から見れば済む。

tests/ は数えない。device/tests は host/link を import する契約テストで、
数えると device が host/link に依存しているように見えてしまう。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from arch_analysis import (
    ComponentMetric,
    ModuleMetric,
    analyze_module,
    component_edges,
    components,
    find_cycles,
    is_test,
    module_edges,
)
from repo_files import git_ls_files

# 表に出すモジュールの数。全部出すと読まなくなる。
DEFAULT_TOP = 15

# ここを超えた関数だけ名前を挙げる。ruff の C901 と同じ値。
COMPLEXITY_FLOOR = 10

ListFiles = Callable[[Path], "Sequence[Path]"]

Cycles = Mapping[str, list[list[str]]]


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    lines.append("  ".join("-" * w for w in widths))
    lines.extend("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows)
    return "\n".join(lines)


def _component_block(component_metrics: Sequence[ComponentMetric]) -> list[str]:
    rows = [
        [
            c.name,
            str(c.files),
            str(c.lines),
            str(c.afferent),
            str(c.efferent),
            f"{c.instability:.2f}",
        ]
        for c in component_metrics
    ]
    return [
        "## コンポーネント (src のみ)\n",
        _table(["name", "files", "lines", "Ca", "Ce", "I"], rows),
    ]


def _module_block(modules: Sequence[ModuleMetric], top: int) -> list[str]:
    worst = sorted(modules, key=lambda m: (-m.max_complexity, -m.cohesion, -m.lines))[:top]
    rows = [
        [
            m.path,
            str(m.lines),
            str(m.functions),
            str(m.classes),
            str(m.max_complexity),
            str(m.cohesion),
        ]
        for m in worst
    ]
    return [
        f"\n## モジュール (worst-first, 上位 {len(worst)} 件)\n",
        _table(["path", "lines", "def", "class", "maxCC", "凝集"], rows),
    ]


def _function_block(modules: Sequence[ModuleMetric]) -> list[str]:
    flagged = sorted(
        (
            (module, function)
            for module in modules
            for function in module.function_metrics
            if function.complexity > COMPLEXITY_FLOOR
        ),
        key=lambda pair: -pair[1].complexity,
    )
    if not flagged:
        return []
    rows = [
        [f"{module.path}:{function.line}", function.name, str(function.complexity)]
        for module, function in flagged
    ]
    return [
        f"\n## 複雑度が {COMPLEXITY_FLOOR} を超えた関数\n",
        _table(["location", "function", "CC"], rows),
    ]


def render(
    modules: Sequence[ModuleMetric],
    component_metrics: Sequence[ComponentMetric],
    cycles: Cycles,
    top: int,
) -> str:
    blocks = [
        *_component_block(component_metrics),
        *_module_block(modules, top),
        *_function_block(modules),
    ]
    for label, groups in cycles.items():
        if groups:
            blocks.append(f"\n## {label}の循環依存\n")
            blocks.extend("  " + " -> ".join([*group, group[0]]) for group in groups)
    return "\n".join(blocks) + "\n"


def as_json(
    modules: Sequence[ModuleMetric],
    component_metrics: Sequence[ComponentMetric],
    cycles: Cycles,
) -> str:
    payload: dict[str, Any] = {
        "components": [
            {
                "name": c.name,
                "files": c.files,
                "lines": c.lines,
                "afferent": c.afferent,
                "efferent": c.efferent,
                "instability": round(c.instability, 3),
            }
            for c in component_metrics
        ],
        "modules": [
            {
                "path": m.path,
                "name": m.name,
                "lines": m.lines,
                "functions": m.functions,
                "classes": m.classes,
                "max_complexity": m.max_complexity,
                "cohesion": m.cohesion,
            }
            for m in modules
        ],
        "cycles": dict(cycles),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main(argv: Sequence[str] | None = None, *, list_files: ListFiles = git_ls_files) -> int:
    parser = argparse.ArgumentParser(description="アーキテクチャのメトリクスを測る。")
    _ = parser.add_argument("--root", default=".")
    _ = parser.add_argument("--json", action="store_true", help="JSON で出す")
    _ = parser.add_argument("--top", type=int, default=DEFAULT_TOP, help="表に出すモジュール数")
    args = parser.parse_args(argv)

    root = Path(str(args.root)).resolve()
    modules: list[ModuleMetric] = []
    for path in list_files(root):
        relative = path.relative_to(root).as_posix()
        if not is_test(relative):
            modules.append(analyze_module(relative, path.read_text(encoding="utf-8")))

    component_metrics = components(modules)
    cycles: Cycles = {
        "モジュール": find_cycles(module_edges(modules)),
        "コンポーネント": find_cycles(component_edges(modules)),
    }

    if bool(args.json):
        print(as_json(modules, component_metrics, cycles))
    else:
        print(render(modules, component_metrics, cycles, int(args.top)), end="")

    return 1 if any(cycles.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
