"""WP3-D：生产 SQLite statement authority 的 AST 静态回归守卫。"""

from __future__ import annotations

import ast
import hashlib
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SQLITE_OWNERS = frozenset(
    {
        "core/advanced_memory.py",
        "core/memory_manager.py",
        "core/persistence_migration.py",
        "core/runtime/event_journal_store.py",
        "core/runtime/event_consumer.py",
        "core/runtime/snapshot_store.py",
    }
)
PRODUCTION_ROOTS = (
    ROOT / "main.py",
    ROOT / "server.py",
    ROOT / "core",
    ROOT / "tools",
    ROOT / "ui",
    ROOT / "scripts",
)
SQL_METHODS = frozenset({"execute", "executemany", "executescript"})
EXPECTED_RUNTIME_SQL_SINKS = 57
EXPECTED_STARTUP_ADMIN_SQL_SINKS = 91
AUDITED_SQLITE_CONNECTION_FACTORIES = frozenset(
    {"core.persistence_migration.open_read_only"}
)
SQL_PREFIXES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "CREATE",
    "DROP",
    "ALTER",
    "PRAGMA",
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
    "WITH",
)
SQLITE_SQL_WHITESPACE = (" ", "\t", "\r", "\n", "\f")
_AUDITED_BUSINESS_EXECUTE_CALLS = Counter(
    {
        ("core/agent_router.py", "AgentRouter._execute_knowledge_retrieval", "self.retrieval_execution_service", "execute", "invocation"): 1,
        ("core/chat_service.py", "ChatService._stream_factory_coordinated_events.produce", "scope", "execute", "<missing>"): 1,
        ("core/runtime/agent_loop.py", "AgentLoop.run_stream", "driver", "execute", "action"): 1,
        ("core/runtime/multi_agent_driver.py", "MultiAgentDriver.execute", "adapter", "execute", "request"): 2,
        ("core/runtime/parallel_execution.py", "ParallelExecutor.execute_ready", "self", "execute", "<missing>"): 1,
        ("core/runtime/parallel_execution.py", "ParallelExecutor._invoke", "driver", "execute", "claim"): 2,
        ("core/runtime/runtime_factory.py", "CoordinatedRunScope.execute", "self.coordinator", "execute", "<missing>"): 1,
        ("core/runtime/tool_adapters.py", "ComplexWorkflowToolAdapter.invoke_once", "ComplexWorkflowSimulationTool(**kwargs)", "execute", "request"): 1,
        ("core/runtime/tool_execution.py", "ToolExecutionService.execute_sync", "self", "execute", "<missing>"): 1,
        ("core/runtime/tool_execution.py", "ToolExecutionService._execute_impl.attempt", "self.attempt_executor", "execute", "<missing>"): 1,
        ("tools/complex_workflow_simulator.py", "complex_workflow_simulator", "_LEGACY_TOOL", "execute", "request"): 1,
    }
)
_AUDITED_BUSINESS_RECEIVERS = frozenset(key[:4] for key in _AUDITED_BUSINESS_EXECUTE_CALLS)


@dataclass(frozen=True)
class GuardFinding:
    path: str
    line: int
    code: str
    detail: str


@dataclass
class GuardResult:
    owners: set[str] = field(default_factory=set)
    sink_count: int = 0
    executescript_count: int = 0
    executemany_count: int = 0
    shadowed_sink_count: int = 0
    unknown_receiver_count: int = 0
    business_execute_count: int = 0
    audited_exceptions: list[str] = field(default_factory=list)
    findings: list[GuardFinding] = field(default_factory=list)

    def fail(self, path: str, node: ast.AST, code: str, detail: str) -> None:
        self.findings.append(
            GuardFinding(path, getattr(node, "lineno", 0), code, detail)
        )


def _production_sources() -> dict[str, str]:
    result: dict[str, str] = {}
    for root in PRODUCTION_ROOTS:
        if root.is_file():
            result[root.relative_to(ROOT).as_posix()] = root.read_text(encoding="utf-8")
            continue
        for path in sorted(root.rglob("*.py")):
            if any(part in {".venv", "venv", "__pycache__"} for part in path.parts):
                continue
            result[path.relative_to(ROOT).as_posix()] = path.read_text(encoding="utf-8")
    return result


