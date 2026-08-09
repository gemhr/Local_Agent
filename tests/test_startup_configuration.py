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
from core.settings import (
    SETTINGS_SECURITY_POLICY_ERROR,
    STARTUP_CONFIGURATION_ERROR,
    Settings,
    SettingsValidationError,
    validate_role_configuration,
)


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


def _tmp_settings(monkeypatch, tmp_path, *, profile="LOCAL", kb_required=None):
    env = {
        "LOCAL_AGENT_ENVIRONMENT_PROFILE": profile,
        "LOCAL_AGENT_REMOTE_API_BASE_URL": "https://example.test/v1",
        "LOCAL_AGENT_MEMORY_DB_PATH": str(tmp_path / "memory.db"),
        "LOCAL_AGENT_EVENT_JOURNAL_DB_PATH": str(tmp_path / "journal.db"),
        "LOCAL_AGENT_SNAPSHOT_DB_PATH": str(tmp_path / "snap.db"),
        "LOCAL_AGENT_OBSERVABILITY_CHECKPOINT_DB_PATH": str(tmp_path / "obs.db"),
        "LOCAL_AGENT_CHROMA_DIR": str(tmp_path / "chroma"),
    }
    if profile == "PRODUCTION":
        env["LOCAL_AGENT_ENVIRONMENT_ID"] = "prod-integration"
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
    monkeypatch.setenv("LOCAL_AGENT_EMBEDDING_MODEL_PATH", str(_EMBEDDING_MODEL_DIR))
    settings = _tmp_settings(monkeypatch, tmp_path, profile="LOCAL")
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
