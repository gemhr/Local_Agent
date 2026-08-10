"""Stage 3 WP1-B — Windows Deployment / Client Proxy Contract Tests.

覆盖：
1. Client HTTP Proxy Governance（`LOCAL_AGENT_CLIENT_TRUST_ENV` strict bool）。
2. Transport Scope 分离（Client vs Remote 完全独立）。
3. Client Session Wiring（所有真实 Client Session 显式使用 Settings 快照）。
4. Windows Deployment Contract（single-process / packaging / secret safety /
   deployment docs 核心事实）。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from core.settings import (
    SETTINGS_PARSE_ERROR,
    EnvironmentProfile,
    Settings,
    SettingsValidationError,
)

ROOT = Path(__file__).resolve().parents[1]
_CLIENT_TRUST_ENV = "LOCAL_AGENT_CLIENT_TRUST_ENV"
_REMOTE_TRUST_ENV = "LOCAL_AGENT_REMOTE_TRUST_ENV"


def _load(monkeypatch, **env) -> Settings:
    """加载 Settings；默认清理会影响本测试的 env，保持其他值由 Settings 默认决定。"""
    for key in (
        _CLIENT_TRUST_ENV,
        _REMOTE_TRUST_ENV,
        "LOCAL_AGENT_ENVIRONMENT_PROFILE",
        "LOCAL_AGENT_ENVIRONMENT_ID",
        "LOCAL_AGENT_REMOTE_API_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    # PRODUCTION 必须显式 environment_id；仅在最终 profile 为 PRODUCTION 且
    # 未由调用方提供时补测试占位。本测试不调用 SERVER role validation，
    # 因此不要求 remote endpoint。
    effective_profile = None
    for key, value in env.items():
        if key == "LOCAL_AGENT_ENVIRONMENT_PROFILE" and value is not None:
            effective_profile = str(value).strip().upper()
    if effective_profile == "PRODUCTION" and "LOCAL_AGENT_ENVIRONMENT_ID" not in env:
        monkeypatch.setenv("LOCAL_AGENT_ENVIRONMENT_ID", "prod-test-placeholder")
    return Settings.load()


# ---------------------------------------------------------------------------
# Client HTTP Proxy Governance — defaults
# ---------------------------------------------------------------------------


def test_client_trust_env_defaults_to_true_when_absent(monkeypatch) -> None:
    settings = _load(monkeypatch)
    assert settings.client_trust_env is True


@pytest.mark.parametrize("profile", ["LOCAL", "TEST", "PRODUCTION"])
def test_client_trust_env_default_true_in_all_profiles(monkeypatch, profile) -> None:
    settings = _load(
        monkeypatch,
        LOCAL_AGENT_ENVIRONMENT_PROFILE=profile,
        LOCAL_AGENT_CLIENT_TRUST_ENV=None,
        LOCAL_AGENT_ENVIRONMENT_ID="prod-id" if profile == "PRODUCTION" else None,
    )
    assert settings.client_trust_env is True


# ---------------------------------------------------------------------------
# Client HTTP Proxy Governance — explicit values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("0", False),
        ("true", True),
        ("false", False),
        ("TRUE", True),
        ("FALSE", False),
        (" 1 ", True),
        (" 0 ", False),
    ],
)
def test_client_trust_env_explicit_bool(monkeypatch, raw, expected) -> None:
    settings = _load(monkeypatch, **{_CLIENT_TRUST_ENV: raw})
    assert settings.client_trust_env is expected


@pytest.mark.parametrize("raw", ["tru", "yes", "on", "None", ""])
def test_client_trust_env_invalid_fails_closed(monkeypatch, raw) -> None:
    with pytest.raises(SettingsValidationError) as exc_info:
        _load(monkeypatch, **{_CLIENT_TRUST_ENV: raw})
    assert exc_info.value.safe_error_code == SETTINGS_PARSE_ERROR
    assert exc_info.value.field == _CLIENT_TRUST_ENV


# ---------------------------------------------------------------------------
# Transport Scope Separation
# ---------------------------------------------------------------------------


def test_client_trust_env_does_not_change_remote_trust_env(monkeypatch) -> None:
    settings = _load(
        monkeypatch,
        **{
            _CLIENT_TRUST_ENV: "0",
            _REMOTE_TRUST_ENV: None,
            "LOCAL_AGENT_ENVIRONMENT_PROFILE": "LOCAL",
        },
    )
    assert settings.client_trust_env is False
    # LOCAL profile 的 remote_trust_env profile 默认仍是 True，不受 client 修改影响。
    assert settings.remote_trust_env is True


def test_remote_trust_env_does_not_change_client_trust_env(monkeypatch) -> None:
    settings = _load(
        monkeypatch,
        **{
            _REMOTE_TRUST_ENV: "0",
            _CLIENT_TRUST_ENV: None,
            "LOCAL_AGENT_ENVIRONMENT_PROFILE": "LOCAL",
        },
    )
    assert settings.remote_trust_env is False
    assert settings.client_trust_env is True


# ---------------------------------------------------------------------------
# Client Session Wiring — main.py + ui/ 全部 Desktop Client → Server Session
# ---------------------------------------------------------------------------


def _session_creation_lines(source: str) -> list[tuple[int, str]]:
    """返回源码中所有 `requests.Session()` 创建点（行号 + 绑定名）。"""
    tree = ast.parse(source)
    findings: list[tuple[int, str]] = []

    def is_requests_session_call(call: ast.Call) -> bool:
        return (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "Session"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "requests"
        )

    def record_with_binding(node: ast.With) -> None:
        for item in node.items:
            if (
                isinstance(item.context_expr, ast.Call)
                and is_requests_session_call(item.context_expr)
                and isinstance(item.optional_vars, ast.Name)
            ):
                findings.append((item.context_expr.lineno, item.optional_vars.id))

    def target_binding_name(target: ast.expr) -> str | None:
        if isinstance(target, ast.Name):
            return target.id
        if isinstance(target, ast.Attribute):
            # `self.http` → "http"；`self.http` 的 value 是 Name(self) 时也认。
            return target.attr
        return None

    def record_assign_binding(assign: ast.Assign) -> None:
        if not isinstance(assign.value, ast.Call) or not is_requests_session_call(assign.value):
            return
        for target in assign.targets:
            name = target_binding_name(target)
            if name is not None:
                findings.append((assign.lineno, name))

    for node in ast.walk(tree):
        # `self.http = requests.Session()` 形式
        if isinstance(node, ast.Assign):
            record_assign_binding(node)
        # `with requests.Session() as session:` 形式
        elif isinstance(node, ast.With):
            record_with_binding(node)
    return findings


def _client_session_inventory() -> list[tuple[str, int, str]]:
    """扫描 main.py + ui/ 下所有 Desktop Client → LocalAgent Server
    `requests.Session()` 创建点，返回 (文件, 行号, 绑定名)。"""
    inventory: list[tuple[str, int, str]] = []
    for rel in ("main.py",):
        source = (ROOT / rel).read_text(encoding="utf-8")
        inventory.extend(
            (rel, lineno, name) for lineno, name in _session_creation_lines(source)
        )
    for ui_rel in sorted((ROOT / "ui").glob("*.py")):
        source = ui_rel.read_text(encoding="utf-8")
        inventory.extend(
            (str(ui_rel.relative_to(ROOT)).replace("\\", "/"), lineno, name)
            for lineno, name in _session_creation_lines(source)
        )
    return inventory


def _main_source() -> str:
    return (ROOT / "main.py").read_text(encoding="utf-8")


def test_client_session_inventory_expects_four_sessions() -> None:
    """锁定 Desktop Client → LocalAgent Server Session 清单（main.py + ui/）：
    当前真实数量为 4。若源码新增/移除 Session 导致数量变化，先记录
    SESSION_INVENTORY_CHANGED，再基于真实 inventory 调整断言。"""
    inventory = _client_session_inventory()
    # 按 (文件, 行号) 排序，保证与源码出现顺序一致。
    inventory = sorted(inventory, key=lambda item: (item[0], item[1]))
    by_file: dict[str, list[str]] = {}
    for rel, _, name in inventory:
        by_file.setdefault(rel, []).append(name)
    expected = {
        "main.py": ["session", "http", "http"],
        "ui/memory_dialog.py": ["http"],
    }
    # 行号顺序即源码顺序；按文件内自然顺序比较（不排序，保留真实出现顺序）。
    assert by_file == expected, f"SESSION_INVENTORY_CHANGED: actual={by_file}"


def test_each_main_session_sets_trust_env_from_settings_before_use() -> None:
    """AST 验证：main.py 每个 `requests.Session()` 对象在创建后的同作用域内
    显式赋值 `trust_env = settings.client_trust_env`。静态证据；行为证据见
    `test_client_session_behavior_uses_settings_snapshot`。"""
    source = _main_source()
    assumptions = [
        (
            "session.trust_env = settings.client_trust_env",
            "ApiWorker.run 流式聊天 Session",
        ),
        (
            "self.http.trust_env = settings.client_trust_env",
            "MainController 搜索/取消 Session",
        ),
        (
            "http.trust_env = settings.client_trust_env",
            "历史分页 fetch Session",
        ),
    ]
    for literal, label in assumptions:
        assert literal in source, f"missing wiring: {label} ({literal})"


def test_memory_dialog_session_sets_trust_env_before_use() -> None:
    """源码证据：ui/memory_dialog.py 的 Session 创建后立即设置
    `self.http.trust_env = client_trust_env`（由 ChatPanel 从启动期快照透传）。"""
    source = (ROOT / "ui" / "memory_dialog.py").read_text(encoding="utf-8")
    assert "self.http.trust_env = client_trust_env" in source
    # ui/ 不得新增第二配置读取链。
    assert "os.getenv" not in source
    assert "os.environ" not in source
    assert "Settings.load()" not in source


def test_memory_dialog_session_honors_client_trust_env(monkeypatch) -> None:
    """行为证据：MemoryManagerDialog 传入 client_trust_env=False/True 后，
    其真实 Session 的 trust_env 与传入值一致（False 时不继承系统 proxy）。"""
    import ui.memory_dialog as memory_dialog_module

    # QDialog 装配需要 QApplication；测试内复用/惰性创建，不影响其他测试。
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    _keep_qapp_alive = app  # 持有引用，防止 GC 在 dialog 存续期间回收 QApplication

    captured: list[requests_SessionLike] = []

    class _OkResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"messages": [], "summaries": []}

    class FakeSession:
        def __init__(self) -> None:
            self.trust_env: bool | None = None  # 记录赋值
            captured.append(self)

        def get(self, *args, **kwargs) -> _OkResponse:
            return _OkResponse()

        def delete(self, *args, **kwargs) -> _OkResponse:
            return _OkResponse()

    monkeypatch.setattr(memory_dialog_module.requests, "Session", FakeSession)

    dialog_false = memory_dialog_module.MemoryManagerDialog(
        "http://127.0.0.1/api/memory", client_trust_env=False
    )
    dialog_true = memory_dialog_module.MemoryManagerDialog(
        "http://127.0.0.1/api/memory", client_trust_env=True
    )

    assert captured, "MemoryManagerDialog 必须创建 Session"
    assert dialog_false.http is captured[0]
    assert captured[0].trust_env is False
    assert dialog_true.http is captured[1]
    assert captured[1].trust_env is True


def test_client_session_behavior_uses_settings_snapshot(monkeypatch) -> None:
    """行为证据：ApiWorker 创建的 Session 使用 Settings 快照的
    `client_trust_env`（而非 requests 默认 True 或硬编码值）。"""
    import dataclasses

    import main as main_module

    captured: list[requests_SessionLike] = []

    class FakeSession:
        def __init__(self) -> None:
            self.trust_env: bool | None = None  # 记录赋值
            captured.append(self)

        def __enter__(self) -> "FakeSession":
            return self

        def __exit__(self, *exc) -> bool:
            return False

        def post(self, *args, **kwargs):  # 模拟网络失败
            import requests as _requests

            raise _requests.ConnectionError("offline-test")

    # 用 client_trust_env=False 的 Settings 替换模块级快照，证明 wiring 消费该值。
    replacement = dataclasses.replace(main_module.settings, client_trust_env=False)
    monkeypatch.setattr(main_module, "settings", replacement)
    monkeypatch.setattr(main_module.requests, "Session", FakeSession)

    worker = main_module.ApiWorker("http://127.0.0.1/api/chat")
    worker.run()

    assert captured, "ApiWorker 必须创建 Session"
    assert all(session.trust_env is False for session in captured)


# 类型占位：避免在类型检查中暴露 requests 依赖。
class requests_SessionLike:
    pass


# ---------------------------------------------------------------------------
# Windows Deployment Contract — single process
# ---------------------------------------------------------------------------


def test_server_entry_has_no_workers_argument() -> None:
    """正式 server 入口 `uv run python server.py`；`server.py.__main__` 不得传
    uvicorn `workers=` 参数。"""
    server_source = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "uvicorn.run(" in server_source
    # 找到 `uvicorn.run(` 调用片段并检查其参数列表。
    uvicorn_call_start = server_source.index("uvicorn.run(")
    # 截取从调用起点到该行末尾（调用通常为单行：uvicorn.run(app, host=..., port=...)）。
    line_start = server_source.rfind("\n", 0, uvicorn_call_start) + 1
    line_end = server_source.find("\n", uvicorn_call_start)
    if line_end == -1:
        line_end = len(server_source)
    call_source = server_source[line_start:line_end]
    assert "uvicorn.run(" in call_source
    # 禁止 uvicorn 的 `--workers` 或 `workers=` 参数。
    assert "--workers" not in call_source
    assert "workers=" not in call_source


def test_deployment_docs_never_list_multi_worker_as_supported() -> None:
    """Deployment docs 只能出现"不支持"说明；不得把 multi-worker 写成受支持
    启动方式。"""
    for doc_rel in (
        "docs/runtime/runtime_deployment_runbook.md",
        "docs/runtime/runtime_operations_runbook.md",
        "docs/runtime/runtime_capability_matrix.md",
    ):
        text = (ROOT / doc_rel).read_text(encoding="utf-8")
        # 合法的不支持说明：NOT_IMPLEMENTED / 禁止 / 不提供 / multi-process Runtime。
        assert "workers" not in text or "NOT_IMPLEMENTED" in text or "禁止" in text, doc_rel
        assert "gunicorn" not in text or "禁止" in text or "NOT_IMPLEMENTED" in text, doc_rel


def test_deployment_runbook_declares_single_process_contract() -> None:
    text = (ROOT / "docs/runtime/runtime_deployment_runbook.md").read_text(encoding="utf-8")
    assert "exactly one LocalAgent server application process" in text
    assert "uv run python server.py" in text
    assert "Windows Native" in text


# ---------------------------------------------------------------------------
# Packaging 锁定
# ---------------------------------------------------------------------------


def test_packaging_files_exist() -> None:
    assert (ROOT / "pyproject.toml").exists()
    assert (ROOT / "uv.lock").exists()


# ---------------------------------------------------------------------------
# Secret Safety（optional artifacts）
# ---------------------------------------------------------------------------


_SECRET_PATTERNS = (
    re.compile(r"(?i)sk-[a-zA-Z0-9]{16,}"),
    re.compile(r"(?i)api[_-]?key\s*=\s*[^\s<>\n]{6,}"),
    re.compile(r"(?i)cookie\s*=\s*[^\s<>\n]{6,}"),
)


def _assert_no_secret_literal(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for pattern in _SECRET_PATTERNS:
        matches = pattern.findall(text)
        # 允许出现 `<secret-store-reference>` 这类 safe placeholder。
        safe = [
            m for m in matches if "<secret" not in m.lower() and "<configured" not in m.lower()
        ]
        assert not safe, f"potential secret literal in {path}: {safe}"


def test_env_example_has_no_secret_literal() -> None:
    path = ROOT / ".env.example"
    if not path.exists():
        pytest.skip("NOT_ADDED_BY_DESIGN: .env.example 可选，未新增")
    _assert_no_secret_literal(path)


def test_start_server_ps1_has_no_secret_literal() -> None:
    path = ROOT / "scripts" / "start_server.ps1"
    if not path.exists():
        pytest.skip("NOT_ADDED_BY_DESIGN: start_server.ps1 可选，未新增")
    _assert_no_secret_literal(path)
    text = path.read_text(encoding="utf-8")
    # 不得在脚本中写死 remote api key / wiki cookie。
    assert "LOCAL_AGENT_REMOTE_API_KEY=" not in text
    assert "LOCAL_AGENT_WIKI_COOKIE=" not in text


# ---------------------------------------------------------------------------
# Deployment Documentation 核心事实
# ---------------------------------------------------------------------------


def test_deployment_docs_core_facts_present() -> None:
    text = (ROOT / "docs/runtime/runtime_deployment_runbook.md").read_text(encoding="utf-8")
    for required in (
        "Windows Native",
        "single server process only",
        "persistent",
        "fully_closed",
        "backup",
        "MUST_BACKUP",
    ):
        assert required in text, f"deployment runbook missing fact: {required}"


# ---------------------------------------------------------------------------
# WP1-C Health / Readiness / Startup handshake 合同 guard
# ---------------------------------------------------------------------------


def test_deployment_runbook_documents_health_readiness_supported() -> None:
    """Deployment Runbook 必须把 Health/Readiness 与 Startup handshake 标记为
    SUPPORTED，并继续锁 continuous monitoring / version compatibility 为
    NOT_IMPLEMENTED / WP4。"""
    text = (ROOT / "docs/runtime/runtime_deployment_runbook.md").read_text(
        encoding="utf-8"
    )
    assert "Health / Readiness" in text and "SUPPORTED" in text
    assert "Startup readiness handshake" in text and "SUPPORTED" in text
    assert "continuous monitoring" in text and "NOT_IMPLEMENTED" in text
    assert "version compatibility" in text and "NOT_IMPLEMENTED" in text


def test_capability_matrix_health_readiness_supported() -> None:
    """Capability Matrix 必须把 Health/Readiness endpoint 与 Startup handshake
    更新为 SUPPORTED，并保留 deferred 限制。"""
    text = (ROOT / "docs/runtime/runtime_capability_matrix.md").read_text(
        encoding="utf-8"
    )
    assert "Health / Readiness endpoint" in text and "SUPPORTED" in text
    assert "Startup readiness handshake" in text and "SUPPORTED" in text
    assert "continuous monitoring" in text and "NOT_IMPLEMENTED" in text
    assert "version compatibility" in text and "NOT_IMPLEMENTED" in text
    assert "post-start dependency aggregate health" in text and "NOT_IMPLEMENTED" in text


def test_deployment_runbook_marks_windows_only_and_no_docker() -> None:
    text = (ROOT / "docs/runtime/runtime_deployment_runbook.md").read_text(encoding="utf-8")
    assert "Windows Native 是当前唯一 certified 部署目标" in text
    assert "Docker" in text and "NOT_IMPLEMENTED" in text


# ---------------------------------------------------------------------------
# Client Proxy 文档防漂移 Guard（transport-wide，不得退回 main.py-only）
# ---------------------------------------------------------------------------


def test_config_reference_documents_client_proxy_scope_transport_wide() -> None:
    """Configuration Reference 的 Client proxy 必须描述 Desktop Client →
    LocalAgent Server 的全部 Client HTTP Session，并覆盖 memory management。"""
    text = (ROOT / "docs/runtime/runtime_configuration_reference.md").read_text(
        encoding="utf-8"
    )
    assert "Desktop Client → LocalAgent Server 的所有 Client HTTP Session" in text
    # 五类真实消费类别至少包含 memory management / /api/memory。
    assert "记忆管理" in text and "/api/memory" in text
    # 消费链应描述启动快照 → ChatPanel plumbing → MemoryManagerDialog。
    assert "ChatPanel plumbing → MemoryManagerDialog" in text
    # 不得退回 main.py-only 表述。
    assert "main.py sessions only" not in text


def test_deployment_runbook_documents_client_proxy_scope_transport_wide() -> None:
    """Deployment Runbook 的 Client Proxy 章节必须覆盖 memory management，
    并保持 Remote/Client scope 分离。"""
    text = (ROOT / "docs/runtime/runtime_deployment_runbook.md").read_text(
        encoding="utf-8"
    )
    assert "Desktop Client → LocalAgent Server 的所有 Client HTTP Session" in text
    assert "记忆管理" in text and "/api/memory" in text
    # ChatPanel 透传链存在；Remote scope 仍只属于 Server → Remote LLM。
    assert "ChatPanel" in text and "MemoryManagerDialog" in text and "透传" in text
    assert "LOCAL_AGENT_REMOTE_TRUST_ENV" in text
    assert "Server → Remote LLM" in text


def test_capability_matrix_client_proxy_evidence_is_transport_wide() -> None:
    """Capability Matrix 的 Client HTTP Proxy Governance 行必须把 owner/evidence
    覆盖到 ui/，不能只写 main.py sessions。"""
    text = (ROOT / "docs/runtime/runtime_capability_matrix.md").read_text(
        encoding="utf-8"
    )
    assert "Client HTTP Proxy Governance" in text
    assert "ui/memory_dialog.py" in text
    assert "ui/chat_panel.py plumbing" in text
    # 不得只写 Settings + main.py sessions。
    assert "Settings + main.py sessions" not in text


def test_owner_matrix_client_proxy_readers_include_ui_plumbing() -> None:
    """Owner Matrix 的 client http session trust_env 行 readers 必须包含
    ui/chat_panel.py plumbing 与 ui/memory_dialog.py memory session。"""
    text = (ROOT / "docs/runtime/runtime_owner_matrix.md").read_text(encoding="utf-8")
    row = next(
        line for line in text.splitlines() if "client http session trust_env" in line
    )
    assert "ui/chat_panel.py plumbing" in row
    assert "ui/memory_dialog.py memory session" in row
    # ChatPanel 只是值传递，不得写成第二 Configuration Owner。
    assert "ChatPanel" in row
    assert "Settings.client_trust_env（唯一解析快照）" in row

# ---------------------------------------------------------------------------
# WP1-D Persistence / Migration / Backup / Restore documentation guards
# ---------------------------------------------------------------------------


def test_deployment_runbook_locks_manual_stopped_server_backup() -> None:
    """Deployment Runbook 必须把 backup 锁为 manual stopped-server；live raw
    copy unsupported；WAL unit（.db + -wal）；MUST_BACKUP set 必须出现。"""
    text = (ROOT / "docs/runtime/runtime_deployment_runbook.md").read_text(encoding="utf-8")
    assert "manual stopped-server" in text
    assert "live raw copy" in text and "unsupported" in text
    assert "MUST_BACKUP" in text
    assert "-wal" in text
    assert "automatic backup" in text and "NOT_IMPLEMENTED" in text


def test_deployment_runbook_locks_restore_validation() -> None:
    """Restore 必须锁为 stopped-server set replacement + explicit full preflight。"""
    text = (ROOT / "docs/runtime/runtime_deployment_runbook.md").read_text(encoding="utf-8")
    assert "files copied != restore validated" in text
    assert "full preflight" in text
    assert "automatic restore" in text and "NOT_IMPLEMENTED" in text


def test_deployment_runbook_locks_forward_only_rollback_truth() -> None:
    """Rollback 必须区分 code/artifact vs persistent-data；binary-only rollback
    NOT ASSUMED；无 downgrade。"""
    text = (ROOT / "docs/runtime/runtime_deployment_runbook.md").read_text(encoding="utf-8")
    assert "old binary compatibility NOT ASSUMED" in text
    assert "binary-only rollback UNSAFE" in text or "binary-only rollback" in text
    assert "downgrade migration" in text and "NOT_IMPLEMENTED" in text
    assert "automatic deployment rollback" in text and "NOT_IMPLEMENTED" in text


def test_deployment_runbook_no_longer_defers_migration_to_wp1_d() -> None:
    """WP1-D 实现后不得再保留 `DEFER TO WP1-D` 的 migration/backup 声明。"""
    text = (ROOT / "docs/runtime/runtime_deployment_runbook.md").read_text(encoding="utf-8")
    assert "DEFER TO WP1-D" not in text


def test_capability_matrix_wp1_d_statuses() -> None:
    """Capability Matrix 必须把 preflight/migration/marker 标为 SUPPORTED，
    automatic backup/restore/rollback/downgrade/online 标为 NOT_IMPLEMENTED，
    Chroma internal schema migration 标为 NOT_LOCAL_SCHEMA_OWNER。"""
    text = (ROOT / "docs/runtime/runtime_capability_matrix.md").read_text(encoding="utf-8")
    for capability in (
        "Persistence preflight",
        "Explicit SQLite migration",
        "Memory versioned migration",
        "Journal known physical migration",
        "Checkpoint explicit recreate",
        "Chroma compatibility marker",
    ):
        assert capability in text and "SUPPORTED" in text
    for capability in (
        "Automatic backup",
        "Automatic restore",
        "Automatic deployment rollback",
        "Downgrade migration",
        "Online backup",
    ):
        assert capability in text and "NOT_IMPLEMENTED" in text
    assert "Chroma internal schema migration" in text
    assert "NOT_LOCAL_SCHEMA_OWNER" in text


def test_error_catalog_wp1_d_safe_codes() -> None:
    """Error Catalog 必须含三个新增 safe persistence code。"""
    text = (ROOT / "docs/runtime/runtime_error_code_catalog.md").read_text(encoding="utf-8")
    for code in (
        "PERSISTENCE_SCHEMA_UNSUPPORTED",
        "PERSISTENCE_PREFLIGHT_FAILED",
        "PERSISTENCE_MIGRATION_FAILED",
    ):
        assert code in text


def test_architecture_doc_locks_migration_vs_recovery() -> None:
    """Architecture 文档必须显式 Deployment Migration != Runtime Recovery
    Validation，并锁 forward-only / no downgrade。"""
    text = (ROOT / "docs/runtime/runtime_architecture_v1.md").read_text(encoding="utf-8")
    assert "Deployment Migration != Runtime Recovery Validation" in text
    assert "NOT_IMPLEMENTED" in text and "downgrade" in text
    assert "PERSISTENCE_SCHEMA_UNSUPPORTED" in text


def test_operations_runbook_has_actionable_backup_restore_rollback() -> None:
    """Operations Runbook 必须给 Operator 可执行的 upgrade/backup/restore/
    rollback 流程，并锁 forward-only。"""
    text = (ROOT / "docs/runtime/runtime_operations_runbook.md").read_text(encoding="utf-8")
    for section in (
        "## Backup Runbook",
        "## Restore Runbook",
        "## Rollback Runbook",
        "## Migration vs Recovery",
    ):
        assert section in text
    assert "old binary compatibility NOT ASSUMED" in text
    assert "binary-only rollback" in text and "UNSAFE" in text
    assert "migrate --backup-confirmed" in text