def _annotation_text(annotation: ast.AST | None) -> str:
    return ast.unparse(annotation) if annotation is not None else ""


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _string_value(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and not any(
        isinstance(value, ast.FormattedValue) for value in node.values
    ):
        return "".join(
            str(value.value)
            for value in node.values
            if isinstance(value, ast.Constant)
        )
    return None


def _formatted_expressions(node: ast.JoinedStr) -> list[ast.AST]:
    return [
        value.value for value in node.values if isinstance(value, ast.FormattedValue)
    ]


def _qualified_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    names: list[str] = []
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(current.name)
        current = parents.get(current)
    return ".".join(reversed(names)) or "<module>"


def _enclosing_function(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
        current = parents.get(current)
    return None


def _imports_sqlite(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.Import) and any(alias.name == "sqlite3" for alias in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            return True
    return False


def _immutable_module_strings(tree: ast.Module) -> dict[str, str]:
    assignments: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            targets: list[ast.AST] = []
            value: ast.AST | None = None
            if isinstance(node, ast.Assign):
                targets, value = list(node.targets), node.value
            elif isinstance(node, ast.AnnAssign):
                targets, value = [node.target], node.value
            elif isinstance(node, ast.AugAssign):
                targets, value = [node.target], node.value
            else:
                targets, value = [node.target], node.value
            for target in targets:
                if isinstance(target, ast.Name):
                    assignments.setdefault(target.id, []).append(value or node)
    result: dict[str, str] = {}
    for name, values in assignments.items():
        if len(values) == 1 and (text := _string_value(values[0])) is not None:
            result[name] = text
    return result


def _imported_sqlite_factory_aliases(
    tree: ast.Module,
    *,
    path: str = "<synthetic>",
    result: GuardResult | None = None,
) -> set[str]:
    """解析当前audited factory的import及最多两跳模块内local alias。"""
    references: set[str] = set()
    direct_name_references: set[str] = set()
    module_aliases: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            for imported in node.names:
                qualified = f"{node.module}.{imported.name}"
                if qualified in AUDITED_SQLITE_CONNECTION_FACTORIES:
                    local = imported.asname or imported.name
                    references.add(local)
                    direct_name_references.add(local)
        elif isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name != "core.persistence_migration":
                    continue
                local_module = imported.asname or imported.name
                module_aliases.add(local_module)
                references.add(f"{local_module}.open_read_only")

    # Frozen bounded propagation: direct reference plus at most two Name aliases.
    alias_assignments: set[ast.Assign | ast.AnnAssign] = set()
    for _ in range(2):
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if _call_name(node.value) not in references:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in references:
                    references.add(target.id)
                    direct_name_references.add(target.id)
                    changed = True
                    alias_assignments.add(node)
        if not changed:
            break

    if result is not None:
        parents = {
            child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
        }
        for node in ast.walk(tree):
            reference = ""
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                reference = node.id if node.id in direct_name_references else ""
                if node.id in module_aliases and isinstance(parents.get(node), ast.Attribute):
                    continue
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                rendered = ast.unparse(node)
                reference = rendered if rendered in references else ""
            if not reference:
                continue
            parent = parents.get(node)
            if isinstance(parent, ast.Call) and parent.func is node:
                continue
            if isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is node:
                if parent in alias_assignments or any(
                    isinstance(target, ast.Name) and target.id in references
                    for target in (
                        parent.targets if isinstance(parent, ast.Assign) else [parent.target]
                    )
                ):
                    continue
            result.fail(
                path,
                node,
                "SQLITE_FACTORY_REFERENCE_ESCAPE",
                f"audited SQLite factory reference escaped: {reference}",
            )
    return references


def _assigned_statement_expressions(scope: ast.AST) -> dict[str, ast.AST]:
    assignments: dict[str, list[ast.AST]] = {}
    nodes: list[ast.AST] = []

    def visit(node: ast.AST) -> None:
        nodes.append(node)
        for child in ast.iter_child_nodes(node):
            if child is not scope and isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
            ):
                continue
            visit(child)

    visit(scope)
    for node in nodes:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                assignments.setdefault(target.id, []).append(node.value)
    return {name: values[0] for name, values in assignments.items() if len(values) == 1}


def _statement_static_text(
    node: ast.AST,
    constants: dict[str, str],
    assigned: dict[str, ast.AST] | None = None,
    seen: frozenset[str] = frozenset(),
) -> str | None:
    if (text := _string_value(node)) is not None:
        return text
    if isinstance(node, ast.Name):
        if node.id in constants:
            return constants[node.id]
        if assigned is not None and node.id in assigned and node.id not in seen:
            return _statement_static_text(
                assigned[node.id], constants, assigned, seen | {node.id}
            )
        return None
    if isinstance(node, ast.JoinedStr):
        return "".join(
            str(value.value)
            for value in node.values
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
        )
    if isinstance(node, ast.BinOp):
        return _statement_static_text(node.left, constants, assigned, seen)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    ):
        return _statement_static_text(node.func.value, constants, assigned, seen)
    return None


def _statement_is_sql_shaped(
    node: ast.AST,
    constants: dict[str, str],
    assigned: dict[str, ast.AST] | None = None,
) -> bool:
    text = _statement_static_text(node, constants, assigned)
    if text is None:
        return False
    normalized = text.lstrip("".join(SQLITE_SQL_WHITESPACE)).upper()
    return any(
        normalized == prefix
        or (
            normalized.startswith(prefix)
            and len(normalized) > len(prefix)
            and normalized[len(prefix)] in SQLITE_SQL_WHITESPACE
        )
        for prefix in SQL_PREFIXES
    )


def _dynamic_statement_finding_code(statement: ast.AST) -> str:
    if isinstance(statement, ast.JoinedStr):
        return "INTERPOLATED_FSTRING"
    if isinstance(statement, ast.BinOp) and isinstance(statement.op, ast.Add):
        return "STRING_CONCATENATION"
    if isinstance(statement, ast.BinOp) and isinstance(statement.op, ast.Mod):
        return "PERCENT_FORMATTING"
    if (
        isinstance(statement, ast.Call)
        and isinstance(statement.func, ast.Attribute)
        and statement.func.attr == "format"
    ):
        return "FORMAT_CALL"
    return "UNRESOLVED_STATEMENT"


def _factory_bound_receivers(
    tree: ast.Module, audited_factories: set[str]
) -> tuple[set[str], dict[str, ast.Call]]:
    audited: set[str] = set()
    unknown: dict[str, ast.Call] = {}

    def record(target: ast.AST, call: ast.Call) -> None:
        if not isinstance(target, ast.Name):
            return
        if _call_name(call.func) in audited_factories:
            audited.add(target.id)
        else:
            unknown[target.id] = call

    for node in ast.walk(tree):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None and isinstance(item.context_expr, ast.Call):
                    record(item.optional_vars, item.context_expr)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                record(target, node.value)
    return audited, unknown


def _known_sql_receivers(
    tree: ast.Module, imported_factory_aliases: set[str]
) -> tuple[set[str], set[str]]:
    names: set[str] = set()
    attributes: set[str] = set()

    def record_target(target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            attributes.add(ast.unparse(target))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                if "Connection" in _annotation_text(argument.annotation):
                    names.add(argument.arg)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if isinstance(value, ast.Call):
                called = _call_name(value.func)
                if called.endswith(".connect") or called in {
                    "connect",
                } | imported_factory_aliases:
                    for target in targets:
                        record_target(target)
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is None or not isinstance(item.context_expr, ast.Call):
                    continue
                called = _call_name(item.context_expr.func)
                if (
                    called.endswith("._connect")
                    or called.endswith("._transaction")
                    or called in imported_factory_aliases
                ):
                    record_target(item.optional_vars)

    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Attribute):
                continue
            if value.func.attr != "cursor" or not _receiver_known(value.func.value, names, attributes):
                continue
            before = (len(names), len(attributes))
            for target in targets:
                record_target(target)
            changed = changed or before != (len(names), len(attributes))
    return names, attributes


def _receiver_known(receiver: ast.AST, names: set[str], attributes: set[str]) -> bool:
    if isinstance(receiver, ast.Name):
        return receiver.id in names
    if isinstance(receiver, ast.Attribute):
        return ast.unparse(receiver) in attributes
    return False


def _semantic_digest(node: ast.AST) -> str:
    semantic = ast.dump(node, include_attributes=False)
    return hashlib.sha256(semantic.encode("utf-8")).hexdigest()


def _is_descendant_of_statements(node: ast.AST, statements: list[ast.stmt]) -> bool:
    return any(node is child for statement in statements for child in ast.walk(statement))


def _fail_closed_handler_for_sink(
    call: ast.Call, parents: dict[ast.AST, ast.AST]
) -> ast.ExceptHandler | None:
    current: ast.AST | None = call
    while current is not None:
        if isinstance(current, ast.Try) and _is_descendant_of_statements(call, current.body):
            for handler in current.handlers:
                if handler.type is None or ast.unparse(handler.type) != "sqlite3.Error":
                    continue
                if len(handler.body) != 1:
                    return None
                action = handler.body[0]
                if isinstance(action, ast.Raise):
                    return handler
                if not isinstance(action, ast.Return):
                    return None
                value = action.value
                if value is None or (
                    isinstance(value, ast.Constant) and value.value in {None, False}
                ):
                    return handler
                if isinstance(value, (ast.Tuple, ast.List)) and not value.elts:
                    return handler
                if isinstance(value, ast.Dict) and not value.keys:
                    return handler
                if (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id == "frozenset"
                    and not value.args
                    and not value.keywords
                ):
                    return handler
                return None
        current = parents.get(current)
    return None


# 冻结的是当前已审计 helper/caller 的 semantic AST，不是函数名白名单。
# 合法生产变更必须显式更新此 manifest 并重新安全审计。
_PRAGMA_FUNCTION_DIGESTS: dict[tuple[str, str], frozenset[str]] = {
    ("core/memory_manager.py", "_table_columns_match"): frozenset(
        {"d3642dc3c622dd98b36f38e724063e4406b0416c7f5c8eff89d031e552ff4dfb"}
    ),
    ("core/memory_manager.py", "_index_definition"): frozenset(
        {"fdbe16ded3229f984efede28852e67a6606cdaf3b98ffe8c89efe552d1e68eda"}
    ),
    ("core/memory_manager.py", "_unique_constraint_set"): frozenset(
        {"07adbb9cbda8b0db44b3b829ba1b9ea4541463ae14dd5da35f0d2b5c92180b96"}
    ),
    ("core/memory_manager.py", "_unique_named_indexes"): frozenset(
        {"48f249e7ebb2349ff79e3b40a3fbba7f58db038549b2875912b44ef36865c59b"}
    ),
    ("core/runtime/event_journal_store.py", "_journal_index_columns"): frozenset(
        {"25887b47fb56be53652581a5eb10433808bb50dde3bfc06f016327a90f2fd183"}
    ),
    ("core/runtime/snapshot_store.py", "_snapshot_shape_current"): frozenset(
        {
            "308b9eff188988ef42e431140fd86e1c78ce79c9a07d896c3d81f349d0a653b3",
            "ad2e396164e028d3570fda6224766791585d1efd1866016764bb1892688c096d",
        }
    ),
    ("core/runtime/snapshot_store.py", "_snapshot_unique_constraint_set"): frozenset(
        {"b9cc6726f92bea90cb8e2105200fea8f1fa3f7aa2fa63f5621a77fb61f84c3d9"}
    ),
}

_PRAGMA_PROVENANCE_DIGESTS: dict[tuple[str, str], str] = {
    ("core/memory_manager.py", "_index_matches"): "77f5e579c85be1a3998c8bc3bb3bd07bf4c27bd3341baed1a0317f0f8ddf23ed",
    ("core/memory_manager.py", "_memory_current_signature_holds"): "bbd26a9fc970bc7802f1e8d4fa4ea236fc8326efbe45a1dc81232e5f34f854b3",
    ("core/memory_manager.py", "_memory_legacy_signature_holds"): "b52507cd07e4f649e666a4e12aefe8dfb759c2238fe98bff4590686a47e765cc",
    ("core/runtime/event_journal_store.py", "_journal_pk_matches"): "a031cd2d25768aa0bb87985781e284e56f79433569125a94374fe40b1b563241",
    ("core/runtime/event_journal_store.py", "_journal_event_id_unique_matches"): "166594664e4110e399ff5562e17c53b84e1fbf52e14093c2eb68cd722a01d4f4",
    ("core/runtime/event_journal_store.py", "_journal_run_type_index_matches"): "b78c46abbd6ba3c8bb37f6d39c7e72c5486211ecc26c0a860dbcb65027ac9ee1",
    ("core/runtime/event_journal_store.py", "_journal_unique_constraint_set"): "4ac313feb9c00810cefdbe7d6bf628fd68bcb76e78a32d5d01649acab2c2df08",
    ("core/runtime/snapshot_store.py", "snapshot_preflight"): "fb26b1c6f358077a24d94e6bf3fbc6047ace520a1adcb759b9ae39de4472170f",
}

_PRAGMA_IDENTIFIER_EXPRESSIONS = {
    ("core/memory_manager.py", "_table_columns_match", "PRAGMA table_info("): {"table"},
    ("core/memory_manager.py", "_index_definition", "PRAGMA index_list("): {"table"},
    ("core/memory_manager.py", "_index_definition", "PRAGMA index_info("): {"index_name"},
    ("core/memory_manager.py", "_unique_constraint_set", "PRAGMA index_list("): {"table"},
    ("core/memory_manager.py", "_unique_constraint_set", "PRAGMA index_info("): {"row[1]"},
    ("core/memory_manager.py", "_unique_named_indexes", "PRAGMA index_list("): {"table"},
    ("core/runtime/event_journal_store.py", "_journal_index_columns", "PRAGMA index_info("): {"index_name"},
    ("core/runtime/snapshot_store.py", "_snapshot_shape_current", "PRAGMA index_info("): {"index_name"},
    ("core/runtime/snapshot_store.py", "_snapshot_unique_constraint_set", "PRAGMA index_info("): {"row[1]"},
}

_PRAGMA_ALLOWED_REFERENCES: dict[str, Counter[tuple[str, str, str]]] = {
    "core/memory_manager.py": Counter(
        {
            ("_index_definition", "_index_matches", "_index_definition(conn, table, index_name)"): 1,
            ("_index_matches", "_memory_v1_core_holds", "_index_matches(conn, table, name, unique=unique, partial=partial, columns=columns, predicate=predicate)"): 1,
            ("_index_matches", "_memory_legacy_signature_holds", "_index_matches(conn, 'messages', name, unique=unique, partial=partial, columns=columns)"): 1,
            ("_index_matches", "_memory_current_signature_holds", "_index_matches(conn, 'long_term_memory', name, unique=unique, partial=partial, columns=columns)"): 1,
            ("_table_columns_match", "_memory_v1_core_holds", "_table_columns_match(conn, 'messages', _MESSAGES_CURRENT_COLUMNS)"): 1,
            ("_table_columns_match", "_memory_v1_core_holds", "_table_columns_match(conn, 'conversation_summaries', _SUMMARY_COLUMNS)"): 1,
            ("_table_columns_match", "_memory_v1_core_holds", "_table_columns_match(conn, 'message_exchanges', _EXCHANGES_COLUMNS)"): 1,
            ("_table_columns_match", "_memory_current_signature_holds", "_table_columns_match(conn, 'long_term_memory', _LONG_TERM_MEMORY_COLUMNS)"): 1,
            ("_table_columns_match", "_memory_legacy_signature_holds", "_table_columns_match(conn, 'messages', _MESSAGES_LEGACY_COLUMNS)"): 1,
            ("_table_columns_match", "_memory_legacy_signature_holds", "_table_columns_match(conn, 'conversation_summaries', _SUMMARY_COLUMNS)"): 1,
            ("_unique_constraint_set", "_memory_v1_core_holds", "_unique_constraint_set(conn, 'messages')"): 1,
            ("_unique_constraint_set", "_memory_v1_core_holds", "_unique_constraint_set(conn, 'conversation_summaries')"): 1,
            ("_unique_constraint_set", "_memory_v1_core_holds", "_unique_constraint_set(conn, 'message_exchanges')"): 1,
            ("_unique_constraint_set", "_memory_current_signature_holds", "_unique_constraint_set(conn, 'long_term_memory')"): 1,
            ("_unique_constraint_set", "_memory_legacy_signature_holds", "_unique_constraint_set(conn, 'messages')"): 1,
            ("_unique_constraint_set", "_memory_legacy_signature_holds", "_unique_constraint_set(conn, 'conversation_summaries')"): 1,
            ("_unique_named_indexes", "_memory_v1_core_holds", "_unique_named_indexes(conn, 'messages')"): 1,
            ("_unique_named_indexes", "_memory_v1_core_holds", "_unique_named_indexes(conn, 'conversation_summaries')"): 1,
            ("_unique_named_indexes", "_memory_v1_core_holds", "_unique_named_indexes(conn, 'message_exchanges')"): 1,
            ("_unique_named_indexes", "_memory_current_signature_holds", "_unique_named_indexes(conn, 'long_term_memory')"): 1,
            ("_unique_named_indexes", "_memory_legacy_signature_holds", "_unique_named_indexes(conn, 'messages')"): 1,
            ("_unique_named_indexes", "_memory_legacy_signature_holds", "_unique_named_indexes(conn, 'conversation_summaries')"): 1,
        }
    ),
    "core/runtime/event_journal_store.py": Counter(
        {
            ("_journal_index_columns", "_journal_pk_matches", "_journal_index_columns(conn, row[1])"): 1,
            ("_journal_index_columns", "_journal_event_id_unique_matches", "_journal_index_columns(conn, row[1])"): 1,
            ("_journal_index_columns", "_journal_run_type_index_matches", "_journal_index_columns(conn, index_name)"): 1,
            ("_journal_index_columns", "_journal_unique_constraint_set", "_journal_index_columns(conn, row[1])"): 1,
        }
    ),
    "core/runtime/snapshot_store.py": Counter(
        {
            ("_snapshot_unique_constraint_set", "_snapshot_shape_current", "_snapshot_unique_constraint_set(conn)"): 1,
            ("_snapshot_shape_current", "snapshot_preflight", "_snapshot_shape_current(conn)"): 1,
        }
    ),
}


def _validate_pragma_model(path: str, tree: ast.Module, result: GuardResult) -> bool:
    expected = {
        name: digests
        for (model_path, name), digests in _PRAGMA_FUNCTION_DIGESTS.items()
        if model_path == path
    }
    provenance = {
        name: digest
        for (model_path, name), digest in _PRAGMA_PROVENANCE_DIGESTS.items()
        if model_path == path
    }
    has_dynamic_pragma = any(
        isinstance(node, ast.JoinedStr) and "PRAGMA" in ast.unparse(node)
        for node in ast.walk(tree)
    )
    if not has_dynamic_pragma or (not expected and not provenance):
        return True
    actual: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name in expected or node.name in provenance
        ):
            actual.setdefault(node.name, []).append(_semantic_digest(node))
    valid = True
    for name, digests in expected.items():
        if set(actual.get(name, [])) != set(digests) or len(actual.get(name, [])) != len(digests):
            result.fail(path, tree, "AUDITED_EXCEPTION_DRIFT", f"{name} semantic shape changed")
            valid = False
    for name, digest in provenance.items():
        if actual.get(name) != [digest]:
            result.fail(path, tree, "AUDITED_EXCEPTION_DRIFT", f"{name} identifier provenance changed")
            valid = False
    allowed_references = _PRAGMA_ALLOWED_REFERENCES.get(path, Counter())
    audited_names = {key[0] for key in allowed_references}
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    actual_references: Counter[tuple[str, str, str]] = Counter()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Name) or node.id not in audited_names or not isinstance(node.ctx, ast.Load):
            continue
        parent = parents.get(node)
        if not isinstance(parent, ast.Call) or parent.func is not node:
            result.fail(path, node, "AUDITED_EXCEPTION_DRIFT", f"{node.id} reference escaped direct call")
            valid = False
            continue
        actual_references[(node.id, _qualified_function(parent, parents), ast.unparse(parent))] += 1
    if actual_references != allowed_references:
        result.fail(path, tree, "AUDITED_EXCEPTION_DRIFT", "PRAGMA helper caller inventory changed")
        valid = False
    return valid


