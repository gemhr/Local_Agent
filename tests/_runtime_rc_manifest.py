from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RCScenario:
    scenario_id: str
    name: str
    test_id: str


RC_SCENARIOS = (
    RCScenario("RC-01", "Coordinated normal", "tests/test_runtime_mode_e2e.py::test_api_to_factory_to_output_delta_to_terminal_happy_path"),
    RCScenario("RC-02", "Model transient retry", "tests/test_retry_model_integration.py::ModelRetryIntegrationTests::test_zero_delay_retry_success_and_stable_indices"),
    RCScenario("RC-03", "Model candidate fallback", "tests/test_model_invocation.py::CoordinatedInvocationIntegrationTests::test_real_coordinated_path_uses_router_and_succeeds_after_fallback"),
    RCScenario("RC-04", "Retrieval rewrite degradation", "tests/test_retrieval_execution.py::test_query_rewrite_and_rerank_failures_are_controlled_degradation"),
    RCScenario("RC-05", "Retrieval search fail-closed", "tests/test_retrieval_execution.py::test_embedding_and_vector_failures_are_not_reported_as_empty"),
    RCScenario("RC-06", "Tool read-only success", "tests/test_tool_execution.py::test_success_reserves_one_tool_call_and_limits_output"),
    RCScenario("RC-07", "Tool transient retry", "tests/test_tool_execution.py::test_read_only_transient_retry_keeps_invocation_and_changes_attempt"),
    RCScenario("RC-08", "Non-idempotent committed failure", "tests/test_tool_execution_integration.py::test_non_idempotent_post_commit_failure_does_not_retry"),
    RCScenario("RC-09", "Parallel best-effort", "tests/test_parallel_execution.py::ParallelExecutionTests::test_best_effort_stable_order_and_state_terminal"),
    RCScenario("RC-10", "Parallel fail-fast", "tests/test_parallel_execution.py::ParallelExecutionCancellationAndPreflightTests::test_fail_fast_cancels_global_semaphore_waiter"),
    RCScenario("RC-11", "Budget exhausted", "tests/test_tool_execution.py::test_budget_failure_does_not_enter_adapter"),
    RCScenario("RC-12", "Deadline timeout", "tests/test_retrieval_execution.py::test_queued_provider_call_deadline_releases_budget_and_never_executes"),
    RCScenario("RC-13", "Client disconnect", "tests/test_client_disconnect.py::test_disconnect_watcher_stops_output_and_is_awaited"),
    RCScenario("RC-14", "Checkpoint success", "tests/test_checkpoint_integration.py::test_step_boundary_checkpoint_is_saved_and_scheduler_resumes"),
    RCScenario("RC-15", "Recovery validation success", "tests/test_recovery_integration.py::test_store_to_journal_recovery_collects_tool_evidence_with_zero_replay"),
    RCScenario("RC-16", "Incomplete tool recovery evidence", "tests/test_recovery_tool_completion_gap.py::test_recovery_validation_invokes_no_model_tool_retrieval_or_compensation"),
    RCScenario("RC-17", "Clean shutdown", "tests/test_shutdown_report_truthfulness.py::test_shutdown_top_level_semantics_distinguish_orchestration_and_closure"),
    RCScenario("RC-18", "Detached-worker shutdown", "tests/test_shutdown_report_truthfulness.py::test_report_distinguishes_unexecuted_drain_and_deferred_model"),
    RCScenario("RC-19", "Explicit Legacy", "tests/test_runtime_mode_e2e.py::test_explicit_legacy_api_does_not_create_coordinated_scope"),
    RCScenario("RC-20", "Coordinated failure without Legacy fallback", "tests/test_runtime_mode_e2e.py::test_api_coordinated_failure_has_no_legacy_fallback"),
)


def assert_real_test_mappings(*scenario_ids: str) -> None:
    selected = {item.scenario_id: item for item in RC_SCENARIOS}
    for scenario_id in scenario_ids:
        scenario = selected[scenario_id]
        file_name, *node_parts = scenario.test_id.split("::")
        source_path = Path(file_name)
        assert source_path.is_file(), scenario.test_id
        source = source_path.read_text(encoding="utf-8")
        assert node_parts[-1] in source, scenario.test_id

