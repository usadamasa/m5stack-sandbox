"""数値のラチェット。行数にも関数の複雑度にも同じ仕掛けを使う。

しきい値を超えたものは baseline に現在値で載り、そこから増やせなくなる。
減らしたぶんは `--update` が取り込んで baseline が下がる。上げる方向へは
動かないので、一度縮めた値は戻せない。baseline に無い新顔がしきい値を
超えたら、その場で落ちる。

baseline に並ぶ値は、そのままリファクタリングの backlog になる。

`--update` は下げるか消すかしかしないので、迷ったら走らせてよい。新しく
超えたものを意図して受け入れるときだけ `--adopt`。こちらはラチェットを
緩めるぶん、差分が PR に出て人の目に触れる。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

Kind = Literal["new", "regression", "stale"]

_LABELS: dict[Kind, str] = {
    "new": "超えた",
    "regression": "増えた",
    "stale": "baseline が古い",
}


@dataclass(frozen=True)
class Finding:
    kind: Kind
    key: str
    value: int | None  # 消えたキーだけ None
    baseline: int | None  # baseline に載っていないときだけ None
    threshold: int

    def message(self, unit: str = "") -> str:
        def n(value: int | None) -> str:
            return f"{value} {unit}" if unit else str(value)

        if self.kind == "new":
            return f"{n(self.value)}。しきい値 {n(self.threshold)} を超えた"
        if self.kind == "regression":
            return f"{n(self.baseline)} から {n(self.value)} へ増えた"
        if self.value is None:
            return f"baseline に {n(self.baseline)} で残っているが、実体が無い"
        return f"baseline は {n(self.baseline)} だが、実際は {n(self.value)}"


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
    values: Mapping[str, int],
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
    for key, current in values.items():
        if current <= threshold:
            continue
        previous = baseline.get(key)
        if previous is None:
            if adopt:
                result[key] = current
            continue
        result[key] = min(current, previous)
    return result


def classify(
    values: Mapping[str, int],
    baseline: Mapping[str, int],
    threshold: int,
) -> list[Finding]:
    findings: list[Finding] = []
    updated = next_baseline(values, baseline, threshold)
    for key in sorted(set(values) | set(baseline)):
        current = values.get(key)
        previous = baseline.get(key)
        if previous is None:
            if current is not None and current > threshold:
                findings.append(Finding("new", key, current, None, threshold))
            continue
        if current is not None and current > previous:
            findings.append(Finding("regression", key, current, previous, threshold))
            continue
        if updated.get(key) != previous:
            findings.append(Finding("stale", key, current, previous, threshold))
    return findings


def report(findings: Sequence[Finding], baseline_name: str, unit: str, fixer: str) -> None:
    width = max(len(f.key) for f in findings)
    for finding in findings:
        print(f"{finding.key:<{width}}  [{_LABELS[finding.kind]}] {finding.message(unit)}")

    kinds = {finding.kind for finding in findings}
    print()
    if "new" in kinds:
        print("減らすか、意図して受け入れるなら:")
        print(f"    uv run {fixer} --adopt   # ラチェットを緩める。差分を残すこと")
    if "regression" in kinds:
        # `--adopt` でも既存エントリは上がらない。緩めるには手で書く。
        print(f"増えたぶんを戻す。意図して受け入れるなら {baseline_name} を手で書き換える")
    if "stale" in kinds:
        print(f"縮んだぶんを {baseline_name} へ取り込む:")
        print(f"    uv run {fixer} --update")


@dataclass(frozen=True)
class Ratchet:
    """1 本のラチェット。どのファイルに、何を、どの単位で積むか。"""

    baseline_path: Path
    threshold: int
    # 数字に添える単位。行数なら "行"、複雑度のように単位が無ければ空。
    unit: str = ""
    # 直し方として案内するコマンド。"poe lines" など。
    fixer: str = ""

    def run(self, values: Mapping[str, int], *, update: bool = False, adopt: bool = False) -> int:
        baseline = load_baseline(self.baseline_path)

        if update or adopt:
            updated = next_baseline(values, baseline, self.threshold, adopt=adopt)
            if updated != baseline:
                save_baseline(self.baseline_path, updated)
                print(f"{self.baseline_path.name} を書き直した ({len(updated)} 件)")
            baseline = updated

        findings = classify(values, baseline, self.threshold)
        if not findings:
            return 0
        report(findings, self.baseline_path.name, self.unit, self.fixer)
        return 1