def _validate_memory_fragment_helpers(
    path: str, tree: ast.Module, result: GuardResult
) -> bool:
    if path != "core/memory_manager.py":
        return True
    helper_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"_committed_exchange_join", "_committed_exchange_filter"}
    ]
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if not helper_calls and not (
        {"_committed_exchange_join", "_committed_exchange_filter"} & functions.keys()
    ):
        return True
    join = functions.get("_committed_exchange_join")
    filter_ = functions.get("_committed_exchange_filter")
    valid = True
    join_body = (
        [item for item in join.body if not _is_docstring_statement(item)]
        if join is not None
        else []
    )
    filter_body = (
        [item for item in filter_.body if not _is_docstring_statement(item)]
        if filter_ is not None
        else []
    )
    if join is None or len(join_body) != 1 or not isinstance(join_body[0], ast.Return):
        result.fail(path, tree, "AUDITED_EXCEPTION_DRIFT", "join helper shape changed")
        valid = False
    elif _string_value(join_body[0].value) is None:
        result.fail(path, join, "AUDITED_EXCEPTION_DRIFT", "join helper is not literal")
        valid = False
    if filter_ is None or len(filter_body) != 1 or not isinstance(filter_body[0], ast.Return):
        result.fail(path, tree, "AUDITED_EXCEPTION_DRIFT", "filter helper shape changed")
        valid = False
    else:
        returned = filter_body[0].value
        expressions = (
            _formatted_expressions(returned) if isinstance(returned, ast.JoinedStr) else []
        )
        if not expressions or {ast.unparse(item) for item in expressions} != {
            "table_alias",
            "join_prefix",
        }:
            result.fail(
                path,
                filter_,
                "AUDITED_EXCEPTION_DRIFT",
                "filter helper may only interpolate private aliases",
            )
            valid = False
        external_args = [call for call in helper_calls if call.args or call.keywords]
        if external_args:
            result.fail(
                path,
                external_args[0],
                "AUDITED_EXCEPTION_DRIFT",
                "fragment helper caller supplied dynamic aliases",
            )
            valid = False
    return valid


