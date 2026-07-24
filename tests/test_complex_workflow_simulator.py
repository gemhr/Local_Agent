#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Focused contract-preparation tests for the complex local Tool simulator."""

from __future__ import annotations

from dataclasses import replace
import inspect
import json
from pathlib import Path
import tempfile
import threading
import unittest

from core.agent_router import AgentRouter
from tools.complex_workflow_simulator import (
    ComplexWorkflowRequest,
    ComplexWorkflowSimulationTool,
    InMemoryWorkflowStateStore,
    JsonFileWorkflowStateStore,
    WorkflowExecutionMode,
    WorkflowFailureInjection,
    WorkflowItem,
    WorkflowProcessingOptions,
    WorkflowResourceLockManager,
    WorkflowResultStatus,
    WorkflowSimulationError,
    WorkflowStage,
    complex_workflow_simulator,
)
from tools.registry import register_all_tools


def make_request(
    *,
    operation_id: str = "operation-1",
    resource_key: str = "mock-resource:group-a",
    idempotency_key: str | None = "idem-1",
    mode: WorkflowExecutionMode = WorkflowExecutionMode.IDEMPOTENT_COMMIT,
    failure: WorkflowFailureInjection = WorkflowFailureInjection.NONE,
    failure_item_id: str | None = None,
    options: WorkflowProcessingOptions | None = None,
    items: tuple[WorkflowItem, ...] | None = None,
    metadata: dict | None = None,
) -> ComplexWorkflowRequest:
    return ComplexWorkflowRequest(
        operation_id=operation_id,
        resource_key=resource_key,
        idempotency_key=idempotency_key,
        execution_mode=mode,
        items=items
        or (
            WorkflowItem("item-1", "increase", 2, attributes={"label": "safe"}),
            WorkflowItem("item-2", "decrease", 1),
        ),
        failure_injection=failure,
        failure_item_id=failure_item_id,
        processing_options=options or WorkflowProcessingOptions(max_parallel_items=2),
        metadata=metadata or {},
    )


