"""WP1-A Startup Validation / Role Boundary / KB policy / lifespan wiring 测试。

覆盖：role 边界（server 不要求 client cookie、client 不要求 model endpoint）、
role/parse/semantic failure 先于首个 resource 构造、KB required/degraded 策略、
7 个批准 knob 注入、并发语义回归（max_concurrency 仍为合同常量 2）、
两个 deprecated 表面、Fault 面保持隔离。
"""

from __future__ import annotations

import inspect
from pathlib import Path
import warnings

import pytest
from fastapi import FastAPI

import server
from core.chat_service import ChatService
from core.runtime import (
    CoordinatedRuntimeFactory,
    RuntimeInitializationError,
    StartupDependencySnapshot,
)
from core.runtime.admission import RuntimeAdmissionState
from core.runtime.application_services import RuntimeLifecycleState
from core.runtime.tool_governance import (
    PRODUCTION_AGENT_IDS,
    ToolGovernanceContext,
    ToolGovernanceOutcome,
    ToolGovernanceService,
    ToolPolicy,
    ToolPolicyCatalog,
)
from core.llm_engine import ScriptedEvaluationLLMEngine
from core.settings import (
    SETTINGS_SECURITY_POLICY_ERROR,
    STARTUP_CONFIGURATION_ERROR,
    RuntimeProfile,
    Settings,
    SettingsValidationError,
    validate_role_configuration,
)
from tools.registry import register_all_tools


def _load(monkeypatch, **env):
    for key, value in {
        "LOCAL_AGENT_ENVIRONMENT_PROFILE": None,
        "LOCAL_AGENT_REMOTE_API_BASE_URL": "https://example.test/v1",
    }.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    return Settings.load()


class _RaisingVectorDB:
    """构造期必失败的 VectorDB，用于 KB degraded/required 集成测试。"""

    def __init__(self, *args, **kwargs) -> None:
        raise RuntimeError("kb broken")


def _tmp_settings(
    monkeypatch,
    tmp_path,
    *,
    profile="LOCAL",
    kb_required=None,
    embedding_model_path=None,
):
    env = {
        "LOCAL_AGENT_ENVIRONMENT_PROFILE": profile,
        "LOCAL_AGENT_REMOTE_API_BASE_URL": "https://example.test/v1",
        "LOCAL_AGENT_MEMORY_DB_PATH": str(tmp_path / "memory.db"),
        "LOCAL_AGENT_EVENT_JOURNAL_DB_PATH": str(tmp_path / "journal.db"),
        "LOCAL_AGENT_SNAPSHOT_DB_PATH": str(tmp_path / "snap.db"),
        "LOCAL_AGENT_OBSERVABILITY_CHECKPOINT_DB_PATH": str(tmp_path / "obs.db"),
        "LOCAL_AGENT_CHROMA_DIR": str(tmp_path / "chroma"),
        "LOCAL_AGENT_EMBEDDING_MODEL_PATH": (
            embedding_model_path or str(tmp_path / "missing-embedding-model")
        ),
    }
    if profile == "PRODUCTION":
        env["LOCAL_AGENT_ENVIRONMENT_ID"] = "prod-integration"
        env["LOCAL_AGENT_TOOL_ALLOWED_READ_ROOTS"] = str(tmp_path.resolve())
    if kb_required is not None:
        env["LOCAL_AGENT_KB_REQUIRED"] = kb_required
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings.load()


_REPO_ROOT = Path(__file__).resolve().parents[1]
# 仓库内确定性的 repository-local healthy embedding 模型（WP1-C healthy KB 测试前置）。
_EMBEDDING_MODEL_DIR = _REPO_ROOT / "data" / "models" / "Qwen3-Embedding-0.6B"


# ---- role boundary ----

def test_server_role_does_not_require_client_cookie(monkeypatch) -> None:
    monkeypatch.delenv("LOCAL_AGENT_WIKI_COOKIE", raising=False)
    settings = _load(monkeypatch)
    validate_role_configuration(settings, role="SERVER")


def test_client_role_does_not_require_remote_endpoint(monkeypatch) -> None:
    settings = _load(monkeypatch, LOCAL_AGENT_REMOTE_API_BASE_URL=None)
    validate_role_configuration(settings, role="CLIENT")
    with pytest.raises(SettingsValidationError) as captured:
        validate_role_configuration(settings, role="SERVER")
    assert captured.value.safe_error_code == STARTUP_CONFIGURATION_ERROR


def test_script_role_has_no_server_requirements(monkeypatch) -> None:
    settings = _load(monkeypatch, LOCAL_AGENT_REMOTE_API_BASE_URL=None)
    validate_role_configuration(settings, role="SCRIPT")


def test_unknown_role_is_programming_error(monkeypatch) -> None:
    settings = _load(monkeypatch)
    with pytest.raises(ValueError):
        validate_role_configuration(settings, role="GATEWAY")


def test_api_base_url_is_client_only_consumer() -> None:
    lifespan_source = inspect.getsource(server.lifespan)
    assert "settings.api_base_url" not in lifespan_source


def test_no_configuration_reload_api() -> None:
    assert not hasattr(Settings, "reload")


# ---- validation ordering / startup ----

def test_role_validation_precedes_resource_construction_in_lifespan() -> None:
    source = inspect.getsource(server.lifespan)
    role_call = source.index("validate_role_configuration(settings, role=SERVER_ROLE)")
    assert role_call < source.index("RuntimeInitializationStack()")
    assert role_call < source.index("MemoryManager")


@pytest.mark.asyncio
async def test_role_validation_fails_before_first_resource_constructor(
    monkeypatch, tmp_path
) -> None:
    db_path = tmp_path / "memory.db"
    env = {
        "LOCAL_AGENT_ENVIRONMENT_PROFILE": "LOCAL",
        "LOCAL_AGENT_REMOTE_API_BASE_URL": None,
        "LOCAL_AGENT_LLM_BACKEND": "remote",
        "LOCAL_AGENT_MEMORY_DB_PATH": str(db_path),
    }
    settings = _load(monkeypatch, **env)
    monkeypatch.setattr(server, "settings", settings)
    app = FastAPI()
    with pytest.raises(SettingsValidationError) as captured:
        async with server.lifespan(app):
            pass
    assert captured.value.safe_error_code == STARTUP_CONFIGURATION_ERROR
    # MemoryManager（第一个 resource）从未被构造：tmp 下无 SQLite 文件。
    assert not db_path.exists()


# ---- KB required / degraded integration（真实 lifespan + 失败 VectorDB）----