def _is_docstring_statement(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _validate_select_one_callers(
    path: str, tree: ast.Module, result: GuardResult
) -> bool:
    if path != "core/runtime/event_journal_store.py":
        return True
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    expected = Counter(
        {
            (
                "SQLiteRunEventJournal._append_impl",
                "SELECT * FROM runtime_event_journal WHERE event_id = ?",
                "(record.event_id,)",
            ): 1,
            (
                "SQLiteRunEventJournal._append_impl",
                "SELECT * FROM runtime_event_journal WHERE run_id = ? AND sequence = ?",
                "(record.run_id, record.sequence)",
            ): 1,
            (
                "SQLiteRunEventJournal.get_by_event_id",
                "SELECT * FROM runtime_event_journal WHERE event_id = ?",
                "(event_id,)",
            ): 1,
        }
    )
    actual: Counter[tuple[str, str | None, str]] = Counter()
    valid = True
    for reference in ast.walk(tree):
        if not isinstance(reference, ast.Attribute) or reference.attr != "_select_one":
            continue
        parent = parents.get(reference)
        if not isinstance(parent, ast.Call) or parent.func is not reference:
            result.fail(
                path,
                reference,
                "SELECT_ONE_REFERENCE_ESCAPE",
                "_select_one may only appear as an exact audited direct call",
            )
            valid = False
            continue
        statement = _string_value(parent.args[0]) if parent.args else None
        normalized = " ".join(statement.split()) if statement is not None else None
        parameters = ast.unparse(parent.args[1]) if len(parent.args) >= 2 else "<missing>"
        actual[(_qualified_function(parent, parents), normalized, parameters)] += 1
    if actual != expected:
        result.fail(
            path,
            tree,
            "SELECT_ONE_CALLER_DRIFT",
            "_select_one direct caller/reference inventory changed",
        )
        valid = False
    return valid


def _order_mapping_is_exact(function: ast.AST | None) -> bool:
    if function is None:
        return False
    for node in ast.walk(function):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "order" for target in targets):
            continue
        if not isinstance(node.value, ast.IfExp):
            return False
        return {
            _string_value(node.value.body),
            _string_value(node.value.orelse),
        } == {"ASC", "DESC"} and isinstance(node.value.test, ast.Name)
    return False


def _placeholder_binding_is_safe(
    function: ast.AST | None,
    statement: ast.JoinedStr,
    call: ast.Call,
) -> bool:
    if function is None or len(call.args) < 2:
        return False
    expressions = _formatted_expressions(statement)
    if len(expressions) != 1 or not isinstance(expressions[0], ast.Name):
        return False
    placeholder = expressions[0].id
    assignments = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == placeholder for target in node.targets)
    ]
    if len(assignments) != 1:
        return False
    value = assignments[0].value
    if not (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "join"
        and _string_value(value.func.value) == ","
        and len(value.args) == 1
        and isinstance(value.args[0], ast.GeneratorExp)
        and _string_value(value.args[0].elt) == "?"
        and len(value.args[0].generators) == 1
        and isinstance(value.args[0].generators[0].iter, ast.Name)
    ):
        return False
    values_name = value.args[0].generators[0].iter.id
    bound = call.args[1]
    return isinstance(bound, ast.Name) and bound.id == values_name


_PRAGMA_EXCEPTIONS = {
    ("core/memory_manager.py", "_table_columns_match", "PRAGMA table_info("),
    ("core/memory_manager.py", "_index_definition", "PRAGMA index_list("),
    ("core/memory_manager.py", "_index_definition", "PRAGMA index_info("),
    ("core/memory_manager.py", "_unique_constraint_set", "PRAGMA index_list("),
    ("core/memory_manager.py", "_unique_constraint_set", "PRAGMA index_info("),
    ("core/memory_manager.py", "_unique_named_indexes", "PRAGMA index_list("),
    ("core/runtime/event_journal_store.py", "_journal_index_columns", "PRAGMA index_info("),
    ("core/runtime/snapshot_store.py", "_snapshot_shape_current", "PRAGMA index_info("),
    ("core/runtime/snapshot_store.py", "_snapshot_unique_constraint_set", "PRAGMA index_info("),
}


def _joined_string_allowed(
    *,
    path: str,
    qualified: str,
    function: ast.AST | None,
    statement: ast.JoinedStr,
    call: ast.Call,
    parents: dict[ast.AST, ast.AST],
    helpers_valid: bool,
    pragma_model_valid: bool,
    result: GuardResult,
) -> bool:
    expressions = _formatted_expressions(statement)
    if not expressions:
        return True
    short_function = qualified.split(".")[-1]
    text = ast.unparse(statement)
    pragma_key = next(
        (
            key
            for key in _PRAGMA_EXCEPTIONS
            if key[0] == path and key[1] == short_function and key[2] in text
        ),
        None,
    )
    if pragma_key is not None:
        allowed_identifiers = _PRAGMA_IDENTIFIER_EXPRESSIONS.get(pragma_key, set())
        identifier_shape = {ast.unparse(expression) for expression in expressions}
        if (
            pragma_model_valid
            and len(expressions) == 1
            and identifier_shape == allowed_identifiers
            and _fail_closed_handler_for_sink(call, parents) is not None
        ):
            result.audited_exceptions.append(f"{path}:{short_function}:{pragma_key[2]}")
            return True
        result.fail(
            path,
            call,
            "AUDITED_EXCEPTION_DRIFT",
            "PRAGMA identifier provenance/read-only/fail-closed shape changed",
        )
        return False

    rendered = {ast.unparse(expression) for expression in expressions}
    helper_calls = {
        "self._committed_exchange_join()",
        "self._committed_exchange_filter()",
    }
    if path == "core/memory_manager.py" and rendered <= helper_calls | {"order"}:
        if not helpers_valid or not (rendered & helper_calls):
            return False
        if "order" in rendered and not _order_mapping_is_exact(function):
            return False
        result.audited_exceptions.append(f"{path}:{short_function}:memory-fragment")
        return True

    if path == "core/memory_manager.py" and short_function == "delete_messages":
        if _placeholder_binding_is_safe(function, statement, call):
            result.audited_exceptions.append(f"{path}:{short_function}:in-placeholder")
            return True
    return False


