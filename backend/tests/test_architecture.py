"""依赖方向（SPEC §3）：入口层 → 任务层 → 核心，下层不反向依赖上层；模块之间没有环，
也不许用函数内 import 把环藏起来。只读源码（ast），不导入任何模块。"""

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "orbit8d"
ROOT = PACKAGE.name

# 每个单元（一级子包，或包根下的单个模块；包根的 __init__ 记作 orbit8d）允许依赖的单元。
# 没列在这里的依赖一律不许出现；新增单元或新增依赖方向时要改这张表，等于明确做一次决定。
ALLOWED: dict[str, frozenset[str]] = {
    ROOT: frozenset(),
    "config": frozenset(),
    "errors": frozenset(),
    "logging_setup": frozenset(),
    "download": frozenset({"errors"}),
    "engine": frozenset(),
    "media": frozenset({"engine", "errors"}),
    "separate": frozenset({"download", "errors"}),
    "assets": frozenset({"config", "download", "engine", "separate"}),
    "jobs": frozenset({"config", "engine", "errors", "logging_setup", "media"}),
    "api": frozenset({ROOT, "assets", "config", "engine", "jobs", "media"}),
    "__main__": frozenset({"api", "config", "logging_setup", "separate"}),
}


def module_name(path: Path) -> str:
    parts = path.relative_to(PACKAGE.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def unit_of(module: str) -> str:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 else ROOT


MODULES = {module_name(path): path for path in sorted(PACKAGE.rglob("*.py"))}


def absolute(node: ast.ImportFrom, path: Path) -> str:
    """from ... import 的模块名（相对 import 换算成绝对的）。"""
    if not node.level:
        return node.module or ""
    package = module_name(path).split(".")
    if path.name != "__init__.py":
        package = package[:-1]
    anchor = package[: len(package) - (node.level - 1)]
    return ".".join([*anchor, *([node.module] if node.module else [])])


def internal_imports(path: Path) -> list[tuple[str, int, bool]]:
    """这个文件 import 的项目内模块：(模块名, 行号, 是否写在函数或类的里面)。"""
    found: list[tuple[str, int, bool]] = []

    def visit(node: ast.AST, nested: bool) -> None:
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = absolute(node, path)
            # from 包 import 子模块 也算依赖那个子模块
            names = [f"{base}.{a.name}" if f"{base}.{a.name}" in MODULES else base for a in node.names]
        found.extend((n, node.lineno, nested) for n in names if n == ROOT or n.startswith(f"{ROOT}."))
        inside = nested or isinstance(
            node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef
        )
        for child in ast.iter_child_nodes(node):
            visit(child, inside)

    visit(ast.parse(path.read_text(encoding="utf-8")), False)
    return found


def test_every_unit_is_listed_and_the_layering_itself_has_no_cycle():
    assert {unit_of(m) for m in MODULES} == set(ALLOWED)
    resolved: set[str] = set()
    while len(resolved) < len(ALLOWED):
        ready = {unit for unit, deps in ALLOWED.items() if unit not in resolved and deps <= resolved}
        assert ready, f"ALLOWED 里有环：{sorted(set(ALLOWED) - resolved)}"
        resolved |= ready


def test_units_only_depend_on_what_the_layering_allows():
    offending = []
    for module, path in MODULES.items():
        for target, line, _ in internal_imports(path):
            source, dest = unit_of(module), unit_of(target)
            if source != dest and dest not in ALLOWED[source]:
                offending.append(f"{path.relative_to(PACKAGE.parent)}:{line} {source} -> {dest} ({target})")
    assert offending == []


def test_internal_imports_are_at_module_level():
    """函数内 import 项目自己的模块，是用来绕开循环依赖的手法：不许。（第三方重依赖的延迟加载不在此列。）"""
    hidden = [
        f"{path.relative_to(PACKAGE.parent)}:{line} imports {target} inside a function"
        for path in MODULES.values()
        for target, line, nested in internal_imports(path)
        if nested
    ]
    assert hidden == []


def test_modules_have_no_import_cycles():
    graph = {
        module: sorted(
            {target for target, _, _ in internal_imports(path) if target in MODULES and target != module}
        )
        for module, path in MODULES.items()
    }
    done: set[str] = set()

    def visit(module: str, trail: tuple[str, ...]) -> list[str] | None:
        if module in trail:
            return [*trail[trail.index(module) :], module]
        if module in done:
            return None
        for target in graph[module]:
            cycle = visit(target, (*trail, module))
            if cycle:
                return cycle
        done.add(module)
        return None

    cycles = [cycle for module in graph if (cycle := visit(module, ()))]
    assert cycles == []


def test_the_api_layer_never_takes_the_store_lock():
    """“查状态 + 改记录 + 排任务”的互斥在 jobs/songs.py 里做（SPEC §6.1）；入口层自己不拿存储锁。"""
    touching = [
        str(path.relative_to(PACKAGE.parent))
        for module, path in MODULES.items()
        if unit_of(module) == "api"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Attribute) and node.attr in ("lock", "_lock")
    ]
    assert touching == []