@pytest.mark.asyncio
async def test_local_kb_failure_degrades_startup(monkeypatch, tmp_path) -> None:
    settings = _tmp_settings(monkeypatch, tmp_path, profile="LOCAL")
    monkeypatch.setattr(server, "settings", settings)
    monkeypatch.setattr(server, "VectorDBManager", _RaisingVectorDB)
    app = FastAPI()
    async with server.lifespan(app):
        assert app.state.runtime_lifecycle_state is RuntimeLifecycleState.READY
        assert (
            app.state.chat_service.router.knowledge_base_error
            == "KNOWLEDGE_BASE_INITIALIZATION_FAILED"
        )
        # WP1-C：allowed degradation 必须注入 immutable startup snapshot。
        assert (
            app.state.runtime_services.startup_dependency_snapshot
            .knowledge_base_degraded
            is True
        )


@pytest.mark.asyncio
async def test_test_kb_failure_degrades_startup(monkeypatch, tmp_path) -> None:
    settings = _tmp_settings(monkeypatch, tmp_path, profile="TEST")
    monkeypatch.setattr(server, "settings", settings)
    monkeypatch.setattr(server, "VectorDBManager", _RaisingVectorDB)
    app = FastAPI()
    async with server.lifespan(app):
        assert app.state.runtime_lifecycle_state is RuntimeLifecycleState.READY
        assert (
            app.state.chat_service.router.knowledge_base_error
            == "KNOWLEDGE_BASE_INITIALIZATION_FAILED"
        )
        assert (
            app.state.runtime_services.startup_dependency_snapshot
            .knowledge_base_degraded
            is True
        )


@pytest.mark.asyncio
async def test_production_kb_failure_fails_startup(monkeypatch, tmp_path) -> None:
    settings = _tmp_settings(monkeypatch, tmp_path, profile="PRODUCTION")
    monkeypatch.setattr(server, "settings", settings)
    monkeypatch.setattr(server, "VectorDBManager", _RaisingVectorDB)
    app = FastAPI()
    with pytest.raises(RuntimeInitializationError) as captured:
        async with server.lifespan(app):
            pass
    assert captured.value.component == "knowledge_base"
    assert app.state.runtime_lifecycle_state is RuntimeLifecycleState.STARTING


@pytest.mark.asyncio
async def test_production_kb_explicit_opt_in_degrades(monkeypatch, tmp_path) -> None:
    settings = _tmp_settings(
        monkeypatch, tmp_path, profile="PRODUCTION", kb_required="false"
    )
    monkeypatch.setattr(server, "settings", settings)
    monkeypatch.setattr(server, "VectorDBManager", _RaisingVectorDB)
    app = FastAPI()
    async with server.lifespan(app):
        assert app.state.runtime_lifecycle_state is RuntimeLifecycleState.READY
        assert (
            app.state.chat_service.router.knowledge_base_error
            == "KNOWLEDGE_BASE_INITIALIZATION_FAILED"
        )
        assert (
            app.state.runtime_services.startup_dependency_snapshot
            .knowledge_base_degraded
            is True
        )


@pytest.mark.resource_intensive
@pytest.mark.asyncio
async def test_healthy_kb_snapshot_is_not_degraded(monkeypatch, tmp_path) -> None:
    """healthy KB：真实 lifespan + 仓库内真实 embedding 模型 → degraded=False。

    使用确定性 repository-local healthy prerequisite
    （data/models/Qwen3-Embedding-0.6B）。只有该路径真实缺失时才 skip；
    lifespan 与 contract assertion 不包 catch，任何实现回归 / AssertionError
    直接 FAIL，不再被 broad except 误转成 ENVIRONMENT_BLOCKED skip。
    """
    if not _EMBEDDING_MODEL_DIR.is_dir():
        pytest.skip(
            "EMBEDDING_MODEL_PREREQUISITE_ABSENT: "
            f"repository-local embedding model missing: {_EMBEDDING_MODEL_DIR}"
        )
    settings = _tmp_settings(
        monkeypatch,
        tmp_path,
        profile="LOCAL",
        embedding_model_path=str(_EMBEDDING_MODEL_DIR),
    )
    monkeypatch.setattr(server, "settings", settings)
    app = FastAPI()
    async with server.lifespan(app):
        assert app.state.runtime_lifecycle_state is RuntimeLifecycleState.READY
        assert (
            app.state.runtime_services.startup_dependency_snapshot
            .knowledge_base_degraded
            is False
        )


# ---- WP1-C：Production Composition Root AdmissionGate identity + draining 行为链 ----

@pytest.mark.asyncio
async def test_production_composition_root_single_admission_gate(
    monkeypatch, tmp_path
) -> None:
    """锁定生产 Composition Root 五方消费同一 Application-level AdmissionGate，
    以及同一 gate 上 close_admission → /readyz 503 → /api/chat 503
    RUNTIME_SHUTTING_DOWN → active_runs 0→0 的完整行为链。

    装配来自真实 server.py::lifespan()；只把 KB 依赖替换为确定性失败 fixture
    （admission identity 与 KB 状态无关），避免真实向量库/模型下载依赖。
    使用模块级 server.app（持有 /health、/readyz、/api/chat 路由），并在
    finally 中快照/恢复 app.state，确保不把 lifespan 残留状态泄漏给其他测试
    （不依赖执行顺序）。
    """
    import httpx

    settings = _tmp_settings(monkeypatch, tmp_path, profile="LOCAL")
    monkeypatch.setattr(server, "settings", settings)
    monkeypatch.setattr(server, "VectorDBManager", _RaisingVectorDB)
    app = server.app
    state_backup = dict(app.state._state)
    transport = httpx.ASGITransport(app=app)
    try:
        async with server.lifespan(app):
            services = app.state.runtime_services
            gate = services.admission_gate

            # 五方 identity：services / ChatService / RuntimeFactory /
            # ShutdownCoordinator / app.state 消费同一个 gate object。
            # （factory 与 coordinator 无公开 gate accessor，使用其真实装配字段
            #   _services / _gate；factory 只经 self._services.admission_gate 读 gate）
            assert gate is app.state.chat_service.admission_gate
            assert gate is app.state.runtime_admission_gate
            assert app.state.coordinated_runtime_factory._services is services
            assert app.state.runtime_shutdown_coordinator._gate is gate

            run_registry = services.run_registry
            assert run_registry.observability_snapshot()["active_runs"] == 0

            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                # Before drain：ACCEPTING + /readyz 200（lifecycle READY，KB 可 degraded）
                assert gate.state is RuntimeAdmissionState.ACCEPTING
                readyz = await client.get("/readyz")
                assert readyz.status_code == 200
                assert readyz.json()["lifecycle"] == "READY"

                # close_admission → DRAINING
                gate.close_admission()
                assert gate.state is RuntimeAdmissionState.DRAINING

                # 同一 gate → /readyz 503 DRAINING
                readyz = await client.get("/readyz")
                assert readyz.status_code == 503
                assert readyz.json()["status"] == "DRAINING"

                # 同一 gate → /api/chat 在 Run 创建前 503 RUNTIME_SHUTTING_DOWN
                chat = await client.post(
                    "/api/chat",
                    json={"agent_id": "core_router", "query": "question"},
                )
                assert chat.status_code == 503
                assert chat.json() == {"detail": "RUNTIME_SHUTTING_DOWN"}

            # No new Run：admission rejection 发生在注册前，active_runs 保持 0 → 0
            assert run_registry.observability_snapshot()["active_runs"] == 0
    finally:
        # 恢复模块级 app.state，避免 lifespan 残留（CLOSED 等）污染后续测试。
        app.state._state.clear()
        app.state._state.update(state_backup)