def scan_sources(
    sources: dict[str, str], *, enforce_inventory: bool = True
) -> GuardResult:
    result = GuardResult()
    parsed: dict[str, ast.Module] = {}
    receiver_inventory: dict[str, tuple[set[str], set[str]]] = {}
    business_calls: dict[str, set[ast.Call]] = {}
    observed_business_calls: Counter[tuple[str, str, str, str, str]] = Counter()
    modules_to_scan: set[str] = set()
    for path, source in sources.items():
        try:
            parsed[path] = ast.parse(source, filename=path)
        except SyntaxError as exc:
            result.findings.append(
                GuardFinding(path, exc.lineno or 0, "SYNTAX_ERROR", str(exc))
            )
            continue
        tree = parsed[path]
        direct_owner = _imports_sqlite(tree)
        factory_aliases = _imported_sqlite_factory_aliases(
            tree, path=path, result=result
        )
        receiver_names, receiver_attributes = _known_sql_receivers(
            tree, factory_aliases
        )
        audited_bound, unknown_bound = _factory_bound_receivers(
            tree, factory_aliases
        )
        receiver_names.update(audited_bound)
        receiver_inventory[path] = (receiver_names, receiver_attributes)
        business_calls[path] = set()
        parents = {
            child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
        }
        constants = _immutable_module_strings(tree)
        assigned_by_scope: dict[ast.AST, dict[str, ast.AST]] = {}
        sql_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in SQL_METHODS
        ]
        if direct_owner:
            result.owners.add(path)
            modules_to_scan.add(path)
        for call in sql_calls:
            if _receiver_known(call.func.value, receiver_names, receiver_attributes):
                if not direct_owner:
                    result.owners.add(path)
                    modules_to_scan.add(path)
                continue
            first_arg = ast.unparse(call.args[0]) if call.args else "<missing>"
            business_key = (
                path,
                _qualified_function(call, parents),
                ast.unparse(call.func.value),
                call.func.attr,
                first_arg,
            )
            business_receiver_key = business_key[:4]
            if business_receiver_key in _AUDITED_BUSINESS_RECEIVERS:
                business_calls[path].add(call)
                result.business_execute_count += 1
                observed_business_calls[business_key] += 1
                continue
            receiver_name = (
                call.func.value.id if isinstance(call.func.value, ast.Name) else None
            )
            from_unknown_factory = receiver_name in unknown_bound
            scope = _enclosing_function(call, parents) or tree
            if scope not in assigned_by_scope:
                assigned_by_scope[scope] = _assigned_statement_expressions(scope)
            clearly_sql_capable = (
                from_unknown_factory
                or call.func.attr in {"executemany", "executescript"}
                or bool(
                    call.args
                    and _statement_is_sql_shaped(
                        call.args[0], constants, assigned_by_scope[scope]
                    )
                )
            )
            if not clearly_sql_capable:
                # 无factory/DB证据且非SQL-shaped的unknown execute不属于SQL候选。
                continue
            result.owners.add(path)
            modules_to_scan.add(path)
            result.unknown_receiver_count += 1
            result.fail(
                path,
                unknown_bound.get(receiver_name, call),
                "UNKNOWN_SQLITE_FACTORY",
                f"cannot prove SQL receiver factory for {ast.unparse(call.func.value)!r}",
            )
            if direct_owner:
                result.fail(
                    path,
                    call,
                    "UNRESOLVED_SQLITE_RECEIVER",
                    f"cannot classify receiver {ast.unparse(call.func.value)!r}",
                )
            if isinstance(call.func.value, ast.Name):
                receiver_names.add(call.func.value.id)
            elif isinstance(call.func.value, ast.Attribute):
                receiver_attributes.add(ast.unparse(call.func.value))

    if enforce_inventory and observed_business_calls != _AUDITED_BUSINESS_EXECUTE_CALLS:
        result.findings.append(
            GuardFinding(
                "<business-inventory>",
                0,
                "BUSINESS_EXECUTE_INVENTORY_MISMATCH",
                "audited production business execute call inventory changed",
            )
        )

    if enforce_inventory and result.owners != set(EXPECTED_SQLITE_OWNERS):
        result.findings.append(
            GuardFinding(
                "<inventory>",
                0,
                "SQLITE_OWNER_INVENTORY_MISMATCH",
                f"expected={sorted(EXPECTED_SQLITE_OWNERS)!r} actual={sorted(result.owners)!r}",
            )
        )

    for path in sorted(modules_to_scan):
        tree = parsed[path]
        parents = {
            child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
        }
        constants = _immutable_module_strings(tree)
        receiver_names, receiver_attributes = receiver_inventory[path]
        helpers_valid = _validate_memory_fragment_helpers(path, tree, result)
        pragma_model_valid = _validate_pragma_model(path, tree, result)
        select_one_valid = _validate_select_one_callers(path, tree, result)
        top_level_functions: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
        for item in tree.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                top_level_functions.setdefault(item.name, []).append(item)
        for definitions in top_level_functions.values():
            for shadowed in definitions[:-1]:
                result.shadowed_sink_count += sum(
                    1
                    for candidate in ast.walk(shadowed)
                    if isinstance(candidate, ast.Call)
                    and isinstance(candidate.func, ast.Attribute)
                    and candidate.func.attr in SQL_METHODS
                    and _receiver_known(
                        candidate.func.value, receiver_names, receiver_attributes
                    )
                )
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in SQL_METHODS
            ):
                continue
            if node in business_calls[path]:
                continue
            if not _receiver_known(node.func.value, receiver_names, receiver_attributes):
                result.fail(
                    path,
                    node,
                    "UNRESOLVED_SQLITE_RECEIVER",
                    f"cannot classify receiver {ast.unparse(node.func.value)!r}",
                )
                continue
            result.sink_count += 1
            if node.func.attr == "executescript":
                result.executescript_count += 1
                result.fail(path, node, "PRODUCTION_EXECUTESCRIPT", "executescript is forbidden")
                continue
            if node.func.attr == "executemany":
                result.executemany_count += 1
            if not node.args:
                result.fail(path, node, "MISSING_STATEMENT", "SQL call has no first argument")
                continue
            statement = node.args[0]
            if _string_value(statement) is not None:
                continue
            if isinstance(statement, ast.Name) and statement.id in constants:
                continue
            qualified = _qualified_function(node, parents)
            function = _enclosing_function(node, parents)
            if (
                path == "core/runtime/event_journal_store.py"
                and qualified == "SQLiteRunEventJournal._select_one"
                and isinstance(statement, ast.Name)
                and statement.id == "statement"
                and select_one_valid
            ):
                result.audited_exceptions.append(f"{path}:{qualified}:literal-callers")
                continue
            if isinstance(statement, ast.JoinedStr) and _joined_string_allowed(
                path=path,
                qualified=qualified,
                function=function,
                statement=statement,
                call=node,
                parents=parents,
                helpers_valid=helpers_valid,
                pragma_model_valid=pragma_model_valid,
                result=result,
            ):
                continue
            code = _dynamic_statement_finding_code(statement)
            result.fail(path, statement, code, ast.unparse(statement))
    return result


def _scan_snippet(source: str, path: str = "core/example.py") -> GuardResult:
    return scan_sources({path: source}, enforce_inventory=False)


def _assert_guard_passes(source: str, path: str = "core/example.py") -> None:
    result = _scan_snippet(source, path)
    assert result.findings == []


def _assert_guard_fails(source: str, code: str, path: str = "core/example.py") -> None:
    result = _scan_snippet(source, path)
    assert code in {finding.code for finding in result.findings}, result.findings


@pytest.mark.parametrize(
    ("statement", "code"),
    [
        ('f"SELECT * FROM x WHERE y = {value}"', "INTERPOLATED_FSTRING"),
        ('"SELECT * FROM x WHERE y = " + value', "STRING_CONCATENATION"),
        ('"SELECT * FROM x WHERE y = %s" % value', "PERCENT_FORMATTING"),
        ('"SELECT * FROM x WHERE y = {}".format(value)', "FORMAT_CALL"),
        ("statement", "UNRESOLVED_STATEMENT"),
    ],
)
def test_scanner_rejects_dynamic_statement_forms(statement: str, code: str) -> None:
    _assert_guard_fails(
        f"""
import sqlite3
def run(value, statement):
    connection = sqlite3.connect(':memory:')
    connection.execute({statement})
""",
        code,
    )


@pytest.mark.parametrize("statement", ["'SELECT 1'", "'SELECT ' + value"])
def test_scanner_rejects_every_production_executescript(statement: str) -> None:
    _assert_guard_fails(
        f"""
import sqlite3
def run(value):
    connection = sqlite3.connect(':memory:')
    connection.executescript({statement})
""",
        "PRODUCTION_EXECUTESCRIPT",
    )


def test_scanner_rejects_raw_value_join_for_in_clause() -> None:
    _assert_guard_fails(
        """
import sqlite3
def run(ids):
    connection = sqlite3.connect(':memory:')
    values = ','.join(str(value) for value in ids)
    connection.execute(f'SELECT * FROM x WHERE id IN ({values})')
""",
        "INTERPOLATED_FSTRING",
    )


def test_scanner_rejects_unknown_sqlite_wrapper_receiver() -> None:
    _assert_guard_fails(
        """
import sqlite3
def run():
    connection = sqlite3.connect(':memory:')
    wrapper = object()
    wrapper.execute('SELECT 1')
""",
        "UNRESOLVED_SQLITE_RECEIVER",
    )


def test_owner_inventory_expansion_fails_closed() -> None:
    sources = _production_sources()
    sources["core/sixth_sqlite_owner.py"] = "import sqlite3\nconnection = sqlite3.connect(':memory:')\n"
    result = scan_sources(sources)
    assert "SQLITE_OWNER_INVENTORY_MISMATCH" in {
        finding.code for finding in result.findings
    }


