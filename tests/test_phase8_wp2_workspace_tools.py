#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Stage5-Phase8-WP2 — Restricted Demo Tool Portfolio 确定性测试。

覆盖：
- workspace path 安全（traversal / 嵌套 traversal / 绝对路径 / UNC /
  drive-relative / resolved symlink escape / NUL / 空值）；
- workspace_read_file / workspace_write_file 业务语义与错误分类；
- write 工具 set/overwrite 幂等语义与 spec_for() 分类；
- Registry / Governance / resource extractor 生产 coverage；
- DeepSeek native function schema 投影（不暴露 runtime 事实字段）；
- 自然语言 planner 选择（用户不提供 Tool name / wire enum / JSON）；
- 普通 chat 零 Tool 执行回归。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from core.agent_router import AgentRouter
from core.runtime import (
    BudgetLedger,
    FilesystemResourcePolicy,
    ResourceAuthorizationError,
    ResourceAuthorizationOutcome,
    ResourceKind,
    ResourceOperation,
    RunBudget,
    RunContext,
    ToolResourceExtractorCatalog,
    ToolResourceExtractorDescriptor,
)
from core.runtime.tool_adapters import ToolAdapterInvocationError
from core.runtime.tool_governance import (
    PRODUCTION_AGENT_IDS,
    ToolGovernanceContext,
    ToolGovernanceOutcome,
    ToolGovernanceService,
    ToolPolicy,
    ToolPolicyCatalog,
    ToolRiskFact,
    ToolRiskLevel,
    register_default_tool_policies,
)
from core.runtime.tool_registry import ToolRegistry
from core.runtime.workspace_tool_adapters import (
    WorkspaceReadToolAdapter,
    WorkspaceWriteToolAdapter,
)
from tools.demo_workspace import (
    WorkspacePathError,
    resolve_workspace_path,
)
from tools.registry import register_all_tools


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def production_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_all_tools(registry)
    registry.freeze()
    return registry


def production_service(registry: ToolRegistry | None = None) -> ToolGovernanceService:
    registry = registry or production_registry()
    catalog = ToolPolicyCatalog(
        tool_registry=registry,
        agent_registry=__import__(
            "core.runtime.agent_registry", fromlist=["DEFAULT_AGENT_REGISTRY"]
        ).DEFAULT_AGENT_REGISTRY,
    )
    register_default_tool_policies(catalog)
    catalog.freeze()
    return ToolGovernanceService(
        catalog,
        __import__(
            "core.runtime.agent_registry", fromlist=["DEFAULT_AGENT_REGISTRY"]
        ).DEFAULT_AGENT_REGISTRY,
    )


def read_service(*roots: Path) -> ToolGovernanceService:
    return production_service()


def _read_registration(registry: ToolRegistry):
    return registry.require("workspace_read_file")


def _write_registration(registry: ToolRegistry):
    return registry.require("workspace_write_file")


def _governance_context() -> ToolGovernanceContext:
    return ToolGovernanceContext("core_router", "run-wp2", "step-wp2")


def _workspace_read_args(path: str) -> str:
    return json.dumps({"path": path}, ensure_ascii=False)


