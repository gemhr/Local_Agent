"""WP12 focused Docker sandbox tests; all secrets in this file are synthetic."""

from __future__ import annotations

import json
from pathlib import Path
import asyncio
from dataclasses import replace

import pytest

from core.runtime import (
    BudgetLedger,
    DockerIsolatedExecutionBackend,
    OperationIdempotency,
    RetryPolicy,
    RunBudget,
    RunCancelledError,
    SandboxExecutionDemoToolAdapter,
    ToolAdapter,
    ToolAdapterInvocationError,
    ToolAdapterResponse,
    ToolAttemptExecutor,
    ToolErrorCategory,
    ToolExecutionBackendResolver,
    ToolExecutionService,
    ToolExecutionSpec,
    ToolExecutionStatus,
    ToolInvocation,
    ToolSideEffectKind,
    create_run_context,
)
from core.runtime.retry import RetryExecutor


def _context(*, timeout: float = 5.0):
    context, _ = create_run_context(entry_agent_id="test", timeout_seconds=timeout)
    context.attach_budget_ledger(BudgetLedger(RunBudget(max_tool_calls=3)))
    return context


def _service(backend: DockerIsolatedExecutionBackend) -> ToolExecutionService:
    return ToolExecutionService(
        attempt_executor=ToolAttemptExecutor(
            backend_resolver=ToolExecutionBackendResolver(isolated_backend=backend)
        )
    )


def _docker_backend(tmp_path: Path) -> DockerIsolatedExecutionBackend:
    input_root = tmp_path / "input"
    input_root.mkdir(exist_ok=True)
    (input_root / "allowed.txt").write_text("mounted input", encoding="utf-8")
    return DockerIsolatedExecutionBackend(input_root=input_root, temp_root=tmp_path)


def _invocation(operation: str, **kwargs: object):
    return SandboxExecutionDemoToolAdapter().build_invocation(
        json.dumps({"operation": operation, **kwargs})
    )


@pytest.mark.asyncio
async def test_allowed_input_and_dedicated_output_are_contained(tmp_path: Path):
    adapter = SandboxExecutionDemoToolAdapter()
    backend = _docker_backend(tmp_path)
    service = _service(backend)
    read = await service.execute(
        invocation=_invocation("READ_INPUT", input_path="allowed.txt"),
        adapter=adapter,
        run_context=_context(),
        step_id="read",
    )
    assert read.status is ToolExecutionStatus.SUCCEEDED
    assert read.output.content == "mounted input"
    written = await service.execute(
        invocation=_invocation(
            "WRITE_OUTPUT", output_path="nested/result.txt", output_text="safe"
        ),
        adapter=adapter,
        run_context=_context(),
        step_id="write",
    )
    assert written.status is ToolExecutionStatus.SUCCEEDED
    assert json.loads(written.output.content)["written"] is True
    assert not (tmp_path / "nested" / "result.txt").exists()


@pytest.mark.asyncio
async def test_path_escape_is_typed_denial_and_external_marker_is_unreadable(tmp_path: Path):
    marker = tmp_path / "outside-marker.txt"
    marker.write_text("WP12_SYNTHETIC_SECRET_OUTSIDE", encoding="utf-8")
    adapter = SandboxExecutionDemoToolAdapter()
    result = await _service(_docker_backend(tmp_path)).execute(
        invocation=_invocation("READ_INPUT", input_path="../outside-marker.txt"),
        adapter=adapter,
        run_context=_context(),
        step_id="escape",
    )
    assert result.safe_error_code == "SANDBOX_PATH_DENIED"
    assert "WP12_SYNTHETIC_SECRET_OUTSIDE" not in str(result)


@pytest.mark.asyncio
async def test_environment_is_not_inherited_and_network_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("WP12_SYNTHETIC_SECRET_PARENT", "WP12_SYNTHETIC_SECRET_VALUE")
    adapter = SandboxExecutionDemoToolAdapter()
    service = _service(_docker_backend(tmp_path))
    env = await service.execute(
        invocation=_invocation("ENV_PROBE"),
        adapter=adapter,
        run_context=_context(),
        step_id="env",
    )
    network = await service.execute(
        invocation=_invocation("NETWORK_PROBE"),
        adapter=adapter,
        run_context=_context(),
        step_id="network",
    )
    assert json.loads(env.output.content)["synthetic_secret_present"] is False
    assert json.loads(network.output.content)["network"] == "blocked"