@pytest.mark.parametrize(
    ("import_line", "factory"),
    [
        ("from core.persistence_migration import open_read_only", "open_read_only"),
        ("from core.persistence_migration import open_read_only as ro", "ro"),
    ],
)
def test_imported_open_read_only_wrapper_owner_and_dynamic_sink_fail_closed(
    import_line: str, factory: str
) -> None:
    result = scan_sources(
        {
            "core/new_reader.py": f"""
{import_line}
def run(path, user_value):
    with {factory}(path) as connection:
        return connection.execute(
            f"SELECT * FROM messages WHERE agent_id = '{{user_value}}'"
        ).fetchall()
"""
        }
    )
    codes = {finding.code for finding in result.findings}
    assert "SQLITE_OWNER_INVENTORY_MISMATCH" in codes
    assert "INTERPOLATED_FSTRING" in codes


def test_imported_wrapper_without_sql_sink_does_not_create_owner() -> None:
    result = scan_sources(
        {
            "core/new_reader.py": """
from core.persistence_migration import open_read_only as ro
def open_only(path):
    with ro(path):
        pass
"""
        },
        enforce_inventory=False,
    )
    assert result.owners == set()
    assert result.sink_count == 0
    assert result.findings == []


@pytest.mark.parametrize(
    "factory_setup",
    [
        "from core.persistence_migration import open_read_only\nfactory = open_read_only",
        (
            "from core.persistence_migration import open_read_only\n"
            "factory = open_read_only\nfactory2 = factory"
        ),
        "import core.persistence_migration as persistence",
        (
            "import core.persistence_migration as persistence\n"
            "factory = persistence.open_read_only"
        ),
    ],
)
def test_audited_wrapper_alias_forms_create_new_owner_and_classify_dynamic_sql(
    factory_setup: str,
) -> None:
    factory = (
        "factory2"
        if "factory2 =" in factory_setup
        else "factory"
        if "factory =" in factory_setup
        else "persistence.open_read_only"
    )
    result = scan_sources(
        {
            "core/new_reader.py": f"""
{factory_setup}
def run(path, value):
    with {factory}(path) as resource:
        resource.execute(f"SELECT * FROM messages WHERE id = {{value}}")
"""
        }
    )
    codes = {finding.code for finding in result.findings}
    assert "SQLITE_OWNER_INVENTORY_MISMATCH" in codes
    assert "INTERPOLATED_FSTRING" in codes


@pytest.mark.parametrize(
    ("statement", "statement_code"),
    [
        ('f"SELECT * FROM messages WHERE id = {value}"', "INTERPOLATED_FSTRING"),
        ('"SELECT * FROM messages WHERE id = " + str(value)', "STRING_CONCATENATION"),
        ('"SELECT * FROM messages WHERE id = %s" % value', "PERCENT_FORMATTING"),
        ('"SELECT * FROM messages WHERE id = {}".format(value)', "FORMAT_CALL"),
        ('"DELETE FROM messages WHERE id IN (" + values_sql + ")"', "STRING_CONCATENATION"),
        ('"SELECT * FROM messages WHERE id = ?", (value,)', None),
        ('"SELECT 1"', None),
        ("statement", "UNRESOLVED_STATEMENT"),
    ],
)
def test_unknown_sql_factory_receiver_fails_closed_and_classifies_statement(
    statement: str, statement_code: str | None
) -> None:
    result = scan_sources(
        {
            "core/future_reader.py": f"""
from some_module import future_read_only
def run(path, value, statement, values):
    values_sql = ",".join(str(item) for item in values)
    with future_read_only(path) as resource:
        resource.execute({statement})
"""
        }
    )
    codes = {finding.code for finding in result.findings}
    assert "UNKNOWN_SQLITE_FACTORY" in codes
    assert "SQLITE_OWNER_INVENTORY_MISMATCH" in codes
    if statement_code is not None:
        assert statement_code in codes


def test_unknown_factory_nested_dynamic_statement_never_silently_passes() -> None:
    result = scan_sources(
        {
            "core/future_reader.py": """
from some_module import future_read_only
def run(path, value):
    sql = "SELECT * FROM messages WHERE id = {}".format(value)
    with future_read_only(path) as resource:
        resource.execute(sql)
"""
        }
    )
    codes = {finding.code for finding in result.findings}
    assert "UNKNOWN_SQLITE_FACTORY" in codes
    assert "SQLITE_OWNER_INVENTORY_MISMATCH" in codes
    assert "UNRESOLVED_STATEMENT" in codes


def test_unknown_assigned_factory_parameterized_sql_fails_owner_closed() -> None:
    result = scan_sources(
        {
            "core/future_reader.py": """
from some_module import future_read_only
def run(path, value):
    resource = future_read_only(path)
    resource.execute("SELECT * FROM messages WHERE id = ?", (value,))
"""
        }
    )
    codes = {finding.code for finding in result.findings}
    assert "UNKNOWN_SQLITE_FACTORY" in codes
    assert "SQLITE_OWNER_INVENTORY_MISMATCH" in codes
    assert "INTERPOLATED_FSTRING" not in codes


@pytest.mark.parametrize("method", ["executemany", "executescript"])
def test_unknown_factory_bulk_or_script_sql_fails_closed(method: str) -> None:
    arguments = (
        '"INSERT INTO messages(id) VALUES (?)", [(1,)]'
        if method == "executemany"
        else '"SELECT 1"'
    )
    result = scan_sources(
        {
            "core/future_writer.py": f"""
def run(factory, path):
    with factory(path) as resource:
        resource.{method}({arguments})
"""
        }
    )
    codes = {finding.code for finding in result.findings}
    assert "UNKNOWN_SQLITE_FACTORY" in codes
    if method == "executescript":
        assert "PRODUCTION_EXECUTESCRIPT" in codes


def test_unknown_factory_without_sql_sink_is_not_an_owner() -> None:
    result = scan_sources(
        {
            "core/resource_only.py": """
def run(future_factory, path):
    with future_factory(path) as resource:
        resource.close()
"""
        },
        enforce_inventory=False,
    )
    assert result.owners == set()
    assert result.unknown_receiver_count == 0
    assert result.findings == []


def test_business_execute_negative_control_remains_non_sql() -> None:
    result = scan_sources(
        {
            "core/business_example.py": """
def run(agent, task):
    return agent.execute(task)
"""
        },
        enforce_inventory=False,
    )
    assert result.owners == set()
    assert result.sink_count == 0
    assert result.findings == []


def test_exact_audited_business_receiver_accepts_sql_looking_task_text() -> None:
    result = scan_sources(
        {
            "core/agent_router.py": """
class AgentRouter:
    def _execute_knowledge_retrieval(self):
        return self.retrieval_execution_service.execute("SELECT business task")
"""
        },
        enforce_inventory=False,
    )
    assert result.owners == set()
    assert result.sink_count == 0
    assert result.findings == []


@pytest.mark.parametrize(
    ("statement", "statement_code"),
    [
        ('"SELECT 1"', None),
        ('"SELECT * FROM x WHERE id = ?", (value,)', None),
        ('f"SELECT {value}"', "INTERPOLATED_FSTRING"),
        ('"SELECT " + str(value)', "STRING_CONCATENATION"),
        ('"SELECT %s" % value', "PERCENT_FORMATTING"),
        ('"SELECT {}".format(value)', "FORMAT_CALL"),
        (
            '"SELECT id FROM x WHERE id = {}".format(value)',
            "FORMAT_CALL",
        ),
    ],
)
def test_ambiguous_receiver_sql_intent_enters_common_statement_classifier(
    statement: str, statement_code: str | None
) -> None:
    result = scan_sources(
        {
            "core/ambiguous_reader.py": f"""
def run(unknown, value):
    return unknown.execute({statement})
"""
        }
    )
    codes = {finding.code for finding in result.findings}
    assert "UNKNOWN_SQLITE_FACTORY" in codes
    assert "SQLITE_OWNER_INVENTORY_MISMATCH" in codes
    if statement_code is not None:
        assert statement_code in codes


@pytest.mark.parametrize(
    "separator",
    SQLITE_SQL_WHITESPACE,
    ids=("space", "tab", "carriage-return", "line-feed", "form-feed"),
)
def test_sqlite_accepts_frozen_sql_token_whitespace(separator: str) -> None:
    with sqlite3.connect(":memory:") as connection:
        assert connection.execute(f"SELECT{separator}1").fetchone() == (1,)