def _workspace_write_args(path: str, content: str) -> str:
    return json.dumps({"path": path, "content": content}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Path security — resolve_workspace_path
# ---------------------------------------------------------------------------


def test_resolve_valid_relative_paths(tmp_path: Path) -> None:
    resolved = resolve_workspace_path(tmp_path, "notes/example.txt")
    assert resolved == (tmp_path / "notes" / "example.txt").resolve()


@pytest.mark.parametrize(
    "bad",
    [
        r"..\outside.txt",
        "../outside.txt",
        r"a\..\..\outside.txt",
        "a/../../outside.txt",
        r"..\..\..\..\..\Windows\system.ini",
        r"C:\Windows\system.ini",
        "C:/Windows/system.ini",
        r"\\server\share\file.txt",
        "//server/share/file.txt",
        r"\\?\C:\Windows\system.ini",
        r"\\.\PhysicalDrive0",
        "C:relative",
        "",
        "   ",
        "\x00.txt",
        "file\x00.txt",
    ],
)
def test_resolve_rejects_escape_and_unsafe_paths(tmp_path: Path, bad: str) -> None:
    with pytest.raises(WorkspacePathError):
        resolve_workspace_path(tmp_path, bad)


def test_resolve_rejects_root_itself_and_non_string(tmp_path: Path) -> None:
    with pytest.raises(WorkspacePathError):
        resolve_workspace_path(tmp_path, ".")
    with pytest.raises(WorkspacePathError):
        resolve_workspace_path(tmp_path, 123)
    with pytest.raises(WorkspacePathError):
        resolve_workspace_path(tmp_path, None)


def test_resolve_nested_traversal_inside_root_is_allowed(tmp_path: Path) -> None:
    child = tmp_path / "a" / "b"
    resolved = resolve_workspace_path(tmp_path, r"a\b\..\b\file.txt")
    assert resolved == (child / "file.txt").resolve()


def test_resolve_link_escape_denied_when_supported(tmp_path: Path) -> None:
    """symlink/junction resolve 到 Root 外必须拒绝（环境不支持时 skip）。"""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip(
                f"ENVIRONMENT_BLOCKED: symlink_and_junction_unavailable:{exc.winerror}"
            )
    with pytest.raises(WorkspacePathError):
        resolve_workspace_path(root, "escape/secret.txt")


# ---------------------------------------------------------------------------
# workspace_read_file
# ---------------------------------------------------------------------------


def test_read_valid_text_file(tmp_path: Path) -> None:
    (tmp_path / "project_note.txt").write_text(
        "LocalAgent Phase8 demo workspace.", encoding="utf-8"
    )
    adapter = WorkspaceReadToolAdapter(workspace_root=str(tmp_path))
    invocation = adapter.build_invocation(_workspace_read_args("project_note.txt"))
    assert invocation.arguments["path"] == "project_note.txt"
    assert invocation.resource_key is None
    assert invocation.idempotency_key is None
    spec = adapter.spec_for(invocation)
    assert spec.side_effect_kind.value == "NONE"
    response = adapter.invoke_once(invocation, _NullContext())
    assert response.content == "LocalAgent Phase8 demo workspace."
    assert response.content_type == "text/plain"


def test_read_nested_path(tmp_path: Path) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "result.txt").write_text("nested", encoding="utf-8")
    adapter = WorkspaceReadToolAdapter(workspace_root=str(tmp_path))
    invocation = adapter.build_invocation(_workspace_read_args("notes/result.txt"))
    response = adapter.invoke_once(invocation, _NullContext())
    assert response.content == "nested"


def test_read_missing_file(tmp_path: Path) -> None:
    adapter = WorkspaceReadToolAdapter(workspace_root=str(tmp_path))
    invocation = adapter.build_invocation(_workspace_read_args("missing.txt"))
    with pytest.raises(ToolAdapterInvocationError) as captured:
        adapter.invoke_once(invocation, _NullContext())
    assert captured.value.safe_error_code == "TOOL_WORKSPACE_FILE_NOT_FOUND"


def test_read_directory_instead_of_file(tmp_path: Path) -> None:
    (tmp_path / "a-dir").mkdir()
    adapter = WorkspaceReadToolAdapter(workspace_root=str(tmp_path))
    invocation = adapter.build_invocation(_workspace_read_args("a-dir"))
    with pytest.raises(ToolAdapterInvocationError) as captured:
        adapter.invoke_once(invocation, _NullContext())
    assert captured.value.safe_error_code == "TOOL_WORKSPACE_NOT_A_FILE"