@pytest.mark.asyncio
async def test_timeout_and_output_capture_fail_closed(tmp_path: Path):
    adapter = SandboxExecutionDemoToolAdapter()
    backend = _docker_backend(tmp_path)
    timeout = await _service(backend).execute(
        invocation=ToolInvocation.create(
            tool_name=adapter.spec.tool_name,
            arguments={"operation": "SLEEP", "sleep_seconds": 5},
            requested_timeout_seconds=0.35,
        ),
        adapter=adapter,
        run_context=_context(timeout=5.0),
        step_id="timeout",
    )
    assert timeout.status is ToolExecutionStatus.TIMED_OUT
    assert timeout.safe_error_code in {"SANDBOX_TIMEOUT", "TOOL_DEADLINE_EXCEEDED"}
    assert timeout.worker_terminated is True
    assert backend.last_cleanup_status == "VERIFIED"
    stress = await _service(_docker_backend(tmp_path)).execute(
        invocation=_invocation("OUTPUT_STRESS", output_size=65_536),
        adapter=adapter,
        run_context=_context(),
        step_id="output",
    )
    assert stress.safe_error_code == "SANDBOX_OUTPUT_LIMIT"


@pytest.mark.asyncio
async def test_isolated_policy_timeout_participates_in_runtime_deadline(tmp_path: Path):
    adapter = SandboxExecutionDemoToolAdapter()
    adapter.spec = replace(
        adapter.spec,
        default_timeout_seconds=5.0,
        sandbox_policy=replace(adapter.spec.sandbox_policy, timeout_seconds=0.25),
    )
    result = await _service(_docker_backend(tmp_path)).execute(
        invocation=_invocation("SLEEP", sleep_seconds=2),
        adapter=adapter,
        run_context=_context(timeout=5.0),
        step_id="policy-timeout",
    )
    assert result.status is ToolExecutionStatus.TIMED_OUT
    assert result.worker_terminated is True


@pytest.mark.asyncio
async def test_run_cancellation_terminates_isolated_container(tmp_path: Path):
    adapter = SandboxExecutionDemoToolAdapter()
    backend = _docker_backend(tmp_path)
    context, source = create_run_context(entry_agent_id="test", timeout_seconds=5.0)
    context.attach_budget_ledger(BudgetLedger(RunBudget(max_tool_calls=2)))
    task = asyncio.create_task(
        _service(backend).execute(
            invocation=_invocation("SLEEP", sleep_seconds=5),
            adapter=adapter,
            run_context=context,
            step_id="cancel",
        )
    )
    await asyncio.sleep(0.2)
    source.cancel("WP12_TEST_CANCELLED")
    with pytest.raises(RunCancelledError):
        await task
    assert backend.last_cleanup_status == "VERIFIED"


@pytest.mark.asyncio
async def test_fixed_child_completes_and_unavailable_backend_does_not_fallback(
    tmp_path: Path,
):
    adapter = SandboxExecutionDemoToolAdapter()
    child = await _service(_docker_backend(tmp_path)).execute(
        invocation=_invocation("CHILD_PROCESS"),
        adapter=adapter,
        run_context=_context(),
        step_id="child",
    )
    assert child.status is ToolExecutionStatus.SUCCEEDED

    unavailable = await _service(
        DockerIsolatedExecutionBackend(
            input_root=tmp_path / "input", docker_executable="wp12-missing-docker"
        )
    ).execute(
        invocation=_invocation("READ_INPUT"),
        adapter=adapter,
        run_context=_context(),
        step_id="unavailable",
    )
    assert unavailable.safe_error_code == "SANDBOX_BACKEND_UNAVAILABLE"


def test_operator_owned_policy_cannot_be_selected_by_invocation():
    adapter = SandboxExecutionDemoToolAdapter()
    with pytest.raises(ToolAdapterInvocationError):
        adapter.build_invocation(
            json.dumps({"operation": "READ_INPUT", "network_mode": "ALLOW_NETWORK"})
        )
    assert adapter.spec.sandbox_policy.network_mode.value == "NO_NETWORK"


@pytest.mark.asyncio
async def test_trusted_builtin_still_uses_trusted_backend():
    class BuiltinAdapter(ToolAdapter):
        def __init__(self):
            self.calls = 0
            self.spec = ToolExecutionSpec(
                tool_name="trusted_demo",
                side_effect_kind=ToolSideEffectKind.NONE,
                idempotency=OperationIdempotency.READ_ONLY,
            )

        def build_invocation(self, argument_text: str):
            return ToolInvocation.create(tool_name="trusted_demo", arguments={})

        def invoke_once(self, invocation, context):
            self.calls += 1
            return ToolAdapterResponse("trusted", "text/plain", "trusted")

    adapter = BuiltinAdapter()
    result = await ToolExecutionService().execute(
        invocation=adapter.build_invocation(""),
        adapter=adapter,
        run_context=_context(),
        step_id="trusted",
    )
    assert result.status is ToolExecutionStatus.SUCCEEDED
    assert adapter.calls == 1
    assert adapter.spec.sandbox_policy.network_mode.value == "NOT_APPLICABLE"
