"""PostgreSQL/SQLAlchemy SQL construction safety guard。

这是一个小而明确的静态 guard：只阻止动态 SQL 结构进入 ``text``、
``execute``、``fetch`` 和 ``executemany`` sink；参数化 SQL 和 SQLAlchemy
expression API 不在禁用范围内。它不是通用 SQL firewall。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


SQL_SINKS = frozenset({"text", "execute", "executemany", "fetch", "fetchrow", "fetchval"})


@dataclass(frozen=True)
class SQLGuardFinding:
    path: str
    line: int
    sink: str
    reason: str


def _dynamic(node: ast.AST, dynamic_names: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in dynamic_names
    if isinstance(node, (ast.JoinedStr, ast.BinOp)):
        return True
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            return True
        return any(_dynamic(arg, dynamic_names) for arg in node.args)
    if isinstance(node, (ast.Tuple, ast.List, ast.Dict, ast.Set)):
        return any(_dynamic(child, dynamic_names) for child in ast.iter_child_nodes(node))
    return False


def scan_source(source: str, *, path: str = "<source>") -> tuple[SQLGuardFinding, ...]:
    tree = ast.parse(source, filename=path)
    dynamic_names: set[str] = set()
    findings: list[SQLGuardFinding] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            if _dynamic(node.value, dynamic_names):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                dynamic_names.update(t.id for t in targets if isinstance(t, ast.Name))
        if not isinstance(node, ast.Call) or not node.args:
            continue
        sink = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
        if sink not in SQL_SINKS:
            continue
        if _dynamic(node.args[0], dynamic_names):
            findings.append(SQLGuardFinding(path, node.lineno, sink, "dynamic SQL structure reached sink"))
    return tuple(findings)


def scan_paths(paths: tuple[Path, ...]) -> tuple[SQLGuardFinding, ...]:
    findings: list[SQLGuardFinding] = []
    for path in paths:
        findings.extend(scan_source(path.read_text(encoding="utf-8"), path=path.as_posix()))
    return tuple(findings)


__all__ = ["SQLGuardFinding", "scan_paths", "scan_source"]