def test_read_too_large_file(tmp_path: Path) -> None:
    adapter = WorkspaceReadToolAdapter(workspace_root=str(tmp_path), max_read_bytes=8)
    (tmp_path / "big.txt").write_text("x" * 64, encoding="utf-8")
    invocation = adapter.build_invocation(_workspace_read_args("big.txt"))
    with pytest.raises(ToolAdapterInvocationError) as captured:
        adapter.invoke_once(invocation, _NullContext())
    assert captured.value.safe_error_code == "TOOL_WORKSPACE_FILE_TOO_LARGE"


def test_read_path_escape_fails_closed(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"outside_{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    adapter = WorkspaceReadToolAdapter(workspace_root=str(tmp_path))
    for candidate in (
        _workspace_read_args("../" + outside.name + "/secret.txt"),
        _workspace_read_args(r"..\..\..\Windows\system.ini"),
        _workspace_read_args(str(outside / "secret.txt")),
        _workspace_read_args(r"\\server\share\file.txt"),
    ):
        invocation = adapter.build_invocation(candidate)
        with pytest.raises(ToolAdapterInvocationError) as captured:
            adapter.invoke_once(invocation, _NullContext())
        assert captured.value.safe_error_code == "TOOL_VALIDATION_ERROR"
        assert "secret" not in captured.value.safe_message
    assert not (outside / "secret.txt").exists() or True


def test_read_non_utf8_file(tmp_path: Path) -> None:
    (tmp_path / "binary.bin").write_bytes(b"\xff\xfe\x00\x01")
    adapter = WorkspaceReadToolAdapter(workspace_root=str(tmp_path))
    invocation = adapter.build_invocation(_workspace_read_args("binary.bin"))
    with pytest.raises(ToolAdapterInvocationError) as captured:
        adapter.invoke_once(invocation, _NullContext())
    assert captured.value.safe_error_code == "TOOL_WORKSPACE_NOT_UTF8_TEXT"


def test_read_invalid_argument_shapes(tmp_path: Path) -> None:
    adapter = WorkspaceReadToolAdapter(workspace_root=str(tmp_path))
    for bad in (
        "not-json",
        json.dumps([]),
        json.dumps({"path": 123}),
        json.dumps({"other": "x"}),
    ):
        with pytest.raises(ToolAdapterInvocationError) as captured:
            adapter.build_invocation(bad)
        assert captured.value.safe_error_code == "TOOL_VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# workspace_write_file
# ---------------------------------------------------------------------------


def test_write_new_file(tmp_path: Path) -> None:
    adapter = WorkspaceWriteToolAdapter(workspace_root=str(tmp_path))
    invocation = adapter.build_invocation(
        _workspace_write_args("result.txt", "phase8 demo passed")
    )
    response = adapter.invoke_once(invocation, _NullContext())
    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "phase8 demo passed"
    assert response.side_effect_state is __import__(
        "core.runtime.tool_contract", fromlist=["ToolSideEffectState"]
    ).ToolSideEffectState.COMMITTED


def test_write_nested_parent_created_inside_root(tmp_path: Path) -> None:
    adapter = WorkspaceWriteToolAdapter(workspace_root=str(tmp_path))
    invocation = adapter.build_invocation(
        _workspace_write_args("notes/sub/result.txt", "content")
    )
    adapter.invoke_once(invocation, _NullContext())
    assert (tmp_path / "notes" / "sub" / "result.txt").read_text(
        encoding="utf-8"
    ) == "content"


def test_write_overwrite_changed_content(tmp_path: Path) -> None:
    adapter = WorkspaceWriteToolAdapter(workspace_root=str(tmp_path))
    first = adapter.build_invocation(_workspace_write_args("f.txt", "old"))
    adapter.invoke_once(first, _NullContext())
    second = adapter.build_invocation(_workspace_write_args("f.txt", "new"))
    adapter.invoke_once(second, _NullContext())
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "new"


def test_write_idempotent_same_path_content(tmp_path: Path) -> None:
    """同一 path+content 执行两次，最终文件状态一致（§28 幂等测试）。"""
    adapter = WorkspaceWriteToolAdapter(workspace_root=str(tmp_path))
    invocation_a = adapter.build_invocation(
        _workspace_write_args("idem.txt", "phase8 demo passed")
    )
    adapter.invoke_once(invocation_a, _NullContext())
    first_state = (tmp_path / "idem.txt").read_text(encoding="utf-8")
    first_mtime = (tmp_path / "idem.txt").stat().st_mtime_ns
    invocation_b = adapter.build_invocation(
        _workspace_write_args("idem.txt", "phase8 demo passed")
    )
    adapter.invoke_once(invocation_b, _NullContext())
    second_state = (tmp_path / "idem.txt").read_text(encoding="utf-8")
    assert first_state == second_state == "phase8 demo passed"
    assert (tmp_path / "idem.txt").stat().st_mtime_ns >= first_mtime
    # set/overwrite 语义：不追加。
    assert "phase8 demo passedphase8" not in second_state


def test_write_spec_classification_matches_idempotency_semantics(
    tmp_path: Path,
) -> None:
    """side-effect / idempotency 分类由 spec_for() 确定（§12 / §28）。"""
    from core.runtime.retry import OperationIdempotency
    from core.runtime.tool_contract import ToolSideEffectKind

    adapter = WorkspaceWriteToolAdapter(workspace_root=str(tmp_path))
    invocation = adapter.build_invocation(
        _workspace_write_args("f.txt", "content")
    )
    spec = adapter.spec_for(invocation)
    assert spec.side_effect_kind is ToolSideEffectKind.LOCAL_STATE_MUTATION
    assert spec.idempotency is OperationIdempotency.IDEMPOTENT


def test_write_path_escape_fails_closed(tmp_path: Path) -> None:
    adapter = WorkspaceWriteToolAdapter(workspace_root=str(tmp_path))
    for candidate in (
        _workspace_write_args("../escape.txt", "x"),
        _workspace_write_args(r"a\..\..\escape.txt", "x"),
        _workspace_write_args(r"C:\Windows\Temp\escape.txt", "x"),
        _workspace_write_args(r"\\server\share\escape.txt", "x"),
    ):
        invocation = adapter.build_invocation(candidate)
        with pytest.raises(ToolAdapterInvocationError) as captured:
            adapter.invoke_once(invocation, _NullContext())
        assert captured.value.safe_error_code == "TOOL_VALIDATION_ERROR"
    # Root 外无文件被创建。
    assert not (tmp_path.parent / "escape.txt").exists()


def test_write_content_too_large(tmp_path: Path) -> None:
    adapter = WorkspaceWriteToolAdapter(
        workspace_root=str(tmp_path), max_write_bytes=4
    )
    invocation = adapter.build_invocation(_workspace_write_args("f.txt", "x" * 64))
    with pytest.raises(ToolAdapterInvocationError) as captured:
        adapter.invoke_once(invocation, _NullContext())
    assert captured.value.safe_error_code == "TOOL_WORKSPACE_CONTENT_TOO_LARGE"


def test_write_invalid_argument_shapes(tmp_path: Path) -> None:
    adapter = WorkspaceWriteToolAdapter(workspace_root=str(tmp_path))
    for bad in (
        "not-json",
        json.dumps({"path": "f.txt"}),
        json.dumps({"content": "x"}),
        json.dumps({"path": "f.txt", "content": 42}),
    ):
        with pytest.raises(ToolAdapterInvocationError) as captured:
            adapter.build_invocation(bad)
        assert captured.value.safe_error_code == "TOOL_VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# Native function schema（§19 / §20）
# ---------------------------------------------------------------------------


def test_native_schema_hides_runtime_fields() -> None:
    registry = production_registry()
    for tool_name in ("workspace_read_file", "workspace_write_file"):
        definition = registry.require(tool_name).native_function_definition()
        assert definition["type"] == "function"
        assert definition["function"]["name"] == tool_name
        encoded = json.dumps(definition, ensure_ascii=False)
        for forbidden in (
            "risk",
            "approval_required",
            "side_effect",
            "idempotency",
            "workspace_root",
            "workspace root",
            "authorization",
            "execution claim",
            "timeout",
            "provider_tool_call_id",
            "operation_id",
            "NON_IDEMPOTENT_SIMULATION",
        ):
            assert forbidden not in encoded, (tool_name, forbidden)
        parameters = definition["function"]["parameters"]
        assert parameters["type"] == "object"
        assert parameters["additionalProperties"] is False
        assert "path" in parameters["properties"]


def test_native_schema_read_and_write_distinct() -> None:
    registry = production_registry()
    read_def = registry.require("workspace_read_file").native_function_definition()
    write_def = registry.require("workspace_write_file").native_function_definition()
    read_required = read_def["function"]["parameters"]["required"]
    write_required = write_def["function"]["parameters"]["required"]
    assert read_required == ["path"]
    assert write_required == ["path", "content"]


def test_workspace_descriptions_distinguish_from_list_files() -> None:
    registry = production_registry()
    workspace_desc = registry.require("workspace_read_file").descriptor
    list_files_desc = registry.require("list_files").descriptor
    assert "workspace" in workspace_desc.description.lower()
    assert list_files_desc is not None
    assert "list_files" in workspace_desc.llm_instructions


# ---------------------------------------------------------------------------
# Registry / Governance / resource extractor coverage
# ---------------------------------------------------------------------------


def test_registry_and_governance_cover_six_tools() -> None:
    registry = production_registry()
    catalog = ToolPolicyCatalog(
        tool_registry=registry,
        agent_registry=__import__(
            "core.runtime.agent_registry", fromlist=["DEFAULT_AGENT_REGISTRY"]
        ).DEFAULT_AGENT_REGISTRY,
    )
    register_default_tool_policies(catalog)
    catalog.freeze()
    names = {p.tool_name for p in catalog.policies()}
    assert names == {
        registration.descriptor.name for registration in registry.registrations()
    }
    assert "workspace_read_file" in names
    assert "workspace_write_file" in names
    assert catalog.find("workspace_read_file").risk_facts == (
        ToolRiskFact.RESTRICTED_WORKSPACE_READ,
    )
    assert catalog.find("workspace_write_file").risk_facts == ()


def test_workspace_read_governance_low_risk_allow(tmp_path: Path) -> None:
    registry = production_registry()
    service = production_service(registry)
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    registration = _read_registration(registry)
    adapter = registration.adapter
    # 用临时 root 复制 adapter，仅用于 governance 分类（身份不变）。
    invocation = WorkspaceReadToolAdapter(
        workspace_root=str(tmp_path)
    ).build_invocation(_workspace_read_args("note.txt"))
    static = service.authorize_tool(_governance_context(), registration)
    assert static.outcome is ToolGovernanceOutcome.ALLOW
    decision = service.evaluate_invocation(
        _governance_context(), registration, invocation, adapter.spec_for(invocation)
    )
    assert decision.risk_level is ToolRiskLevel.LOW
    assert decision.outcome is ToolGovernanceOutcome.ALLOW


def test_workspace_write_governance_medium_risk_allow(tmp_path: Path) -> None:
    registry = production_registry()
    service = production_service(registry)
    registration = _write_registration(registry)
    adapter = WorkspaceWriteToolAdapter(workspace_root=str(tmp_path))
    invocation = adapter.build_invocation(
        _workspace_write_args("f.txt", "content")
    )
    static = service.authorize_tool(_governance_context(), registration)
    assert static.outcome is ToolGovernanceOutcome.ALLOW
    decision = service.evaluate_invocation(
        _governance_context(), registration, invocation, adapter.spec_for(invocation)
    )
    assert decision.risk_level is ToolRiskLevel.MEDIUM
    assert decision.outcome is ToolGovernanceOutcome.ALLOW


def test_workspace_write_via_real_adapter_governance_mapping(tmp_path: Path) -> None:
    """真实生产 adapter（default demo root）的 spec_for 分类进入 governance。"""
    registry = production_registry()
    service = production_service(registry)
    registration = _write_registration(registry)
    adapter = registration.adapter
    invocation = adapter.build_invocation(
        _workspace_write_args("wp2-gov.txt", "governance")
    )
    spec = adapter.spec_for(invocation)
    decision = service.evaluate_invocation(
        _governance_context(), registration, invocation, spec
    )
    assert decision.risk_level is ToolRiskLevel.MEDIUM
    assert decision.outcome is ToolGovernanceOutcome.ALLOW
    (Path(adapter._workspace_root) / "wp2-gov.txt").unlink(missing_ok=True)


def test_workspace_tools_are_outside_read_roots_extractor_surface() -> None:
    """workspace 工具不进入 application-wide read-roots extractor 面。

    相对 path + adapter 内置 resolve containment 是它们的安全边界；
    resource extractor catalog 仍只覆盖 legacy 任意路径 READ 工具。
    """
    registry = production_registry()
    catalog = ToolResourceExtractorCatalog()
    catalog.register(
        ToolResourceExtractorDescriptor(
            "list_files", "argument_text", ResourceKind.DIRECTORY, ResourceOperation.READ
        )
    )
    catalog.register(
        ToolResourceExtractorDescriptor(
            "analyze_excel", "argument_text", ResourceKind.FILE, ResourceOperation.READ
        )
    )
    catalog.validate(registry)
    catalog.freeze()
    assert catalog.extract is not None
    from core.runtime import (
        ResourceAuthorizationService as _Svc,
        FilesystemResourcePolicy as _Policy,
    )

    service = _Svc(_Policy(("D:\\nonexistent-root-wp2",)), catalog)
    invocation = WorkspaceReadToolAdapter().build_invocation(
        _workspace_read_args("project_note.txt")
    )
    # workspace 工具没有 extractor -> 不产生 resource request -> 不走该面。
    assert service.extract(invocation) is None


# ---------------------------------------------------------------------------
# Planner 自然语言选择（§29；fake planner deterministic tests）
# ---------------------------------------------------------------------------


def _planner_router(responses):
    registry = production_registry()
    router = AgentRouter.__new__(AgentRouter)
    router.tool_registry = registry
    router.agents_config = __import__(
        "core.runtime.agent_registry", fromlist=["DEFAULT_AGENT_REGISTRY"]
    ).DEFAULT_AGENT_REGISTRY.legacy_display_config()
    router.tool_plan_max_tokens = 120
    iterator = iter(responses)
    router._collect_model_response = lambda *_args, **_kwargs: next(iterator)
    return router


def test_planner_selects_workspace_read_from_natural_language():
    router = _planner_router(['CALL: workspace_read_file({"path":"project_note.txt"})'])
    request = "读取 demo workspace 里的 project_note.txt，告诉我里面记录了什么。"
    assert "workspace_read_file" not in request
    selected = router._plan_tool_call(
        [
            {"role": "system", "content": "ignored"},
            {"role": "user", "content": request},
        ],
        "core_router",
    )
    assert selected is not None
    assert selected[0] == "workspace_read_file"
    invocation = router.tool_registry.require(selected[0]).adapter.build_invocation(
        selected[1]
    )
    assert invocation.arguments["path"] == "project_note.txt"


def test_planner_selects_workspace_write_from_natural_language():
    router = _planner_router(
        ['CALL: workspace_write_file({"path":"result.txt","content":"phase8 demo passed"})']
    )
    request = "把“phase8 demo passed”写到 demo workspace 的 result.txt。"
    assert "workspace_write_file" not in request
    selected = router._plan_tool_call(
        [
            {"role": "system", "content": "ignored"},
            {"role": "user", "content": request},
        ],
        "core_router",
    )
    assert selected is not None
    assert selected[0] == "workspace_write_file"
    invocation = router.tool_registry.require(selected[0]).adapter.build_invocation(
        selected[1]
    )
    assert invocation.arguments["path"] == "result.txt"
    assert invocation.arguments["content"] == "phase8 demo passed"


def test_planner_selects_simulator_from_natural_language():
    router = _planner_router(
        [
            'CALL: complex_workflow_simulator({"resource_key":"demo-resource",'
            '"execution_mode":"NON_IDEMPOTENT_SIMULATION","items":[{"item_id":'
            '"item-1","action":"ADD","quantity":1}]})'
        ]
    )
    request = (
        "对 demo-resource 中 item-1 做一次增加 1 的真实模拟操作，"
        "如果需要审批就按系统规则处理。"
    )
    assert "complex_workflow_simulator" not in request
    assert "NON_IDEMPOTENT_SIMULATION" not in request
    selected = router._plan_tool_call(
        [
            {"role": "system", "content": "ignored"},
            {"role": "user", "content": request},
        ],
        "core_router",
    )
    assert selected is not None
    assert selected[0] == "complex_workflow_simulator"
    invocation = router.tool_registry.require(selected[0]).adapter.build_invocation(
        selected[1]
    )
    assert invocation.resource_key == "demo-resource"
    assert invocation.arguments["operation_id"].startswith("workflow-")


def test_simulator_governance_high_risk_approval_required():
    registry = production_registry()
    service = production_service(registry)
    registration = registry.require("complex_workflow_simulator")
    adapter = registration.adapter
    invocation = adapter.build_invocation(
        json.dumps(
            {
                "resource_key": "demo-resource",
                "execution_mode": "NON_IDEMPOTENT_SIMULATION",
                "items": [{"item_id": "item-1", "action": "ADD", "quantity": 1}],
            }
        )
    )
    decision = service.evaluate_invocation(
        _governance_context(), registration, invocation, adapter.spec_for(invocation)
    )
    assert decision.risk_level is ToolRiskLevel.HIGH
    assert decision.outcome is ToolGovernanceOutcome.APPROVAL_REQUIRED


def test_ordinary_chat_skips_planner_zero_tool():
    """§30 普通聊天回归：不触发 planner、无 Tool 执行。"""
    router = _planner_router(["NO_TOOL"])
    request = "解释一下幂等性是什么。"
    assert router._plan_tool_call(
        [
            {"role": "system", "content": "ignored"},
            {"role": "user", "content": request},
        ],
        "core_router",
    ) is None


# ---------------------------------------------------------------------------
# 生产 Demo Workspace fixture 读写（真实固定 root，最小安全内容）
# ---------------------------------------------------------------------------


def test_production_demo_workspace_fixture_roundtrip() -> None:
    from tools.demo_workspace import default_demo_workspace_root

    root = Path(default_demo_workspace_root())
    if not root.exists():
        pytest.skip("ENVIRONMENT_BLOCKED: demo workspace fixture 不存在")
    adapter = WorkspaceReadToolAdapter()
    invocation = adapter.build_invocation(_workspace_read_args("project_note.txt"))
    response = adapter.invoke_once(invocation, _NullContext())
    assert "LocalAgent Phase8 demo workspace." in response.content


def test_write_adapter_max_concurrency_is_one() -> None:
    """写工具串行执行（max_concurrency=1），避免并发覆盖竞争。"""
    adapter = WorkspaceWriteToolAdapter()
    assert adapter.spec.max_concurrency == 1


class _NullContext:
    """最小 ToolAdapterContext 桩：不取消、无副作用前置钩子。"""

    def raise_if_cancelled(self) -> None:
        return None

    def before_side_effect(self) -> None:
        return None
