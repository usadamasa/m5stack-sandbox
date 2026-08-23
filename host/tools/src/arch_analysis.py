"""ソースから形を測る。表に出す係は `arch_metrics.py`。

測るのは 4 つ。関数ごとの循環的複雑度、モジュールの凝集度、コンポーネント
間の結合度 (Ca/Ce/instability)、そして依存の循環。どれも AST と import 文
だけから出るので、追加の依存は無い。

複雑度の**判定**は ruff の C901 が持っている。ここで出す数は観測であって、
mccabe の数え方とは一致しないことがある。ラチェットを掛けるなら ruff の
出力を使うこと。ここの数を二つ目の基準にしない。

抽象度 (A) と main sequence からの距離 (D) は測っていない。A は抽象型の
割合で、flat module が主体のこのツリーでは常に 0 に近く、D が I の写しに
なるだけで何も足さないため。
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

# host/* は src/ を、device は自分自身を import root にしている。
SOURCE_ROOTS = ("src",)


@dataclass(frozen=True)
class FunctionMetric:
    name: str
    line: int
    complexity: int


@dataclass(frozen=True)
class ModuleMetric:
    path: str
    name: str
    lines: int
    functions: int
    classes: int
    max_complexity: int
    # 定義が参照でいくつの塊に分かれるか。1 が一枚岩、0 は定義なし。
    cohesion: int
    raw_imports: frozenset[str]
    function_metrics: tuple[FunctionMetric, ...]


@dataclass(frozen=True)
class ComponentMetric:
    name: str
    files: int
    lines: int
    afferent: int  # Ca: ここに依存しているコンポーネントの数
    efferent: int  # Ce: ここが依存しているコンポーネントの数
    instability: float  # Ce / (Ca + Ce)。1 に近いほど変わりやすい


# ------------------------------------------------------------------ paths


def is_test(path: str) -> bool:
    return "tests/" in path or Path(path).name.startswith("test_")


def component_of(path: str) -> str:
    parts = Path(path).parts
    if parts and parts[0] == "host" and len(parts) > 1:
        return f"host/{parts[1]}"
    return parts[0] if parts else ""


def module_name(path: str) -> str:
    """repo 相対パスから import 名を作る。

    host/* は `src/` が、device は member 自身が import root。デバイスの
    `/flash/` を写した階層がそのまま package 名になる。
    """
    parts = list(Path(path).with_suffix("").parts)
    parts = parts[len(Path(component_of(path)).parts) :]
    if parts and parts[0] in SOURCE_ROOTS:
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def resolve_import(name: str, known: Mapping[str, str]) -> str | None:
    """import 名を内部モジュール名へ。外部ライブラリなら None。"""
    while name:
        if name in known:
            return name
        if "." not in name:
            return None
        name = name.rsplit(".", 1)[0]
    return None


# --------------------------------------------------------------- modules


def _own_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """関数の内側には降りない walk。ネストした関数は別に数えるため。"""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        stack.extend(ast.iter_child_nodes(current))


def _complexity(node: ast.AST) -> int:
    total = 1
    for child in _own_nodes(node):
        if isinstance(
            child, ast.If | ast.IfExp | ast.For | ast.AsyncFor | ast.While | ast.ExceptHandler
        ):
            total += 1
        elif isinstance(child, ast.BoolOp):
            total += len(child.values) - 1
        elif isinstance(child, ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp):
            total += sum(1 + len(gen.ifs) for gen in child.generators)
        elif isinstance(child, ast.match_case):
            total += 1
    return total


def _walk_functions(body: Sequence[ast.stmt], prefix: str) -> Iterator[FunctionMetric]:
    for node in body:
        if isinstance(node, ast.ClassDef):
            yield from _walk_functions(node.body, f"{prefix}{node.name}.")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield FunctionMetric(f"{prefix}{node.name}", node.lineno, _complexity(node))
            # ネストした関数は親の名前を継がない。クラスの prefix は保つ。
            yield from _walk_functions(node.body, prefix)


def _definitions(tree: ast.Module) -> dict[str, ast.stmt]:
    """モジュールレベルの定義。定数も含める — 共有定数は凝集の辺になる。"""
    found: dict[str, ast.stmt] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found[node.target.id] = node
    return found


def _cohesion(tree: ast.Module) -> int:
    """定義を参照で繋いだグラフの連結成分数。LCOM4 を flat module へ。"""
    definitions = _definitions(tree)
    if not definitions:
        return 0

    parent = {name: name for name in definitions}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    for name, node in definitions.items():
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name) and inner.id in definitions and inner.id != name:
                root_a, root_b = find(name), find(inner.id)
                if root_a != root_b:
                    parent[root_b] = root_a
    return len({find(name) for name in definitions})


def _imports(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def analyze_module(path: str, source: str) -> ModuleMetric:
    tree = ast.parse(source)
    functions = tuple(_walk_functions(tree.body, ""))
    return ModuleMetric(
        path=path,
        name=module_name(path),
        lines=len(source.splitlines()),
        functions=len(functions),
        classes=sum(1 for node in ast.walk(tree) if isinstance(node, ast.ClassDef)),
        max_complexity=max((f.complexity for f in functions), default=0),
        cohesion=_cohesion(tree),
        raw_imports=frozenset(_imports(tree)),
        function_metrics=functions,
    )


# ------------------------------------------------------------- aggregate


def module_edges(modules: Sequence[ModuleMetric]) -> dict[str, set[str]]:
    """内部モジュール間の依存。外部ライブラリは落ちる。"""
    known = {module.name: module.path for module in modules}
    edges: dict[str, set[str]] = {}
    for module in modules:
        targets = {resolve_import(name, known) for name in module.raw_imports}
        edges[module.name] = {t for t in targets if t is not None and t != module.name}
    return edges


def component_edges(modules: Sequence[ModuleMetric]) -> dict[str, set[str]]:
    by_name = {module.name: module for module in modules}
    edges: dict[str, set[str]] = {component_of(m.path): set() for m in modules}
    for source, targets in module_edges(modules).items():
        origin = component_of(by_name[source].path)
        for target in targets:
            destination = component_of(by_name[target].path)
            if destination != origin:
                edges[origin].add(destination)
    return edges


def components(modules: Sequence[ModuleMetric]) -> list[ComponentMetric]:
    files: dict[str, int] = {}
    lines: dict[str, int] = {}
    for module in modules:
        component = component_of(module.path)
        files[component] = files.get(component, 0) + 1
        lines[component] = lines.get(component, 0) + module.lines

    outgoing = component_edges(modules)
    incoming: dict[str, set[str]] = {name: set() for name in files}
    for origin, targets in outgoing.items():
        for target in targets:
            incoming[target].add(origin)

    result: list[ComponentMetric] = []
    for name in sorted(files):
        efferent, afferent = len(outgoing[name]), len(incoming[name])
        total = efferent + afferent
        result.append(
            ComponentMetric(
                name=name,
                files=files[name],
                lines=lines[name],
                afferent=afferent,
                efferent=efferent,
                instability=(efferent / total) if total else 0.0,
            )
        )
    return result


def _pop_scc(stack: list[str], on_stack: set[str], root: str) -> list[str]:
    """root まで巻き戻して、1 つの強連結成分を取り出す。"""
    group: list[str] = []
    while True:
        member = stack.pop()
        on_stack.discard(member)
        group.append(member)
        if member == root:
            return group


def find_cycles(graph: Mapping[str, set[str]]) -> list[list[str]]:
    """Tarjan の強連結成分。2 つ以上の頂点を含むものだけ返す。

    自己 import は循環依存の話ではないので、頂点 1 つの成分は落とす。
    """
    edges = {node: sorted(targets & set(graph)) for node, targets in graph.items()}
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    found: list[list[str]] = []

    def strongconnect(node: str) -> None:
        index[node] = low[node] = len(index)
        stack.append(node)
        on_stack.add(node)
        for target in edges[node]:
            if target not in index:
                strongconnect(target)
                low[node] = min(low[node], low[target])
            elif target in on_stack:
                low[node] = min(low[node], index[target])
        if low[node] != index[node]:
            return
        group = _pop_scc(stack, on_stack, node)
        if len(group) > 1:
            found.append(sorted(group))

    for node in sorted(graph):
        if node not in index:
            strongconnect(node)
    return sorted(found)