# ---- WP2-A ToolRegistry 生产装配 ----

@pytest.mark.asyncio
async def test_lifespan_injects_populated_frozen_tool_registry(
    monkeypatch, tmp_path
) -> None:
    """真实 server.py::lifespan() 必须注入 populated + frozen ToolRegistry。

    只替换 KB 为确定性失败 fixture（ToolRegistry 装配与 KB 状态无关）。
    """
    settings = _tmp_settings(monkeypatch, tmp_path, profile="LOCAL")
    monkeypatch.setattr(server, "settings", settings)
    monkeypatch.setattr(server, "VectorDBManager", _RaisingVectorDB)
    app = FastAPI()
    async with server.lifespan(app):
        router = app.state.chat_service.router
        registry = router.tool_registry
        assert registry.frozen is True
        assert tuple(
            registration.descriptor.name
            for registration in registry.registrations()
        ) == (
            "list_files",
            "analyze_excel",
            "get_system_status",
            "complex_workflow_simulator",
        )
        # 全部四个注册都是 adapter-backed，且 Descriptor/Adapter identity 一致
        for registration in registry.registrations():
            assert (
                registration.adapter.spec.tool_name
                == registration.descriptor.name
            )
        # 兼容视图只读且派生自 Registry
        with pytest.raises(TypeError):
            router.tools["list_files"] = {}
        # planner prompt 只派生自 frozen Registry descriptors
        prompt = router._build_tool_planner_prompt("core_router")
        assert "list_files: List files in a local directory." in prompt
        assert "complex_workflow_simulator" in prompt


@pytest.mark.asyncio
async def test_tool_registry_duplicate_blocks_startup(
    monkeypatch, tmp_path
) -> None:
    """Registry 构造失败（duplicate）必须 startup fail，never READY。"""
    settings = _tmp_settings(monkeypatch, tmp_path, profile="LOCAL")
    monkeypatch.setattr(server, "settings", settings)
    monkeypatch.setattr(server, "VectorDBManager", _RaisingVectorDB)

    def duplicate_populator(tool_registry) -> None:
        register_all_tools(tool_registry)
        register_all_tools(tool_registry)  # 第二次注册同 canonical name → DUPLICATE

    monkeypatch.setattr(server, "register_all_tools", duplicate_populator)
    app = FastAPI()
    with pytest.raises(RuntimeInitializationError) as captured:
        async with server.lifespan(app):
            pass
    assert captured.value.component == "tool_registry"
    assert app.state.runtime_lifecycle_state is RuntimeLifecycleState.STARTING


# ---- WP2-B Tool Governance 生产装配 ----

@pytest.mark.asyncio
async def test_lifespan_injects_governance_into_production_router(
    monkeypatch, tmp_path
) -> None:
    """真实 lifespan：冻结 Catalog（4 policies）、5×4 explicit permission、
    ToolGovernanceService 注入同一生产 AgentRouter，startup 成功 READY。"""
    settings = _tmp_settings(monkeypatch, tmp_path, profile="LOCAL")
    monkeypatch.setattr(server, "settings", settings)
    monkeypatch.setattr(server, "VectorDBManager", _RaisingVectorDB)
    app = FastAPI()
    async with server.lifespan(app):
        router = app.state.chat_service.router
        service = router.tool_governance_service
        assert isinstance(service, ToolGovernanceService)
        catalog = service._catalog  # 测试 seam；Service 不暴露 catalog 是契约行为
        assert catalog.frozen is True
        assert len(catalog.policies()) == 4
        # 5×4 explicit ALLOW
        for registration in router.tool_registry.registrations():
            for agent_id in PRODUCTION_AGENT_IDS:
                decision = service.authorize_tool(
                    ToolGovernanceContext(agent_id, "run", "step"),
                    registration,
                )
                assert decision.outcome is ToolGovernanceOutcome.ALLOW, (
                    registration.descriptor.name,
                    agent_id,
                    decision,
                )
        assert app.state.runtime_lifecycle_state is RuntimeLifecycleState.READY


@pytest.mark.asyncio
async def test_governance_missing_policy_blocks_startup(
    monkeypatch, tmp_path
) -> None:
    """任一生产 Tool 缺 policy -> Catalog freeze 失败 -> startup fail，never READY。"""
    settings = _tmp_settings(monkeypatch, tmp_path, profile="LOCAL")
    monkeypatch.setattr(server, "settings", settings)
    monkeypatch.setattr(server, "VectorDBManager", _RaisingVectorDB)

    def partial_policies(catalog: ToolPolicyCatalog) -> None:
        # 只注册 3 条 policy，缺 complex_workflow_simulator -> freeze fail。
        for tool_name in (
            "list_files",
            "analyze_excel",
            "get_system_status",
        ):
            catalog.register(
                ToolPolicy(
                    tool_name=tool_name,
                    allowed_agent_ids=frozenset(PRODUCTION_AGENT_IDS),
                )
            )

    monkeypatch.setattr(
        server, "register_default_tool_policies", partial_policies
    )
    app = FastAPI()
    with pytest.raises(RuntimeInitializationError) as captured:
        async with server.lifespan(app):
            pass
    assert captured.value.component == "tool_governance"
    assert app.state.runtime_lifecycle_state is RuntimeLifecycleState.STARTING


# ---- Runtime knob wiring（结构回归）----

def test_lifespan_wires_approved_knobs_from_settings() -> None:
    source = inspect.getsource(server.lifespan)
    for attribute in (
        "blocking_max_workers",
        "blocking_max_pending_tasks",
        "event_channel_capacity",
        "planning_timeout_seconds",
        "step_result_per_result_chars",
        "step_result_run_total_chars",
        "step_result_max_entries",
    ):
        assert f"settings.{attribute}" in source


def test_settings_has_no_max_concurrency_field() -> None:
    assert "max_concurrency" not in {
        field.name for field in Settings.__dataclass_fields__.values()
    }