def test_sqlite_rejects_vertical_tab_as_sql_token_whitespace() -> None:
    with sqlite3.connect(":memory:") as connection:
        with pytest.raises(sqlite3.OperationalError, match="unrecognized token"):
            connection.execute("SELECT\v1")


@pytest.mark.parametrize(
    ("static_text", "statement_code"),
    [
        ("SELECT {}", "FORMAT_CALL"),
        ("SELECT\t{}", "FORMAT_CALL"),
        ("SELECT\r{}", "FORMAT_CALL"),
        ("SELECT\n{}", "FORMAT_CALL"),
        ("SELECT\f{}", "FORMAT_CALL"),
        ("SELECT", None),
        ("   select\t{}", "FORMAT_CALL"),
    ],
    ids=(
        "space",
        "tab",
        "carriage-return",
        "line-feed",
        "form-feed",
        "exact-keyword",
        "leading-space-lowercase",
    ),
)
def test_ambiguous_receiver_honors_sql_token_boundaries(
    static_text: str, statement_code: str | None
) -> None:
    statement = repr(static_text)
    if statement_code == "FORMAT_CALL":
        statement += ".format(value)"
    result = _scan_snippet(
        f"""
def run(unknown, value):
    return unknown.execute({statement})
"""
    )
    codes = {finding.code for finding in result.findings}
    assert "UNKNOWN_SQLITE_FACTORY" in codes
    if statement_code is not None:
        assert statement_code in codes


@pytest.mark.parametrize(
    "static_text",
    [
        "SELECTOR\t{}",
        "SELECTED\t{}",
        "selection\t{}",
        "run SELECT\t{}",
        "RESELECT\t{}",
        "run task\t{}",
    ],
)
def test_non_sql_token_boundaries_remain_non_sql(static_text: str) -> None:
    result = _scan_snippet(
        f"""
def run(unknown, value):
    return unknown.execute({static_text!r}.format(value))
"""
    )
    assert result.owners == set()
    assert result.sink_count == 0
    assert result.findings == []


def test_exact_audited_business_receiver_accepts_tab_sql_looking_text() -> None:
    result = _scan_snippet(
        """
class AgentRouter:
    def _execute_knowledge_retrieval(self):
        return self.retrieval_execution_service.execute("SELECT\tbusiness task")
""",
        path="core/agent_router.py",
    )
    assert result.business_execute_count == 1
    assert result.owners == set()
    assert result.sink_count == 0
    assert result.findings == []


def test_ambiguous_assigned_format_statement_never_silently_passes() -> None:
    result = scan_sources(
        {
            "core/ambiguous_reader.py": """
def run(unknown, value):
    sql = "SELECT {}".format(value)
    return unknown.execute(sql)
"""
        }
    )
    codes = {finding.code for finding in result.findings}
    assert "UNKNOWN_SQLITE_FACTORY" in codes
    assert "UNRESOLVED_STATEMENT" in codes


def test_ambiguous_non_sql_format_remains_non_sql() -> None:
    result = scan_sources(
        {
            "core/non_sql_task.py": """
def run(unknown, value):
    return unknown.execute("run task {}".format(value))
"""
        },
        enforce_inventory=False,
    )
    assert result.owners == set()
    assert result.sink_count == 0
    assert result.findings == []


@pytest.mark.parametrize(
    "escape",
    [
        "callback(open_read_only)",
        "return open_read_only",
        "items.append(open_read_only)",
    ],
)
def test_audited_factory_reference_escape_fails_closed(escape: str) -> None:
    result = scan_sources(
        {
            "core/factory_escape.py": f"""
from core.persistence_migration import open_read_only
def register(callback, items):
    {escape}
"""
        },
        enforce_inventory=False,
    )
    assert "SQLITE_FACTORY_REFERENCE_ESCAPE" in {
        finding.code for finding in result.findings
    }


def test_pragma_parameter_and_fail_open_handler_are_rejected() -> None:
    _assert_guard_fails(
        """
import sqlite3
def _table_columns_match(conn: sqlite3.Connection, user_table):
    try:
        conn.execute(f"PRAGMA table_info({user_table})")
    except sqlite3.Error:
        return True
""",
        "AUDITED_EXCEPTION_DRIFT",
        "core/memory_manager.py",
    )


@pytest.mark.parametrize(
    ("source", "expected_code"),
    [
        (
            """
import sqlite3
def _table_columns_match(conn: sqlite3.Connection, table, expected):
    try:
        conn.execute(f"PRAGMA table_info({table})")
    except sqlite3.Error:
        return False
""",
            "AUDITED_EXCEPTION_DRIFT",
        ),
        (
            """
import sqlite3
def _table_columns_match(conn: sqlite3.Connection, user_table, expected):
    try:
        conn.execute(f"PRAGMA table_info({user_table})")
    except sqlite3.Error:
        return False
""",
            "AUDITED_EXCEPTION_DRIFT",
        ),
        (
            """
import sqlite3
def _table_columns_match(conn: sqlite3.Connection, table, expected):
    try:
        conn.execute(f"PRAGMA table_info({table})")
    except sqlite3.Error:
        return ("success",)
""",
            "AUDITED_EXCEPTION_DRIFT",
        ),
        (
            """
import sqlite3
def _table_columns_match(conn: sqlite3.Connection, table, expected):
    try:
        conn.execute("SELECT 1")
    except sqlite3.Error:
        return False
    conn.execute(f"PRAGMA table_info({table})")
""",
            "AUDITED_EXCEPTION_DRIFT",
        ),
        (
            """
import sqlite3
def unapproved(conn: sqlite3.Connection, table):
    try:
        conn.execute(f"PRAGMA table_info({table})")
    except sqlite3.Error:
        return False
""",
            "INTERPOLATED_FSTRING",
        ),
        (
            """
import sqlite3
def unapproved(conn: sqlite3.Connection, value):
    try:
        conn.execute(f"PRAGMA user_version = {value}")
    except sqlite3.Error:
        return False
""",
            "INTERPOLATED_FSTRING",
        ),
        (
            """
import sqlite3
def _table_columns_match(conn: sqlite3.Connection, table, expected):
    try:
        conn.execute(f"PRAGMA table_info({table})")
    except sqlite3.Error:
        return False
    return True
""",
            "AUDITED_EXCEPTION_DRIFT",
        ),
    ],
)
def test_pragma_exception_negative_matrix(source: str, expected_code: str) -> None:
    _assert_guard_fails(source, expected_code, "core/memory_manager.py")


def test_select_one_alias_reference_escape_is_rejected() -> None:
    source = (ROOT / "core/runtime/event_journal_store.py").read_text(encoding="utf-8")
    source = source.replace(
        "    def _record_from_row(self, row: sqlite3.Row) -> JournalRecord:",
        "    def injected(self, statement):\n"
        "        selector = self._select_one\n"
        "        return selector(statement, ())\n\n"
        "    def _record_from_row(self, row: sqlite3.Row) -> JournalRecord:",
    )
    result = _scan_snippet(source, "core/runtime/event_journal_store.py")
    assert "SELECT_ONE_REFERENCE_ESCAPE" in {
        finding.code for finding in result.findings
    }


@pytest.mark.parametrize(
    ("method_body", "expected_code"),
    [
        ("        callback(self._select_one)\n", "SELECT_ONE_REFERENCE_ESCAPE"),
        ("        return self._select_one\n", "SELECT_ONE_REFERENCE_ESCAPE"),
        ("        items.append(self._select_one)\n", "SELECT_ONE_REFERENCE_ESCAPE"),
        ("        alias = journal._select_one\n", "SELECT_ONE_REFERENCE_ESCAPE"),
        (
            "        return (lambda: self._select_one('SELECT 1', ()))()\n",
            "SELECT_ONE_CALLER_DRIFT",
        ),
        (
            "        return self._select_one('SELECT 1', ())\n",
            "SELECT_ONE_CALLER_DRIFT",
        ),
    ],
)
def test_select_one_reference_and_new_caller_negative_matrix(
    method_body: str, expected_code: str
) -> None:
    source = (ROOT / "core/runtime/event_journal_store.py").read_text(encoding="utf-8")
    source = source.replace(
        "    def _record_from_row(self, row: sqlite3.Row) -> JournalRecord:",
        "    def injected(self):\n" + method_body + "\n"
        "    def _record_from_row(self, row: sqlite3.Row) -> JournalRecord:",
    )
    result = _scan_snippet(source, "core/runtime/event_journal_store.py")
    assert expected_code in {finding.code for finding in result.findings}, result.findings


