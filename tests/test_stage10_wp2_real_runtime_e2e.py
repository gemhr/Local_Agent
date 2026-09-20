"""Stage10-WP2 REAL_RUNTIME_E2E external-provider boundary.

The simulator is deliberately test-only.  It runs as a separate OS process,
keeps its state in a temporary directory independent from LocalAgent's
PostgreSQL, and exposes only a real HTTP operation/lookup boundary.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

import httpx
import pytest

from core.persistence.repositories.execution import DurableExecutionRepository
from core.runtime.execution_aggregate import ExecutionRootInput
from core.runtime.planning import TaskCapabilityRequirements, create_single_step_plan
from core.runtime.recovery_coordinator import RecoveryCoordinator, RecoveryCoordinatorConfig
from core.runtime.run_control import DurableRunControlService
from core.runtime.tool_contract import ToolInvocation
from core.runtime.tool_idempotency import (
    DurableToolInvocationService,
    ProviderReconciliationEvidence,
    ProviderReconciliationResult,
    ToolInvocationState,
)


_SIMULATOR = r'''
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import sys

state_path = Path(sys.argv[1])
ready_path = Path(sys.argv[2])
drop_response = os.environ.get("WP2_PROVIDER_DROP_RESPONSE") == "1"

def read_state():
    if not state_path.exists():
        return {}
    return json.loads(state_path.read_text(encoding="utf-8"))

def write_state(value):
    temp = state_path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.replace(temp, state_path)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def _json(self, status, value):
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/operation":
            self._json(404, {"error": "not_found"})
            return
        size = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(size))
        operation_id = payload["operation_id"]
        state = read_state()
        item = state.get(operation_id)
        if item is None:
            state[operation_id] = {"operation_id": operation_id, "status": "COMMITTED", "execute_count": 1}
            write_state(state)
        if drop_response:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            return
        self._json(200, state[operation_id])

    def do_GET(self):
        prefix = "/operation/"
        if not self.path.startswith(prefix):
            self._json(404, {"error": "not_found"})
            return
        operation_id = self.path[len(prefix):]
        item = read_state().get(operation_id)
        self._json(200 if item is not None else 404, item or {"status": "UNKNOWN"})

server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
ready_path.write_text(str(server.server_address[1]), encoding="ascii")
server.serve_forever()
'''


def _execution_root(run_id: str) -> ExecutionRootInput:
    return ExecutionRootInput(
        run_id=run_id,
        resume_input={"entry_agent_id": "core_router", "user_query": "wp2 e2e"},
        plan=create_single_step_plan("core_router", TaskCapabilityRequirements()),
        absolute_deadline=None,
        budget_totals={"max_model_calls": 1},
        budget_reserved={"model_calls": 0},
        budget_consumed={"model_calls": 0},
    )


class _ProviderProcess:
    def __init__(self, root: Path) -> None:
        self.state_path = root / "provider-state.json"
        self.ready_path = root / "provider-ready"
        env = dict(os.environ)
        env["WP2_PROVIDER_DROP_RESPONSE"] = "1"
        self.process = subprocess.Popen(
            [sys.executable, "-c", _SIMULATOR, str(self.state_path), str(self.ready_path)],
            cwd=str(Path(__file__).parents[1]),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        while not self.ready_path.exists():
            if self.process.poll() is not None:
                raise RuntimeError("provider simulator exited before readiness")
            if time.monotonic() >= deadline:
                raise TimeoutError("provider simulator readiness timeout")
            time.sleep(0.02)
        self.base_url = f"http://127.0.0.1:{self.ready_path.read_text(encoding='ascii')}"

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)

    def execute_count(self, operation_id: str) -> int:
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        return int(payload[operation_id]["execute_count"])


class _HttpProviderReconciler:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    async def reconcile_durable(self, record):
        if not record.provider_operation_id:
            return ProviderReconciliationResult.UNKNOWN
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.get(f"{self.base_url}/operation/{record.provider_operation_id}")
            if response.status_code != 200:
                return ProviderReconciliationResult.UNKNOWN
            payload = response.json()
        if payload.get("status") != "COMMITTED":
            return ProviderReconciliationResult.UNKNOWN
        return ProviderReconciliationEvidence(
            outcome=ProviderReconciliationResult.COMMITTED,
            result={
                "invocation_id": record.invocation_id,
                "attempt_id": "provider-reconciliation",
                "tool_name": record.tool_name,
                "status": "SUCCEEDED",
                "output": {
                    "content_type": "application/json",
                    "content": json.dumps({"operation_id": record.provider_operation_id}),
                    "original_size_bytes": 1,
                    "returned_size_bytes": 1,
                    "truncated": False,
                    "digest": "provider-result",
                },
                "safe_summary": "external provider committed",
                "side_effect_state": "COMMITTED",
                "idempotency_replayed": False,
                "retry_disposition": "SAFE_WITH_IDEMPOTENCY_KEY",
                "resource_key_digest": None,
                "started_at": datetime.now(UTC).isoformat(),
                "completed_at": datetime.now(UTC).isoformat(),
                "duration_ms": 0,
            },
        )


@pytest.mark.skipif(
    os.getenv("LOCAL_AGENT_RUN_REAL_RUNTIME_E2E") != "1",
    reason="set LOCAL_AGENT_RUN_REAL_RUNTIME_E2E=1 to run the real PostgreSQL/process E2E",
)
@pytest.mark.asyncio
async def test_real_runtime_e2e_response_loss_reconciles_without_duplicate_side_effect(clean_database):
    """REAL_RUNTIME_E2E: committed provider response is lost, then looked up."""
    with tempfile.TemporaryDirectory(prefix="localagent-wp2-provider-") as temporary:
        provider_process = _ProviderProcess(Path(temporary))
        try:
            operation_id = f"wp2-operation-{uuid.uuid4().hex}"
            async with httpx.AsyncClient(timeout=0.25, trust_env=False) as client:
                with pytest.raises(httpx.HTTPError):
                    await client.post(
                        f"{provider_process.base_url}/operation",
                        json={"operation_id": operation_id},
                    )
            assert provider_process.execute_count(operation_id) == 1

            run_id = f"wp2-e2e-{uuid.uuid4().hex}"
            control = DurableRunControlService(clean_database, lease_seconds=1)
            repository = DurableExecutionRepository(clean_database, control)
            initial_lease = await control.claim(run_id, "wp2-e2e-owner")
            await repository.initialize(_execution_root(run_id), lease=initial_lease)
            service = DurableToolInvocationService(clean_database)
            invocation = ToolInvocation.create(
                tool_name="external_http_provider",
                invocation_id=uuid.uuid4().hex,
                idempotency_key=operation_id,
                arguments={"operation_id": operation_id},
            )
            await service.prepare(
                lease=initial_lease,
                step_id="answer",
                invocation=invocation,
                tool_name=invocation.tool_name,
            )
            await service.start(lease=initial_lease, invocation_id=invocation.invocation_id)
            await service.unknown(
                lease=initial_lease,
                invocation_id=invocation.invocation_id,
                reason="POST_COMMIT_RESPONSE_LOST",
                provider_operation_id=operation_id,
            )
            await control.release(initial_lease)

            provider = _HttpProviderReconciler(provider_process.base_url)

            async def reconcile(candidate):
                lease = await control.claim(candidate.run_id, "wp2-e2e-reconciler")
                return await service.reconcile_durable_record(
                    lease=lease,
                    provider=provider,
                    invocation_id=candidate.invocation_id,
                )

            coordinator = RecoveryCoordinator(
                repository,
                control,
                lambda *_args: asyncio.sleep(0),
                config=RecoveryCoordinatorConfig(batch_size=1, reconciliation_max_concurrency=1),
            )
            coordinator.configure_reconciliation(reconcile)
            assert await coordinator.reconciliation_scan_once() == 1
            await asyncio.gather(*tuple(coordinator._reconciliation_tasks))
            final = await service.get(invocation.invocation_id)
            assert final is not None
            assert final.state is ToolInvocationState.COMMITTED
            assert final.provider_operation_id == operation_id
            assert provider_process.execute_count(operation_id) == 1
        finally:
            provider_process.close()