def test_runtime_factory_default_max_concurrency_remains_two() -> None:
    parameters = inspect.signature(CoordinatedRuntimeFactory.__init__).parameters
    assert parameters["max_concurrency"].default == 2


def test_lifespan_wires_remote_trust_env() -> None:
    assert "trust_env=settings.remote_trust_env" in inspect.getsource(server.lifespan)


def test_lifespan_gates_kb_on_knowledge_base_required() -> None:
    assert "settings.knowledge_base_required" in inspect.getsource(server.lifespan)


def test_lifespan_publishes_application_metadata() -> None:
    assert "app.state.application_metadata" in inspect.getsource(server.lifespan)


# ---- deprecated surfaces ----

def test_deprecated_observability_timeout_warns_without_value(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_AGENT_OBSERVABILITY_SHUTDOWN_TIMEOUT_SECONDS", "7")
    with pytest.warns(DeprecationWarning) as record:
        settings = Settings.load()
    # 仍保留字段并严格解析，但无行为接线。
    assert settings.observability_shutdown_timeout_seconds == 7
    messages = [str(item.message) for item in record]
    assert any(
        "LOCAL_AGENT_OBSERVABILITY_SHUTDOWN_TIMEOUT_SECONDS" in message
        for message in messages
    )
    assert all("= 7" not in message for message in messages)


def test_chat_service_capacity_shim_warns_when_explicit() -> None:
    with pytest.warns(DeprecationWarning):
        ChatService(object(), event_channel_capacity=1)  # type: ignore[arg-type]


def test_chat_service_capacity_default_does_not_warn() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        ChatService(object())  # type: ignore[arg-type]


def test_chat_service_does_not_expose_second_capacity_owner() -> None:
    # ChatService 参数是 ignored shim；真实 capacity 只经 Factory 消费。
    assert "LOCAL_AGENT_EVENT_CHANNEL_CAPACITY" in inspect.getsource(
        ChatService.__init__
    )
    factory = inspect.getsource(server.lifespan)
    assert "event_channel_capacity=settings.event_channel_capacity" in factory


# ---- fault production isolation 回归 ----

def test_settings_still_have_no_fault_surface() -> None:
    fields = {field.name.lower() for field in Settings.__dataclass_fields__.values()}
    assert not any("fault" in name or "chaos" in name for name in fields)


# ---- WP1-D persistence preflight startup integration ----

def test_persistence_preflight_precedes_memory_constructor() -> None:
    source = inspect.getsource(server.lifespan)
    preflight_call = source.index("run_persistence_preflight")
    memory_call = source.index("MemoryManager(")
    assert preflight_call < memory_call
    # preflight 在首个 resource 构造之前（role validation 之后）
    assert source.index("RuntimeInitializationStack()") < preflight_call


def _memory_legacy_sql(db_path: Path) -> None:
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                metadata TEXT
            );
            CREATE TABLE conversation_summaries (
                agent_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                last_message_id INTEGER NOT NULL DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX idx_messages_agent_time
                ON messages(agent_id, timestamp DESC, id DESC);
            CREATE INDEX idx_messages_timestamp
                ON messages(timestamp DESC, id DESC);
            """
        )


def _memory_columns(db_path: Path) -> frozenset[str]:
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        return frozenset(
            row[1] for row in conn.execute("PRAGMA table_info(messages)")
        )


@pytest.mark.asyncio
async def test_memory_migration_required_blocks_ready(monkeypatch, tmp_path) -> None:
    settings = _tmp_settings(monkeypatch, tmp_path, profile="LOCAL")
    db_path = Path(settings.memory_db_path)
    _memory_legacy_sql(db_path)
    monkeypatch.setattr(server, "settings", settings)
    app = FastAPI()
    with pytest.raises(RuntimeInitializationError) as captured:
        async with server.lifespan(app):
            pass
    assert captured.value.component == "persistence_preflight"
    assert app.state.runtime_lifecycle_state is RuntimeLifecycleState.STARTING
    # constructor 未执行 mutation：legacy 保持 legacy（列未被补加）
    assert "memory_scope" not in _memory_columns(db_path)


@pytest.mark.asyncio
async def test_memory_unsupported_blocks_ready(monkeypatch, tmp_path) -> None:
    import sqlite3

    settings = _tmp_settings(monkeypatch, tmp_path, profile="LOCAL")
    db_path = Path(settings.memory_db_path)
    from core.memory_manager import MemoryManager

    manager = MemoryManager(db_path=str(db_path))
    manager.add_message("a", "user", "hello")
    with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA user_version = 4")
    monkeypatch.setattr(server, "settings", settings)
    app = FastAPI()
    with pytest.raises(RuntimeInitializationError) as captured:
        async with server.lifespan(app):
            pass
    assert captured.value.component == "persistence_preflight"
    assert app.state.runtime_lifecycle_state is RuntimeLifecycleState.STARTING


@pytest.mark.asyncio
async def test_journal_migration_required_blocks_ready(monkeypatch, tmp_path) -> None:
    import sqlite3

    settings = _tmp_settings(monkeypatch, tmp_path, profile="LOCAL")
    journal_path = Path(settings.event_journal_db_path)
    with sqlite3.connect(journal_path) as conn:
        conn.executescript(
            """
            CREATE TABLE runtime_event_journal (
                journal_schema_version INTEGER NOT NULL,
                event_schema_version INTEGER NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                emitted_at TEXT NOT NULL,
                journaled_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                component TEXT NOT NULL,
                step_id TEXT,
                step_sequence INTEGER,
                safe_payload TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                event_digest TEXT NOT NULL,
                PRIMARY KEY (run_id, sequence)
            );
            CREATE INDEX idx_runtime_event_journal_run_type
                ON runtime_event_journal(run_id, event_type);
            """
        )
    monkeypatch.setattr(server, "settings", settings)
    app = FastAPI()
    with pytest.raises(RuntimeInitializationError) as captured:
        async with server.lifespan(app):
            pass
    assert captured.value.component == "persistence_preflight"
    assert app.state.runtime_lifecycle_state is RuntimeLifecycleState.STARTING


@pytest.mark.asyncio
async def test_checkpoint_incompatible_blocks_ready(monkeypatch, tmp_path) -> None:
    import sqlite3

    settings = _tmp_settings(monkeypatch, tmp_path, profile="LOCAL")
    checkpoint_path = Path(settings.observability_checkpoint_db_path)
    with sqlite3.connect(checkpoint_path) as conn:
        conn.execute(
            """
            CREATE TABLE event_consumption_checkpoint (
                consumer_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                processed_at TEXT NOT NULL
            )
            """
        )
    monkeypatch.setattr(server, "settings", settings)
    app = FastAPI()
    with pytest.raises(RuntimeInitializationError) as captured:
        async with server.lifespan(app):
            pass
    assert captured.value.component == "persistence_preflight"
    assert app.state.runtime_lifecycle_state is RuntimeLifecycleState.STARTING


@pytest.mark.asyncio
async def test_quick_check_failed_blocks_ready(monkeypatch, tmp_path) -> None:
    settings = _tmp_settings(monkeypatch, tmp_path, profile="LOCAL")
    db_path = Path(settings.memory_db_path)
    db_path.write_bytes(b"NOT A SQLITE DATABASE" * 8)
    monkeypatch.setattr(server, "settings", settings)
    app = FastAPI()
    with pytest.raises(RuntimeInitializationError) as captured:
        async with server.lifespan(app):
            pass
    assert captured.value.component == "persistence_preflight"
    assert app.state.runtime_lifecycle_state is RuntimeLifecycleState.STARTING


@pytest.mark.asyncio
async def test_all_current_preflight_reaches_ready_and_sets_memory_v3(
    monkeypatch, tmp_path
) -> None:
    """happy path：全新/current persistence → READY，且 constructor 新建 v2。"""
    import sqlite3

    settings = _tmp_settings(monkeypatch, tmp_path, profile="LOCAL")
    monkeypatch.setattr(server, "settings", settings)
    app = FastAPI()
    async with server.lifespan(app):
        assert app.state.runtime_lifecycle_state is RuntimeLifecycleState.READY
        assert (
            app.state.runtime_services.startup_dependency_snapshot
            .knowledge_base_degraded
            is True
        )
    with sqlite3.connect(settings.memory_db_path) as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 3


# ---- WP1-D P1 remediation：malformed physical signature startup fail-closed ----

def _malformed_memory_bytes(path: Path) -> bytes:
    import sqlite3

    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchone()[0].encode("utf-8")


@pytest.mark.asyncio
async def test_malformed_memory_blocks_ready_and_not_repaired(
    monkeypatch, tmp_path
) -> None:
    from test_persistence_preflight import _memory_malformed_constraints

    settings = _tmp_settings(monkeypatch, tmp_path, profile="LOCAL")
    db_path = Path(settings.memory_db_path)
    _memory_malformed_constraints(db_path)
    before = _malformed_memory_bytes(db_path)
    monkeypatch.setattr(server, "settings", settings)
    app = FastAPI()
    with pytest.raises(RuntimeInitializationError) as captured:
        async with server.lifespan(app):
            pass
    assert captured.value.component == "persistence_preflight"
    assert app.state.runtime_lifecycle_state is RuntimeLifecycleState.STARTING
    # constructor 未修复：messages 表 SQL 保持不变
    assert _malformed_memory_bytes(db_path) == before


@pytest.mark.asyncio
async def test_malformed_journal_blocks_ready(monkeypatch, tmp_path) -> None:
    from test_persistence_preflight import _journal_malformed

    settings = _tmp_settings(monkeypatch, tmp_path, profile="LOCAL")
    journal_path = Path(settings.event_journal_db_path)
    _journal_malformed(journal_path)
    before = journal_path.read_bytes()
    monkeypatch.setattr(server, "settings", settings)
    app = FastAPI()
    with pytest.raises(RuntimeInitializationError) as captured:
        async with server.lifespan(app):
            pass
    assert captured.value.component == "persistence_preflight"
    assert app.state.runtime_lifecycle_state is RuntimeLifecycleState.STARTING
    assert journal_path.read_bytes() == before


@pytest.mark.asyncio
async def test_malformed_snapshot_enabled_blocks_ready(monkeypatch, tmp_path) -> None:
    from test_persistence_preflight import _snapshot_malformed

    settings = _tmp_settings(monkeypatch, tmp_path, profile="LOCAL")
    monkeypatch.setenv("LOCAL_AGENT_SNAPSHOT_ENABLED", "true")
    settings = Settings.load()
    snapshot_path = Path(settings.snapshot_store_db_path)
    _snapshot_malformed(snapshot_path)
    before = snapshot_path.read_bytes()
    monkeypatch.setattr(server, "settings", settings)
    app = FastAPI()
    with pytest.raises(RuntimeInitializationError) as captured:
        async with server.lifespan(app):
            pass
    assert captured.value.component == "persistence_preflight"
    assert app.state.runtime_lifecycle_state is RuntimeLifecycleState.STARTING
    assert snapshot_path.read_bytes() == before


# ---- WP1-D second P1 remediation：semantic UNIQUE startup fail-closed ----

@pytest.mark.asyncio
async def test_memory_missing_run_id_unique_blocks_ready(monkeypatch, tmp_path) -> None:
    """Re-Gate reproduction：message_exchanges.run_id 缺 UNIQUE → preflight
    UNSUPPORTED → never READY → DB 保持未修改。"""
    import gc

    from test_persistence_preflight import _memory_missing_run_id_unique

    settings = _tmp_settings(monkeypatch, tmp_path, profile="LOCAL")
    db_path = Path(settings.memory_db_path)
    _memory_missing_run_id_unique(db_path)
    # Test isolation 加固：helper 内 `with sqlite3.connect(...)` 不关闭连接，
    # WAL 帧只有在连接被 GC 时才 checkpoint 进主文件。快照前强制 collect，
    # 使 before/after 字节比较确定，不受套件 GC 时序影响（不弱化断言）。
    gc.collect()
    before = db_path.read_bytes()
    monkeypatch.setattr(server, "settings", settings)
    app = FastAPI()
    with pytest.raises(RuntimeInitializationError) as captured:
        async with server.lifespan(app):
            pass
    assert captured.value.component == "persistence_preflight"
    assert app.state.runtime_lifecycle_state is RuntimeLifecycleState.STARTING
    assert db_path.read_bytes() == before


@pytest.mark.asyncio
async def test_journal_extra_unique_blocks_ready(monkeypatch, tmp_path) -> None:
    """Journal 额外 UNIQUE(trace_id) → preflight UNSUPPORTED → never READY。"""
    import sqlite3

    settings = _tmp_settings(monkeypatch, tmp_path, profile="LOCAL")
    journal_path = Path(settings.event_journal_db_path)
    with sqlite3.connect(journal_path) as conn:
        conn.executescript(
            """
            CREATE TABLE runtime_event_journal (
                journal_schema_version INTEGER NOT NULL,
                event_schema_version INTEGER NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                emitted_at TEXT NOT NULL,
                journaled_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                component TEXT NOT NULL,
                step_id TEXT,
                step_sequence INTEGER,
                span_id TEXT,
                parent_span_id TEXT,
                safe_payload TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                event_digest TEXT NOT NULL,
                PRIMARY KEY (run_id, sequence)
            );
            CREATE INDEX idx_runtime_event_journal_run_type
                ON runtime_event_journal(run_id, event_type);
            CREATE UNIQUE INDEX j_extra_trace ON runtime_event_journal(trace_id);
            """
        )
    before = journal_path.read_bytes()
    monkeypatch.setattr(server, "settings", settings)
    app = FastAPI()
    with pytest.raises(RuntimeInitializationError) as captured:
        async with server.lifespan(app):
            pass
    assert captured.value.component == "persistence_preflight"
    assert app.state.runtime_lifecycle_state is RuntimeLifecycleState.STARTING
    assert journal_path.read_bytes() == before


# ---- WP4-C: AgentEvalOps trace export 最小配置与 lifespan 接线 -------------

def test_agentevalops_trace_export_defaults_disabled(monkeypatch) -> None:
    for key in (
        "LOCAL_AGENT_AGENTEVALOPS_TRACE_EXPORT_ENABLED",
        "LOCAL_AGENT_AGENTEVALOPS_BASE_URL",
        "LOCAL_AGENT_AGENTEVALOPS_API_KEY",
        "LOCAL_AGENT_AGENTEVALOPS_PROJECT_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = _load(monkeypatch)
    assert settings.agentevalops_trace_export_enabled is False
    assert settings.agentevalops_base_url == ""
    assert settings.agentevalops_api_key == ""
    assert settings.agentevalops_project_id == ""
    assert settings.agentevalops_connect_timeout_seconds == 0.5
    assert settings.agentevalops_total_deadline_seconds == 3.0
    assert "agentevalops_api_key" not in repr(settings)


def test_agentevalops_disabled_ignores_invalid_other_fields(monkeypatch) -> None:
    settings = _load(
        monkeypatch,
        LOCAL_AGENT_AGENTEVALOPS_TRACE_EXPORT_ENABLED="false",
        LOCAL_AGENT_AGENTEVALOPS_BASE_URL="not-a-url",
        LOCAL_AGENT_AGENTEVALOPS_TRACE_EXPORT_TOTAL_DEADLINE_SECONDS="not-a-number",
    )
    assert settings.agentevalops_trace_export_enabled is False
    assert settings.agentevalops_base_url == ""


def test_agentevalops_enabled_valid_config(monkeypatch) -> None:
    settings = _load(
        monkeypatch,
        LOCAL_AGENT_AGENTEVALOPS_TRACE_EXPORT_ENABLED="true",
        LOCAL_AGENT_AGENTEVALOPS_BASE_URL="http://127.0.0.1:8001",
        LOCAL_AGENT_AGENTEVALOPS_API_KEY="api-key-test-1",
        LOCAL_AGENT_AGENTEVALOPS_PROJECT_ID="project-test-1",
    )
    assert settings.agentevalops_trace_export_enabled is True
    assert settings.agentevalops_base_url == "http://127.0.0.1:8001"
    assert settings.agentevalops_api_key == "api-key-test-1"
    assert settings.agentevalops_project_id == "project-test-1"


def test_agentevalops_enabled_requires_base_url(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError) as captured:
        _load(
            monkeypatch,
            LOCAL_AGENT_AGENTEVALOPS_TRACE_EXPORT_ENABLED="true",
            LOCAL_AGENT_AGENTEVALOPS_API_KEY="api-key-test-1",
            LOCAL_AGENT_AGENTEVALOPS_PROJECT_ID="project-test-1",
        )
    assert captured.value.safe_error_code == STARTUP_CONFIGURATION_ERROR
    assert captured.value.field == "LOCAL_AGENT_AGENTEVALOPS_BASE_URL"


def test_agentevalops_enabled_requires_api_key_and_project(monkeypatch) -> None:
    for missing in ("LOCAL_AGENT_AGENTEVALOPS_API_KEY", "LOCAL_AGENT_AGENTEVALOPS_PROJECT_ID"):
        env = {
            "LOCAL_AGENT_AGENTEVALOPS_TRACE_EXPORT_ENABLED": "true",
            "LOCAL_AGENT_AGENTEVALOPS_BASE_URL": "http://127.0.0.1:8001",
            "LOCAL_AGENT_AGENTEVALOPS_API_KEY": "api-key-test-1",
            "LOCAL_AGENT_AGENTEVALOPS_PROJECT_ID": "project-test-1",
        }
        env[missing] = None
        with pytest.raises(SettingsValidationError) as captured:
            _load(monkeypatch, **env)
        assert captured.value.field == missing


def test_agentevalops_invalid_base_url_fails(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError) as captured:
        _load(
            monkeypatch,
            LOCAL_AGENT_AGENTEVALOPS_TRACE_EXPORT_ENABLED="true",
            LOCAL_AGENT_AGENTEVALOPS_BASE_URL="ftp://agent-eval.example",
            LOCAL_AGENT_AGENTEVALOPS_API_KEY="api-key-test-1",
            LOCAL_AGENT_AGENTEVALOPS_PROJECT_ID="project-test-1",
        )
    assert captured.value.field == "LOCAL_AGENT_AGENTEVALOPS_BASE_URL"
    assert captured.value.reason_code == "invalid_url"


def test_agentevalops_connect_exceeds_total_deadline_fails(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError) as captured:
        _load(
            monkeypatch,
            LOCAL_AGENT_AGENTEVALOPS_TRACE_EXPORT_ENABLED="true",
            LOCAL_AGENT_AGENTEVALOPS_BASE_URL="http://127.0.0.1:8001",
            LOCAL_AGENT_AGENTEVALOPS_API_KEY="api-key-test-1",
            LOCAL_AGENT_AGENTEVALOPS_PROJECT_ID="project-test-1",
            LOCAL_AGENT_AGENTEVALOPS_CONNECT_TIMEOUT_SECONDS="2.0",
            LOCAL_AGENT_AGENTEVALOPS_TRACE_EXPORT_TOTAL_DEADLINE_SECONDS="1.0",
        )
    assert captured.value.field == "LOCAL_AGENT_AGENTEVALOPS_CONNECT_TIMEOUT_SECONDS"
    assert captured.value.reason_code == "connect_exceeds_total_deadline"


def test_agentevalops_fractional_millisecond_fails(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError) as captured:
        _load(
            monkeypatch,
            LOCAL_AGENT_AGENTEVALOPS_TRACE_EXPORT_ENABLED="true",
            LOCAL_AGENT_AGENTEVALOPS_BASE_URL="http://127.0.0.1:8001",
            LOCAL_AGENT_AGENTEVALOPS_API_KEY="api-key-test-1",
            LOCAL_AGENT_AGENTEVALOPS_PROJECT_ID="project-test-1",
            LOCAL_AGENT_AGENTEVALOPS_CONNECT_TIMEOUT_SECONDS="0.0001",
        )
    assert captured.value.reason_code == "invalid_millisecond_conversion"


def test_agentevalops_deadline_invariant_against_close_timeout(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError) as captured:
        _load(
            monkeypatch,
            LOCAL_AGENT_AGENTEVALOPS_TRACE_EXPORT_ENABLED="true",
            LOCAL_AGENT_AGENTEVALOPS_BASE_URL="http://127.0.0.1:8001",
            LOCAL_AGENT_AGENTEVALOPS_API_KEY="api-key-test-1",
            LOCAL_AGENT_AGENTEVALOPS_PROJECT_ID="project-test-1",
            LOCAL_AGENT_AGENTEVALOPS_TRACE_EXPORT_TOTAL_DEADLINE_SECONDS="5.0",
        )
    assert captured.value.field == (
        "LOCAL_AGENT_AGENTEVALOPS_TRACE_EXPORT_TOTAL_DEADLINE_SECONDS"
    )
    assert captured.value.reason_code == "deadline_not_below_close_timeout"


def test_agentevalops_production_requires_https(monkeypatch, tmp_path) -> None:
    with pytest.raises(SettingsValidationError) as captured:
        _tmp_settings(
            monkeypatch,
            tmp_path,
            profile="PRODUCTION",
        )
        _load(
            monkeypatch,
            LOCAL_AGENT_ENVIRONMENT_PROFILE="PRODUCTION",
            LOCAL_AGENT_ENVIRONMENT_ID="prod-wp4c",
            LOCAL_AGENT_TOOL_ALLOWED_READ_ROOTS=str(tmp_path.resolve()),
            LOCAL_AGENT_AGENTEVALOPS_TRACE_EXPORT_ENABLED="true",
            LOCAL_AGENT_AGENTEVALOPS_BASE_URL="http://127.0.0.1:8001",
            LOCAL_AGENT_AGENTEVALOPS_API_KEY="api-key-test-1",
            LOCAL_AGENT_AGENTEVALOPS_PROJECT_ID="project-test-1",
        )
    assert captured.value.safe_error_code == SETTINGS_SECURITY_POLICY_ERROR
    assert captured.value.field == "LOCAL_AGENT_AGENTEVALOPS_BASE_URL"
    assert captured.value.reason_code == "production_requires_https"


def test_agentevalops_production_https_valid(monkeypatch, tmp_path) -> None:
    settings = _load(
        monkeypatch,
        LOCAL_AGENT_ENVIRONMENT_PROFILE="PRODUCTION",
        LOCAL_AGENT_ENVIRONMENT_ID="prod-wp4c",
        LOCAL_AGENT_TOOL_ALLOWED_READ_ROOTS=str(tmp_path.resolve()),
        LOCAL_AGENT_AGENTEVALOPS_TRACE_EXPORT_ENABLED="true",
        LOCAL_AGENT_AGENTEVALOPS_BASE_URL="https://agent-eval.example",
        LOCAL_AGENT_AGENTEVALOPS_API_KEY="api-key-test-1",
        LOCAL_AGENT_AGENTEVALOPS_PROJECT_ID="project-test-1",
    )
    assert settings.agentevalops_trace_export_enabled is True
    assert settings.agentevalops_base_url == "https://agent-eval.example"


def test_lifespan_wires_agentevalops_dispatcher_before_recorder() -> None:
    """enabled 路径：exporter → dispatcher → recorder(observer) 的构造顺序。"""
    source = inspect.getsource(server.lifespan)
    assert "agentevalops_trace_exporter" in source
    assert "trace_export_dispatcher" in source
    exporter_call = source.index("AgentEvalOpsTraceExporter(")
    dispatcher_call = source.index("TraceExportDispatcher(")
    recorder_call = source.index("InMemorySpanRecorder(")
    assert exporter_call < dispatcher_call < recorder_call
    assert "completion_observer=" in source
    assert "observe_completed_span" in source
    assert "trace_export_dispatcher=trace_export_dispatcher" in source


def test_lifespan_agentevalops_disabled_preserves_no_exporter() -> None:
    """disabled 路径：不构造 exporter/dispatcher（无 HTTP 依赖）。"""
    source = inspect.getsource(server.lifespan)
    assert "if settings.agentevalops_trace_export_enabled:" in source
    assert "completion_observer=" in source
    assert "trace_export_dispatcher.observe_completed_span" in source


def test_agentevalops_settings_no_fault_surface() -> None:
    fields = {field.name.lower() for field in Settings.__dataclass_fields__.values()}
    assert not any("fault" in name or "chaos" in name for name in fields)


# ---- WP6-E Layer1 runtime profile + scripted backend（G2 frozen contract）----


def test_runtime_profile_unknown_profile_fails_closed() -> None:
    """unknown runtime profile -> fail closed。"""
    with pytest.raises(SettingsValidationError, match="unknown_profile"):
        RuntimeProfile.parse("BOGUS_PROFILE")
    with pytest.raises(SettingsValidationError, match="invalid_profile"):
        RuntimeProfile.parse("  ")


def test_runtime_profile_production_default_unchanged(monkeypatch) -> None:
    """normal production profile behavior unchanged：默认 PRODUCTION，remote 仍需 URL。"""
    monkeypatch.delenv("LOCAL_AGENT_RUNTIME_PROFILE", raising=False)
    assert RuntimeProfile.parse(None) is RuntimeProfile.PRODUCTION
    settings = _load(monkeypatch, LOCAL_AGENT_REMOTE_API_BASE_URL=None)
    with pytest.raises(SettingsValidationError) as captured:
        validate_role_configuration(settings, role="SERVER")
    assert captured.value.safe_error_code == STARTUP_CONFIGURATION_ERROR


def test_layer1_profile_does_not_require_remote_url(monkeypatch) -> None:
    """Layer1 profile 不要求 remote API URL。"""
    settings = _load(
        monkeypatch,
        LOCAL_AGENT_RUNTIME_PROFILE="EPISODIC_EVALUATION_LAYER1",
        LOCAL_AGENT_LLM_BACKEND="scripted",
        LOCAL_AGENT_REMOTE_API_BASE_URL=None,
    )
    assert settings.runtime_profile is RuntimeProfile.EPISODIC_EVALUATION_LAYER1
    assert settings.llm_backend == "scripted"
    validate_role_configuration(settings, role="SERVER")  # 不抛 required_for_remote_backend


def test_layer1_profile_does_not_require_remote_key(monkeypatch) -> None:
    """Layer1 profile 不要求 remote API key。"""
    monkeypatch.delenv("LOCAL_AGENT_REMOTE_API_KEY", raising=False)
    settings = _load(
        monkeypatch,
        LOCAL_AGENT_RUNTIME_PROFILE="EPISODIC_EVALUATION_LAYER1",
        LOCAL_AGENT_LLM_BACKEND="scripted",
        LOCAL_AGENT_REMOTE_API_BASE_URL="https://example.test/v1",
    )
    validate_role_configuration(settings, role="SERVER")


def test_layer1_profile_forbids_provider_backend(monkeypatch) -> None:
    """Layer1 profile 强制 scripted backend，拒绝 remote/hybrid（zero network composition）。"""
    with pytest.raises(SettingsValidationError, match="evaluation_profile_forbids_provider_backend"):
        _load(
            monkeypatch,
            LOCAL_AGENT_RUNTIME_PROFILE="EPISODIC_EVALUATION_LAYER1",
            LOCAL_AGENT_LLM_BACKEND="remote",
            LOCAL_AGENT_REMOTE_API_BASE_URL="https://example.test/v1",
        )
    settings = _load(
        monkeypatch,
        LOCAL_AGENT_RUNTIME_PROFILE="EPISODIC_EVALUATION_LAYER1",
        LOCAL_AGENT_LLM_BACKEND=None,
        LOCAL_AGENT_REMOTE_API_BASE_URL="https://example.test/v1",
    )
    assert settings.llm_backend == "scripted"


def test_scripted_backend_requires_evaluation_profile(monkeypatch) -> None:
    """PRODUCTION profile 不允许 scripted backend（fail closed）。"""
    settings = _load(
        monkeypatch,
        LOCAL_AGENT_RUNTIME_PROFILE=None,
        LOCAL_AGENT_LLM_BACKEND="scripted",
        LOCAL_AGENT_REMOTE_API_BASE_URL="https://example.test/v1",
    )
    with pytest.raises(SettingsValidationError, match="scripted_backend_requires_evaluation_profile"):
        validate_role_configuration(settings, role="SERVER")


def test_startup_profile_not_request_switchable() -> None:
    """request 不能切换 startup profile：profile 只在 Settings.load 由 env 读取。"""
    source = inspect.getsource(Settings)
    assert "LOCAL_AGENT_RUNTIME_PROFILE" in source
    # ChatRequest / RuntimeExecuteRequest 没有 runtime_profile 字段（structural）。
    chat_fields = set(server.ChatRequest.model_fields)
    assert "runtime_profile" not in chat_fields
    assert "evaluation_profile" not in chat_fields


def test_layer1_profile_composition_builds_only_scripted_engine() -> None:
    """Layer1 scripted backend 只构造 ScriptedEvaluationLLMEngine，不构造 RemoteLLMEngine。"""
    source = inspect.getsource(server.lifespan)
    assert "ScriptedEvaluationLLMEngine" in source
    assert "RemoteLLMEngine(" in source
    assert source.index('if settings.llm_backend == "scripted"') < source.index(
        'if settings.llm_backend in {"local", "hybrid"}'
    )
    assert source.index('if settings.llm_backend in {"local", "hybrid"}') < source.index(
        'if settings.llm_backend in {"remote", "hybrid"}'
    )


def test_layer1_scripted_profile_supports_specialist_and_synthesis_capabilities() -> None:
    """脚本评估 profile 可执行 code_expert 与 structured synthesis，生产分支不变。"""
    source = inspect.getsource(server.lifespan)
    scripted_branch = source.split('if settings.llm_backend == "scripted"', 1)[1].split(
        'if settings.llm_backend in {"local", "hybrid"}', 1
    )[0]
    assert "False, True, True, False, 1, 1" in scripted_branch


def test_scripted_engine_does_not_perform_network_io() -> None:
    """ScriptedEvaluationLLMEngine.generate 是纯确定性生成器，不 import/call 任何 HTTP client。"""
    import inspect as _inspect

    engine = ScriptedEvaluationLLMEngine()
    messages = [
        {"role": "system", "content": "LocalAgent Planner"},
        {"role": "user", "content": "整理发布清单"},
    ]
    output = list(engine.generate(messages))
    assert output and all(isinstance(item, str) and item.strip() for item in output)
    source = _inspect.getsource(ScriptedEvaluationLLMEngine)
    for forbidden in ("httpx", "requests", "socket", "urllib"):
        assert forbidden not in source


def test_repeated_scripted_inputs_deterministic() -> None:
    """repeated scripted inputs deterministic：同输入 -> 同输出序列。"""
    engine = ScriptedEvaluationLLMEngine()
    messages = [
        {"role": "system", "content": "LocalAgent Planner"},
        {"role": "user", "content": "整理项目生产环境的发布清单"},
    ]
    first = list(engine.generate(messages))
    for _ in range(3):
        assert list(engine.generate(messages)) == first
    semantic = [
        {"role": "system", "content": "长期记忆候选提取器"},
        {"role": "user", "content": "x"},
    ]
    assert list(engine.generate(semantic)) == ['{"schema_version":1,"candidates":[]}']


def test_scripted_planner_uses_compilable_synthesis_for_code_expert() -> None:
    """单个 code_expert task 仍须满足 PlanCompiler 的 synthesis 约束。"""
    engine = ScriptedEvaluationLLMEngine()
    output = "".join(
        engine.generate(
            [
                {"role": "system", "content": "LocalAgent Planner"},
                    {"role": "user", "content": "项目生产环境的部署方式是什么"},
            ]
        )
    )
    assert '"task_id":"deploy_probe"' in output
    assert '"synthesis_required":true' in output


def test_scripted_profile_routes_bounded_request_semantics_without_scenario_id() -> None:
    engine = ScriptedEvaluationLLMEngine()
    cases = {
        "整理发布清单和回滚方案": ('"task_id":"release_list"', '"task_id":"rollback_plan"'),
        "核对数据库配置及备份策略": ('"task_id":"config_check"', '"task_id":"backup_review"'),
        "mysql 主从复制中断的恢复流程是什么": ('"task_id":"recovery_summary"',),
        "检查部署环境的状态并汇报结果": ('"task_id":"env_status"',),
    }
    for request, expected_task_ids in cases.items():
        output = "".join(engine.generate([{"role": "system", "content": "LocalAgent Planner"}, {"role": "user", "content": request}]))
        assert all(task_id in output for task_id in expected_task_ids)
        assert "E0" not in output and "scenario" not in output.lower()


def test_scripted_profile_returns_no_tool_for_tool_planner() -> None:
    engine = ScriptedEvaluationLLMEngine()
    assert list(engine.generate([{"role": "system", "content": "无需工具时仅输出 NO_TOOL"}, {"role": "user", "content": "x"}])) == ["NO_TOOL"]