def test_select_one_dynamic_caller_and_exception_drift_fail() -> None:
    source = (ROOT / "core/runtime/event_journal_store.py").read_text(encoding="utf-8")
    source = source.replace(
        "    def _record_from_row(self, row: sqlite3.Row) -> JournalRecord:",
        "    def injected(self, statement):\n"
        "        return self._select_one(statement, ())\n\n"
        "    def _record_from_row(self, row: sqlite3.Row) -> JournalRecord:",
    )
    result = _scan_snippet(source, "core/runtime/event_journal_store.py")
    assert "SELECT_ONE_CALLER_DRIFT" in {finding.code for finding in result.findings}

    _assert_guard_fails(
        """
import sqlite3
class MemoryManager:
    @staticmethod
    def _committed_exchange_filter(user_text='m'):
        return f'{user_text}.id = 1'
    @staticmethod
    def _committed_exchange_join():
        return ' LEFT JOIN x ON 1=1 '
    def run(self, user_text):
        connection = sqlite3.connect(':memory:')
        connection.execute(f'SELECT 1 WHERE {self._committed_exchange_filter()}')
""",
        "AUDITED_EXCEPTION_DRIFT",
        "core/memory_manager.py",
    )


@pytest.mark.parametrize(
    "statement_and_parameters",
    [
        '("SELECT * FROM x WHERE y = ?", (value,))',
        '("SELECT * FROM x LIMIT ?", (value,))',
        '("SELECT * FROM x WHERE y = :value", {"value": value})',
    ],
)
def test_scanner_accepts_db_api_binding(statement_and_parameters: str) -> None:
    _assert_guard_passes(
        f"""
import sqlite3
def run(value):
    connection = sqlite3.connect(':memory:')
    connection.execute{statement_and_parameters}
"""
    )


def test_scanner_accepts_cursor_module_constant_and_zero_interpolation() -> None:
    _assert_guard_passes(
        """
import sqlite3
_SQL = 'SELECT 1'
def run():
    connection = sqlite3.connect(':memory:')
    cursor = connection.cursor()
    cursor.execute(_SQL)
    connection.execute(f'SELECT 1')
"""
    )


def test_scanner_accepts_exact_bool_order_and_memory_fragments() -> None:
    _assert_guard_passes(
        """
import sqlite3
class MemoryManager:
    @staticmethod
    def _committed_exchange_filter(table_alias='m', join_prefix='me'):
        return f"({table_alias}.exchange_id IS NULL OR {join_prefix}.state = 'COMMITTED')"
    @staticmethod
    def _committed_exchange_join():
        return ' LEFT JOIN message_exchanges me ON me.exchange_id = m.exchange_id'
    def get_chat_history(self, ascending):
        order = 'ASC' if ascending else 'DESC'
        connection = sqlite3.connect(':memory:')
        connection.execute(f'''SELECT m.id FROM messages m
            {self._committed_exchange_join()}
            WHERE {self._committed_exchange_filter()}
            ORDER BY m.id {order} LIMIT ?''', (1,))
""",
        "core/memory_manager.py",
    )


def test_scanner_accepts_in_placeholder_shape_with_separate_values() -> None:
    _assert_guard_passes(
        """
import sqlite3
class MemoryManager:
    @staticmethod
    def _committed_exchange_filter(table_alias='m', join_prefix='me'):
        return f"({table_alias}.exchange_id IS NULL OR {join_prefix}.state = 'COMMITTED')"
    @staticmethod
    def _committed_exchange_join():
        return ' LEFT JOIN message_exchanges me ON me.exchange_id = m.exchange_id'
    def delete_messages(self, message_ids):
        placeholders = ','.join('?' for _ in message_ids)
        connection = sqlite3.connect(':memory:')
        connection.execute(f'DELETE FROM messages WHERE id IN ({placeholders})', message_ids)
""",
        "core/memory_manager.py",
    )


def test_scanner_accepts_exact_fixed_and_schema_metadata_pragma_exceptions() -> None:
    sources = _production_sources()
    for path in (
        "core/memory_manager.py",
        "core/runtime/event_journal_store.py",
        "core/runtime/snapshot_store.py",
    ):
        result = scan_sources({path: sources[path]}, enforce_inventory=False)
        assert result.findings == [], (path, result.findings)
        assert any("PRAGMA" in exception for exception in result.audited_exceptions)


def test_current_production_sqlite_inventory_and_statement_authority() -> None:
    result = scan_sources(_production_sources())
    assert result.owners == set(EXPECTED_SQLITE_OWNERS)
    assert result.sink_count == 152
    assert result.shadowed_sink_count == 4
    assert result.sink_count - result.shadowed_sink_count == 148
    assert result.executescript_count == 0
    assert result.executemany_count == 0
    assert result.unknown_receiver_count == 0
    assert result.business_execute_count == 13
    assert EXPECTED_RUNTIME_SQL_SINKS == 57
    assert EXPECTED_STARTUP_ADMIN_SQL_SINKS == 91
    assert (
        EXPECTED_RUNTIME_SQL_SINKS + EXPECTED_STARTUP_ADMIN_SQL_SINKS
        == result.sink_count - result.shadowed_sink_count
    )
    assert result.findings == []
    assert result.audited_exceptions


def test_current_imported_sqlite_factory_provenance_inventory() -> None:
    sources = _production_sources()
    consumers = {
        path
        for path, source in sources.items()
        if _imported_sqlite_factory_aliases(ast.parse(source, filename=path))
    }
    assert AUDITED_SQLITE_CONNECTION_FACTORIES == {
        "core.persistence_migration.open_read_only"
    }
    assert consumers == {
        "core/memory_manager.py",
        "core/runtime/event_consumer.py",
        "core/runtime/event_journal_store.py",
        "core/runtime/snapshot_store.py",
    }
    assert consumers < set(EXPECTED_SQLITE_OWNERS)


def test_current_tool_and_route_sql_authority_inventory() -> None:
    import inspect

    import server
    from core.runtime.tool_registry import ToolRegistry
    from tools.registry import register_all_tools

    registry = ToolRegistry()
    register_all_tools(registry)
    registry.freeze()
    assert {item.name for item in registry.descriptors()} == {
        "list_files",
        "analyze_excel",
        "get_system_status",
        "complex_workflow_simulator",
    }
    forbidden = {
        "sql",
        "raw_sql",
        "sql_fragment",
        "table",
        "table_name",
        "column",
        "column_name",
        "order_by",
        "sort",
        "sort_by",
        "sort_direction",
        "query_expression",
    }
    for registration in registry.registrations():
        descriptor_words = {
            word.strip(".,:;()[]{}").lower()
            for word in registration.descriptor.description.split()
        }
        assert descriptor_words.isdisjoint(forbidden)
        function = getattr(registration.adapter, "function", None)
        if function is not None:
            assert set(inspect.signature(function).parameters).isdisjoint(forbidden)

    openapi = server.app.openapi()
    for path, operations in openapi["paths"].items():
        for operation in operations.values():
            if not isinstance(operation, dict):
                continue
            parameter_names = {
                parameter["name"] for parameter in operation.get("parameters", [])
            }
            assert parameter_names.isdisjoint(forbidden), path
    history_parameters = {
        parameter["name"]
        for parameter in openapi["paths"]["/api/history/{agent_id}"]["get"]["parameters"]
    }
    assert history_parameters == {"agent_id", "limit", "offset"}


def test_formal_sql_injection_contract_is_scoped_and_supported() -> None:
    security = (ROOT / "docs/runtime/runtime_security_boundary.md").read_text(encoding="utf-8")
    capability = (ROOT / "docs/runtime/runtime_capability_matrix.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs/runtime/runtime_architecture_v1.md").read_text(encoding="utf-8")
    owners = (ROOT / "docs/runtime/runtime_owner_matrix.md").read_text(encoding="utf-8")
    combined = "\n".join((security, capability, architecture, owners))

    assert "SQL Injection protection" in capability
    assert "SUPPORTED" in capability
    assert "current LocalAgent production SQLite inventory" in combined
    assert "SQL structure owner" in combined
    assert "DB-API" in combined
    assert "No generic SQL firewall" in combined
    assert "No NL2SQL" in combined
    assert "No SQL Tool" in combined
    assert "FTS query-language semantics" in combined
    assert "LIKE wildcard semantics" in combined
    assert "schema-metadata PRAGMA" in combined
    assert "Chroma" in combined and "direct SQL owner" in combined
    assert "internal logs" in combined
    assert "SUPPORTED_CURRENT_INVENTORY" not in combined
    assert "SQL Injection、SSRF均为 `NOT_APPLICABLE_CURRENT_INVENTORY`" not in combined