class ComplexWorkflowSimulationToolTests(unittest.TestCase):
    def test_dry_run_succeeds_without_any_state_write(self) -> None:
        store = InMemoryWorkflowStateStore()
        result = ComplexWorkflowSimulationTool(state_store=store).execute(
            make_request(
                mode=WorkflowExecutionMode.DRY_RUN,
                idempotency_key=None,
            )
        )
        self.assertEqual(result.status, WorkflowResultStatus.SUCCEEDED)
        self.assertFalse(result.side_effect_committed)
        self.assertEqual(store.resource_states, {})
        self.assertEqual(store.committed_operations, [])
        self.assertEqual(store.audit_records, [])

    def test_idempotent_commit_succeeds_and_records_one_side_effect(self) -> None:
        store = InMemoryWorkflowStateStore()
        result = ComplexWorkflowSimulationTool(state_store=store).execute(make_request())
        self.assertEqual(result.status, WorkflowResultStatus.SUCCEEDED)
        self.assertTrue(result.side_effect_committed)
        self.assertEqual(len(store.committed_operations), 1)
        self.assertTrue(all(item.side_effect_committed for item in result.item_results))

    def test_same_idempotency_key_and_request_replays(self) -> None:
        store = InMemoryWorkflowStateStore()
        tool = ComplexWorkflowSimulationTool(state_store=store)
        request = make_request()
        first = tool.execute(request)
        replay = tool.execute(request)
        self.assertEqual(first.status, WorkflowResultStatus.SUCCEEDED)
        self.assertEqual(replay.status, WorkflowResultStatus.IDEMPOTENCY_REPLAY)
        self.assertTrue(replay.idempotency_replayed)
        self.assertEqual(first.audit_digest, replay.audit_digest)

    def test_replay_does_not_repeat_side_effect(self) -> None:
        store = InMemoryWorkflowStateStore()
        tool = ComplexWorkflowSimulationTool(state_store=store)
        request = make_request()
        tool.execute(request)
        tool.execute(request)
        self.assertEqual(len(store.committed_operations), 1)
        self.assertEqual(store.resource_states[request.resource_key], 1)

    def test_same_key_with_different_request_is_conflict(self) -> None:
        store = InMemoryWorkflowStateStore()
        tool = ComplexWorkflowSimulationTool(state_store=store)
        tool.execute(make_request())
        conflict = tool.execute(
            make_request(items=(WorkflowItem("other", "increase", 1),))
        )
        self.assertEqual(conflict.status, WorkflowResultStatus.FAILED)
        self.assertEqual(conflict.safe_error_code, "TOOL_IDEMPOTENCY_CONFLICT")
        self.assertEqual(len(store.committed_operations), 1)

    def test_non_idempotent_simulation_repeats_side_effect(self) -> None:
        store = InMemoryWorkflowStateStore()
        tool = ComplexWorkflowSimulationTool(state_store=store)
        request = make_request(
            mode=WorkflowExecutionMode.NON_IDEMPOTENT_SIMULATION,
            idempotency_key=None,
        )
        tool.execute(request)
        tool.execute(request)
        self.assertEqual(len(store.committed_operations), 2)

    def test_transient_before_side_effect_is_safe_to_invoke_again(self) -> None:
        store = InMemoryWorkflowStateStore()
        tool = ComplexWorkflowSimulationTool(state_store=store)
        failure = tool.execute(
            make_request(failure=WorkflowFailureInjection.TRANSIENT_BEFORE_SIDE_EFFECT)
        )
        success = tool.execute(make_request())
        self.assertEqual(failure.safe_error_code, "TOOL_TRANSIENT_FAILURE")
        self.assertFalse(failure.side_effect_committed)
        self.assertEqual(success.status, WorkflowResultStatus.SUCCEEDED)
        self.assertEqual(len(store.committed_operations), 1)

    def test_timeout_before_side_effect(self) -> None:
        result = ComplexWorkflowSimulationTool().execute(
            make_request(failure=WorkflowFailureInjection.TIMEOUT_BEFORE_SIDE_EFFECT)
        )
        self.assertEqual(result.status, WorkflowResultStatus.TIMED_OUT)
        self.assertEqual(result.safe_error_code, "TOOL_TIMEOUT")
        self.assertFalse(result.side_effect_committed)

    def test_failure_stage_delays_injection_to_selected_checkpoint(self) -> None:
        request = replace(
            make_request(failure=WorkflowFailureInjection.TRANSIENT_BEFORE_SIDE_EFFECT),
            failure_stage=WorkflowStage.COMMIT_SIDE_EFFECTS,
        )
        result = ComplexWorkflowSimulationTool().execute(request)
        stages = [record.stage for record in result.completed_stages]
        self.assertIn(WorkflowStage.PROCESS_ITEMS, stages)
        self.assertIn(WorkflowStage.VALIDATE_PROCESSED_ITEMS, stages)
        self.assertEqual(result.safe_error_code, "TOOL_TRANSIENT_FAILURE")
        self.assertFalse(result.side_effect_committed)

    def test_validation_failure_is_sanitized(self) -> None:
        result = ComplexWorkflowSimulationTool().execute(
            make_request(failure=WorkflowFailureInjection.VALIDATION_ERROR)
        )
        self.assertEqual(result.safe_error_code, "TOOL_VALIDATION_ERROR")
        self.assertNotIn("Traceback", result.safe_message)

    def test_injected_and_actual_resource_conflicts(self) -> None:
        manager = WorkflowResourceLockManager()
        tool = ComplexWorkflowSimulationTool(lock_manager=manager)
        injected = tool.execute(
            make_request(failure=WorkflowFailureInjection.RESOURCE_CONFLICT)
        )
        self.assertEqual(injected.safe_error_code, "TOOL_RESOURCE_CONFLICT")
        self.assertTrue(manager.acquire("mock-resource:group-a"))
        actual = tool.execute(make_request())
        manager.release("mock-resource:group-a")
        self.assertEqual(actual.safe_error_code, "TOOL_RESOURCE_CONFLICT")

    def test_different_resource_keys_can_execute_concurrently(self) -> None:
        entered = 0
        maximum = 0
        guard = threading.Lock()
        both_entered = threading.Event()

        def sleeper(_: float) -> None:
            nonlocal entered, maximum
            with guard:
                entered += 1
                maximum = max(maximum, entered)
                if entered == 2:
                    both_entered.set()
            both_entered.wait(1)
            with guard:
                entered -= 1

        tool = ComplexWorkflowSimulationTool(sleeper=sleeper)
        results = []

        def run(key: str) -> None:
            results.append(
                tool.execute(
                    make_request(
                        operation_id=f"operation-{key[-1]}",
                        resource_key=key,
                        idempotency_key=f"idem-{key[-1]}",
                        items=(WorkflowItem(f"item-{key[-1]}", "change", 1),),
                        options=WorkflowProcessingOptions(processing_delay_ms=1),
                    )
                )
            )

        threads = [
            threading.Thread(target=run, args=("mock-resource:a",)),
            threading.Thread(target=run, args=("mock-resource:b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(maximum, 2)
        self.assertTrue(all(result.status == WorkflowResultStatus.SUCCEEDED for result in results))

    def test_partial_failure_allowed_returns_stable_partial_result(self) -> None:
        request = make_request(
            failure=WorkflowFailureInjection.PARTIAL_ITEM_FAILURE,
            failure_item_id="item-2",
            options=WorkflowProcessingOptions(
                max_parallel_items=2, allow_partial_success=True
            ),
        )
        result = ComplexWorkflowSimulationTool().execute(request)
        self.assertEqual(result.status, WorkflowResultStatus.PARTIALLY_SUCCEEDED)
        self.assertEqual([item.item_id for item in result.item_results], ["item-1", "item-2"])
        self.assertEqual([item.status.value for item in result.item_results], ["SUCCEEDED", "FAILED"])
        self.assertTrue(result.item_results[0].side_effect_committed)
        self.assertFalse(result.item_results[1].side_effect_committed)

    def test_partial_failure_disallowed_does_not_commit(self) -> None:
        store = InMemoryWorkflowStateStore()
        result = ComplexWorkflowSimulationTool(state_store=store).execute(
            make_request(
                failure=WorkflowFailureInjection.PARTIAL_ITEM_FAILURE,
                failure_item_id="item-1",
                options=WorkflowProcessingOptions(allow_partial_success=False),
            )
        )
        self.assertEqual(result.status, WorkflowResultStatus.FAILED)
        self.assertEqual(result.safe_error_code, "TOOL_PARTIAL_FAILURE")
        self.assertFalse(result.side_effect_committed)
        self.assertEqual(store.committed_operations, [])

    def test_fail_after_side_effect_without_compensation_exposes_ambiguity(self) -> None:
        store = InMemoryWorkflowStateStore()
        result = ComplexWorkflowSimulationTool(state_store=store).execute(
            make_request(
                failure=WorkflowFailureInjection.FAIL_AFTER_SIDE_EFFECT,
                options=WorkflowProcessingOptions(enable_compensation=False),
            )
        )
        self.assertEqual(result.safe_error_code, "TOOL_SIDE_EFFECT_FAILURE")
        self.assertTrue(result.side_effect_committed)
        self.assertFalse(result.compensation_attempted)
        self.assertEqual(len(store.committed_operations), 1)

    def test_compensation_success_restores_state(self) -> None:
        store = InMemoryWorkflowStateStore()
        result = ComplexWorkflowSimulationTool(state_store=store).execute(
            make_request(failure=WorkflowFailureInjection.FAIL_AFTER_SIDE_EFFECT)
        )
        self.assertTrue(result.compensation_attempted)
        self.assertTrue(result.compensation_succeeded)
        self.assertFalse(result.side_effect_committed)
        self.assertEqual(store.resource_states["mock-resource:group-a"], 0)
        self.assertEqual(store.compensation_records[-1]["status"], "SUCCEEDED")

    def test_compensation_failure_requires_manual_attention(self) -> None:
        result = ComplexWorkflowSimulationTool().execute(
            make_request(failure=WorkflowFailureInjection.COMPENSATION_FAILURE)
        )
        self.assertEqual(result.safe_error_code, "TOOL_COMPENSATION_FAILURE")
        self.assertTrue(result.side_effect_committed)
        self.assertTrue(result.compensation_attempted)
        self.assertFalse(result.compensation_succeeded)

    def test_cancellation_before_side_effect(self) -> None:
        result = ComplexWorkflowSimulationTool(cancellation_probe=lambda: True).execute(
            make_request()
        )
        self.assertEqual(result.status, WorkflowResultStatus.CANCELLED)
        self.assertFalse(result.side_effect_committed)

    def test_cancellation_after_side_effect_compensates(self) -> None:
        calls = 0

        def probe() -> bool:
            nonlocal calls
            calls += 1
            return calls >= 5

        store = InMemoryWorkflowStateStore()
        result = ComplexWorkflowSimulationTool(
            state_store=store, cancellation_probe=probe
        ).execute(make_request())
        self.assertEqual(result.status, WorkflowResultStatus.CANCELLED)
        self.assertTrue(result.compensation_attempted)
        self.assertTrue(result.compensation_succeeded)
        self.assertFalse(result.side_effect_committed)

    def test_resource_lock_is_released_after_success_failure_and_cancellation(self) -> None:
        manager = WorkflowResourceLockManager()
        for request, probe in (
            (make_request(), None),
            (
                make_request(
                    operation_id="operation-2",
                    idempotency_key="idem-2",
                    failure=WorkflowFailureInjection.UNKNOWN_FAILURE,
                ),
                None,
            ),
            (
                make_request(operation_id="operation-3", idempotency_key="idem-3"),
                lambda: True,
            ),
        ):
            ComplexWorkflowSimulationTool(
                lock_manager=manager, cancellation_probe=probe
            ).execute(request)
            self.assertFalse(manager.is_locked(request.resource_key))

    def test_input_order_is_stable_with_parallel_processing(self) -> None:
        items = tuple(
            WorkflowItem(f"item-{index}", "change", 1) for index in range(8)
        )
        result = ComplexWorkflowSimulationTool(sleeper=lambda _: None).execute(
            make_request(
                items=items,
                options=WorkflowProcessingOptions(
                    max_parallel_items=4, processing_delay_ms=1
                ),
            )
        )
        self.assertEqual(
            [item.item_id for item in result.item_results],
            [item.item_id for item in items],
        )

    def test_max_parallel_items_is_effective_and_bounded(self) -> None:
        active = 0
        maximum = 0
        guard = threading.Lock()
        release = threading.Event()

        def sleeper(_: float) -> None:
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
                if active == 2:
                    release.set()
            release.wait(1)
            with guard:
                active -= 1

        items = tuple(WorkflowItem(f"item-{index}", "change", 1) for index in range(4))
        ComplexWorkflowSimulationTool(sleeper=sleeper).execute(
            make_request(
                items=items,
                options=WorkflowProcessingOptions(
                    max_parallel_items=2, processing_delay_ms=1
                ),
            )
        )
        self.assertEqual(maximum, 2)
        for invalid in (True, 0, -1, 17, 1.5):
            with self.assertRaises(ValueError):
                WorkflowProcessingOptions(max_parallel_items=invalid)  # type: ignore[arg-type]
        for invalid in (True, -1, 5001, 1.5):
            with self.assertRaises(ValueError):
                WorkflowProcessingOptions(processing_delay_ms=invalid)  # type: ignore[arg-type]

    def test_safe_error_object_and_result_exclude_raw_exception(self) -> None:
        secret = "raw-secret-exception-text"

        def bad_sleeper(_: float) -> None:
            raise RuntimeError(secret)

        result = ComplexWorkflowSimulationTool(sleeper=bad_sleeper).execute(
            make_request(options=WorkflowProcessingOptions(processing_delay_ms=1))
        )
        serialized = json.dumps(result.to_dict())
        self.assertNotIn(secret, serialized)
        self.assertNotIn("Traceback", serialized)
        error = WorkflowSimulationError(
            "TOOL_UNKNOWN_FAILURE", WorkflowStage.PROCESS_ITEMS, "operation-1"
        )
        self.assertNotIn(secret, repr(error))
        self.assertEqual(
            set(vars(error)),
            {
                "safe_error_code",
                "safe_message",
                "stage",
                "operation_id",
                "side_effect_committed",
                "compensation_attempted",
                "compensation_succeeded",
            },
        )

    def test_audit_digest_does_not_contain_full_input(self) -> None:
        secret = "prompt-and-secret-body-must-not-persist"
        result = ComplexWorkflowSimulationTool().execute(
            make_request(
                mode=WorkflowExecutionMode.DRY_RUN,
                idempotency_key=None,
                items=(
                    WorkflowItem(
                        "item-1", "change", 1, attributes={"secret": secret}
                    ),
                ),
                metadata={"prompt": secret},
            )
        )
        self.assertIsNotNone(result.audit_digest)
        self.assertEqual(len(result.audit_digest or ""), 64)
        self.assertNotIn(secret, result.audit_digest or "")

    def test_json_state_store_only_writes_safe_atomic_file_in_explicit_directory(self) -> None:
        secret = "do-not-save-this-prompt"
        with tempfile.TemporaryDirectory() as directory:
            store = JsonFileWorkflowStateStore(directory)
            request = make_request(
                metadata={"prompt": secret},
                items=(WorkflowItem("item-1", "change", 1, attributes={"secret": secret}),),
            )
            first = ComplexWorkflowSimulationTool(state_store=store).execute(request)
            files = list(Path(directory).iterdir())
            self.assertEqual(files, [Path(directory) / store.FILE_NAME])
            self.assertNotIn(secret, files[0].read_text(encoding="utf-8"))
            reloaded = JsonFileWorkflowStateStore(directory)
            replay = ComplexWorkflowSimulationTool(state_store=reloaded).execute(request)
            self.assertEqual(first.status, WorkflowResultStatus.SUCCEEDED)
            self.assertEqual(replay.status, WorkflowResultStatus.IDEMPOTENCY_REPLAY)

    def test_legacy_wrapper_accepts_json_and_returns_safe_json(self) -> None:
        payload = {
            "operation_id": "legacy-operation",
            "resource_key": "mock-resource:legacy",
            "idempotency_key": None,
            "execution_mode": "DRY_RUN",
            "items": [{"item_id": "legacy-item", "action": "change", "quantity": 1}],
            "failure_injection": "NONE",
            "processing_options": {"max_parallel_items": 1},
            "metadata": {},
        }
        response = json.loads(complex_workflow_simulator(json.dumps(payload)))
        self.assertEqual(response["status"], "SUCCEEDED")
        invalid = json.loads(complex_workflow_simulator("not-json"))
        self.assertEqual(invalid["safe_error_code"], "TOOL_VALIDATION_ERROR")

    def test_registry_exposes_legacy_tool_name(self) -> None:
        class Router:
            def __init__(self) -> None:
                self.tools = {}

            def register_tool(self, name, func, description) -> None:
                self.tools[name] = (func, description)

        router = Router()
        register_all_tools(router)
        self.assertIn("complex_workflow_simulator", router.tools)
        self.assertIs(router.tools["complex_workflow_simulator"][0], complex_workflow_simulator)

    def test_agent_router_can_select_and_parse_the_legacy_tool_protocol(self) -> None:
        router = AgentRouter.__new__(AgentRouter)
        router.tools = {"complex_workflow_simulator": {"func": complex_workflow_simulator}}
        self.assertTrue(router._tool_intent_likely("run the complex workflow simulator"))
        parsed = router._parse_tool_call(
            'CALL: complex_workflow_simulator({"operation_id":"operation-1"})'
        )
        self.assertEqual(
            parsed,
            (
                "complex_workflow_simulator",
                '{"operation_id":"operation-1"}',
            ),
        )

    def test_input_validation_rejects_missing_key_duplicates_and_bool_numbers(self) -> None:
        with self.assertRaises(ValueError):
            make_request(idempotency_key=None)
        with self.assertRaises(ValueError):
            make_request(
                items=(
                    WorkflowItem("same", "change", 1),
                    WorkflowItem("same", "change", 2),
                )
            )
        for invalid in (True, -1, 0, 1.5):
            with self.assertRaises(ValueError):
                WorkflowItem("item", "change", invalid)  # type: ignore[arg-type]

    def test_all_success_stages_are_explicit_and_release_is_recorded(self) -> None:
        result = ComplexWorkflowSimulationTool().execute(
            make_request(mode=WorkflowExecutionMode.DRY_RUN, idempotency_key=None)
        )
        stages = [record.stage for record in result.completed_stages]
        self.assertEqual(
            stages,
            [
                WorkflowStage.VALIDATE_REQUEST,
                WorkflowStage.LOAD_EXISTING_STATE,
                WorkflowStage.ACQUIRE_RESOURCE,
                WorkflowStage.CREATE_SNAPSHOT,
                WorkflowStage.PREPARE_ITEMS,
                WorkflowStage.PROCESS_ITEMS,
                WorkflowStage.VALIDATE_PROCESSED_ITEMS,
                WorkflowStage.COMMIT_SIDE_EFFECTS,
                WorkflowStage.CREATE_AUDIT_RECORD,
                WorkflowStage.FINALIZE,
                WorkflowStage.RELEASE_RESOURCE,
            ],
        )

    def test_tool_has_no_runtime_status_retry_budget_or_external_io_dependency(self) -> None:
        import tools.complex_workflow_simulator as module

        source = inspect.getsource(module)
        self.assertNotIn("RunStatus", source)
        self.assertNotIn("StepStatus", source)
        self.assertNotIn("RunCoordinator", source)
        self.assertNotIn("CancellationToken", source)
        self.assertNotIn("requests.", source)
        self.assertNotIn("socket.", source)
        self.assertNotIn("retry(", source.lower())


if __name__ == "__main__":
    unittest.main()
