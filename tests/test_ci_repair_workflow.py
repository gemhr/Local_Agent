"""CI 修复位置、Git 产物与审批的定向验证。"""

import asyncio
import hashlib
import json
import subprocess
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from core.agent_platform import ci_repair_artifacts as artifacts
from core.agent_platform import ci_repair_replay
from core.agent_platform.application import AgentApplicationService
from core.agent_platform.ci_diagnosis import CIInvestigationService
from core.agent_platform.ci_diagnosis_tools import TOOL_NAMES
from core.agent_platform.opencode_runtime_adapter import OpenCodeRuntimeAdapter
from core.agent_platform.registrations import BUSINESS_REGISTRATION_BUNDLE
from core.agent_platform.workflow import WorkflowError, WorkflowService
from core.agent_platform.workflow_artifacts import WorkflowArtifactError
from core.auth import AuthError, ObjectAuthorizationService
from core.chat_service import ChatService
from core.ci_simulation import tlv_input
from core.ci_simulation.models import CIExecutionRow
from core.incidents.models import IncidentRow, InvestigationRow
from core.persistence.models import UserRoleRow
from core.persistence.repositories.external_runs import ExternalRunRepository
from core.persistence.repositories.workflows import WorkflowRepository
from core.runtime.model_invocation import ModelAdapterResponse
from core.runtime.snapshot_serialization import sha256_digest, text_digest
from core.workspace.configuration import WorkspaceConfiguration
from core.workspace.contracts import WorkflowCopy, WorkflowEdit
from core.workspace.service import WorkspaceService
from tests.test_ci_adaptive_stop import build, calls
from tests.test_opencode_runtime_adapter import _assistant, _instance


TEMPLATE = next(item for item in BUSINESS_REGISTRATION_BUNDLE.workflows if item.workflow_id == "ci_repair_review")


def repository_at(path, *, autocrlf=False):
    path.mkdir()
    def git(*args):
        subprocess.run(["git", "-C", str(path), *args], capture_output=True, check=True, timeout=10)
    git("init", "--initial-branch=main")
    git("config", "core.autocrlf", "false")
    for name, content in tlv_input.load_baselines().items():
        file = path / name
        file.parent.mkdir(exist_ok=True)
        file.write_bytes(content.encode("utf-8"))
    git("add", "--", ".")
    git("-c", "user.name=CI Fixture", "-c", "user.email=ci@invalid", "commit", "-m", "Synthetic fixture")
    if autocrlf:
        git("config", "core.autocrlf", "true")
    return path


def update_target(path, *, crlf=False):
    target = path / artifacts.TARGET
    candidate = target.read_bytes().replace(b"100107", b"11020007")
    target.write_bytes(candidate.replace(b"\n", b"\r\n") if crlf else candidate)


def with_inconsistent_candidate(artifact):
    body = {k: v for k, v in artifact.items() if k != "artifact_digest"}
    body["after"] += "\n"
    body["after_sha256"] = text_digest(body["after"])
    return dict(body, artifact_digest=sha256_digest(body))


def test_repair_git_rejects_autocrlf_candidate(tmp_path):
    root = repository_at(tmp_path / "repo", autocrlf=True)
    baseline = artifacts.capture_baseline(root, "ci-repair")
    run = SimpleNamespace(repair_baseline=baseline, workflow_run_id=str(uuid4()))
    step = SimpleNamespace(step_id="repair", attempt=1, platform_run_id=str(uuid4()), runtime_instance_id="ci-repair")
    ref = dict(session_id="ses_repair", user_message_id="msg_user", message_id="msg_reply")
    target = root / artifacts.TARGET
    candidate = target.read_bytes().replace(b"100107", b"11020007").replace(b"\n", b"\r\n")
    index = (root / ".git" / "index").read_bytes()
    target.write_bytes(candidate)
    with pytest.raises(WorkflowArtifactError, match="CI_REPAIR_PATCH_INVALID"):
        artifacts.freeze_patch(run, step, ref)
    assert target.read_bytes() == candidate
    assert (root / ".git" / "index").read_bytes() == index


def test_repair_git_scope_and_frozen_bytes(tmp_path):
    root = repository_at(tmp_path / "repo")
    baseline = artifacts.capture_baseline(root, "ci-repair")
    run = SimpleNamespace(repair_baseline=baseline, workflow_run_id=str(uuid4()))
    step = SimpleNamespace(step_id="repair", attempt=1, platform_run_id=str(uuid4()), runtime_instance_id="ci-repair")
    ref = dict(session_id="ses_repair", user_message_id="msg_user", message_id="msg_reply")
    update_target(root)
    index = (root / ".git" / "index").read_bytes()
    artifact = artifacts.freeze_patch(run, step, ref)
    artifacts.verify_current(run, SimpleNamespace(artifact_ref=artifact))
    target = root / artifacts.TARGET
    assert target.read_bytes() == artifact["after"].encode("utf-8")
    assert b"\r" not in target.read_bytes()
    assert (root / ".git" / "index").read_bytes() == index
    applied = tmp_path / "apply" / artifacts.TARGET
    applied.parent.mkdir(parents=True)
    applied.write_bytes(artifact["before"].encode("utf-8"))
    subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "-C", str(tmp_path / "apply"), "apply", "-"],
        input=artifact["diff"].encode("utf-8"), capture_output=True, check=True, timeout=10)
    assert applied.read_bytes() == target.read_bytes()
    assert hashlib.sha256(applied.read_bytes()).hexdigest() == artifact["after_sha256"]
    assert '+  "request_tlv_hex": "11020007"' in artifact["diff"]
    assert artifact["before_sha256"] == tlv_input.BASELINE_DIGESTS[artifacts.TARGET]
    assert artifact["baseline_revision"] == baseline["revision"]
    with pytest.raises(WorkflowArtifactError, match="CI_REPAIR_ARTIFACT_INVALID"):
        artifacts.verify_frozen(dict(artifact, diff=artifact["diff"] + "\n"))
    with pytest.raises(WorkflowArtifactError, match="CI_REPAIR_PATCH_INVALID"):
        artifacts.verify_frozen(with_inconsistent_candidate(artifact))
    extra_diff = artifact["diff"] + (
        "diff --git a/cases/extra.json b/cases/extra.json\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/cases/extra.json\n@@ -0,0 +1 @@\n+{}\n")
    body = dict(artifact, diff=extra_diff, diff_digest=text_digest(extra_diff))
    del body["artifact_digest"]
    with pytest.raises(WorkflowArtifactError, match="CI_REPAIR_PATCH_INVALID"):
        artifacts.verify_frozen(dict(body, artifact_digest=sha256_digest(body)))
    extra = root / "cases" / "untracked.json"
    extra.write_bytes(b"{}")
    with pytest.raises(WorkflowArtifactError, match="CI_REPAIR_WORKTREE_SCOPE_INVALID"):
        artifacts.freeze_patch(run, step, ref)
    extra.unlink()
    subprocess.run(["git", "-C", str(root), "add", "--", artifacts.TARGET], capture_output=True, check=True, timeout=10)
    with pytest.raises(WorkflowArtifactError, match="CI_REPAIR_BASELINE_DRIFT"):
        artifacts.freeze_patch(run, step, ref)


@pytest.mark.parametrize(("case_id", "request_tlv", "reason", "value"), [
    (tlv_input.PRIMARY_CASE, "100107", tlv_input.STALE_ERROR, 7),
    (tlv_input.PRIMARY_CASE, "11020007", None, 7),
    (tlv_input.PRIMARY_CASE, "11010007", "TLV_INPUT_INVALID", None),
    (tlv_input.PRIMARY_CASE, "11020700", "TLV_BUSINESS_VALUE_MISMATCH", 1792),
    (tlv_input.CONTROL_CASE, "11020009", None, 9),
    (tlv_input.CONTROL_CASE, "11020008", "TLV_BUSINESS_VALUE_MISMATCH", 8),
])
def test_replay_oracle(case_id, request_tlv, reason, value):
    sources = tlv_input.load_baselines()
    script = json.loads(sources[tlv_input.SCRIPT_PATHS[case_id]])
    script[tlv_input.INPUT_FIELD] = request_tlv
    result = ci_repair_replay.evaluate_case(json.loads(sources[tlv_input.INTERFACE_PATH]), json.dumps(script), case_id)
    assert result["oracle_status"] == ("PASS" if reason is None else "FAIL")
    assert result["reason"] == reason and result["actual_value"] == value


def observed_model(messages, *, generation_options, insufficient=False, **_):
    observed = [json.loads(json.loads(m["content"])["content"]) for m in messages if m["role"] == "tool"]
    if not observed:
        assert any('"failure_mechanism": "TLV_INPUT_REVISION_STALE"' in m["content"]
                   for m in messages if m["role"] == "user")
        return calls(("context", TOOL_NAMES[0], {}))
    context, *reads = observed
    seen = {e["kind"]: e for e in reads}
    pending = [e for e in context["evidence"] if e["kind"] not in seen]
    if pending:
        return calls(*[("read-" + e["kind"], TOOL_NAMES[1], {"evidence_id": e["evidence_id"]}) for e in pending[:2]])
    assert generation_options["tool_choice"] == "none"
    report = dict(schema_version="ci-diagnosis.v1", kind="REPORT", incident_id=context["incident_id"],
        diagnosis_status="INSUFFICIENT_EVIDENCE" if insufficient else "SUPPORTED",
        failure_stage="AUTOMATION", failure_mechanism=seen["SCRIPT_BASELINE"]["content"]["precheck_error"],
        root_cause_hypothesis="旧 TLV 触发预检失败",
        supporting_evidence=[dict(evidence_id=e["evidence_id"], digest=e["digest"]) for e in reads],
        uncertainties=["受控不准入"] if insufficient else [], recommended_checks=["检查接口定义"],
        recommended_owner_role="TEST_OWNER")
    return ModelAdapterResponse(json.dumps(report, ensure_ascii=False), finish_reason="stop", tool_call_count=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["approved", "rejected", "unknown", "insufficient", "wrong_worktree", "crlf", "inconsistent", "drift", "recover"])
async def test_ci_repair_workflow_freezes_exact_approval(clean_database, tmp_path, monkeypatch, mode):
    from server import RunExecutionSupervisor
    from tests.test_stage6_wp2_ownership import _ROLE_IDS, _create_user, _ensure_roles, _principal

    root = repository_at(tmp_path / "repair", autocrlf=mode == "crlf")
    if mode == "inconsistent":
        freeze_patch = artifacts.freeze_patch
        monkeypatch.setattr(artifacts, "freeze_patch", lambda *args: with_inconsistent_candidate(freeze_patch(*args)))
    q = await build(clean_database, tmp_path / "native")
    monkeypatch.setattr(q.model, "invoke_request", lambda *args, **kwargs: observed_model(
        *args, **kwargs, insufficient=mode == "insufficient"))
    principal = q.principal
    instance, _ = await q.incidents.create_scenario(principal, tlv_input.SCENARIO_KEY, str(uuid4()))
    source, incident, _ = await q.incidents.execute(principal, instance.instance_id, str(uuid4()))
    transport_calls, stored = [], None

    def external(request):
        nonlocal stored
        transport_calls.append((request.method, request.url.path))
        assert request.url.params["directory"] == "/workspace"
        if request.url.path == "/path":
            return httpx.Response(200, json={"directory": "/workspace",
                "worktree": "/" if mode == "wrong_worktree" else "/workspace"})
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_repair", "directory": "/workspace"})
        if request.method == "GET":
            return httpx.Response(200, json=stored)
        data = json.loads(request.content)
        dto = json.loads(data["parts"][0]["text"])
        assert dto["execution_id"] == source.execution_id
        assert dto["allowed_paths"] == [artifacts.TARGET]
        update_target(root, crlf=mode == "crlf")
        if mode == "unknown":
            raise httpx.ReadTimeout("controlled uncertainty", request=request)
        stored = _assistant("ses_repair", data["messageID"], "已更新 TLV")
        return httpx.Response(200, json=stored)

    services = q.factory.services
    runs = ExternalRunRepository(clean_database, services.client_event_feed)
    external_instance = replace(_instance("ci-repair"), workspace="/workspace", repair_repository=str(root),
        use_instance_tool_permissions=True, allowed_principals=frozenset({(principal.tenant_id, principal.user_id)}))
    adapter = OpenCodeRuntimeAdapter(external_instance, runs=runs, run_registry=services.run_registry,
        transport=httpx.MockTransport(external))
    app = AgentApplicationService(ChatService(q.factory._router, coordinated_runtime_factory=q.factory),
        q.factory._agent_registry, runtime_instances={"ci-repair": adapter}, external_runs=runs)
    supervisor = RunExecutionSupervisor(max_active_runs=2)
    authz = ObjectAuthorizationService(clean_database)
    investigator = CIInvestigationService(q.incidents, app, q.repo, supervisor, authz, real_model=True)
    service = WorkflowService(WorkflowRepository(clean_database), {TEMPLATE.workflow_id: TEMPLATE}, app,
        authz, supervisor, services.run_registry, services.admission_gate, ci_investigation=investigator)
    config = WorkspaceConfiguration(clean_database, app, service, {TEMPLATE.workflow_id: TEMPLATE})
    spaces = WorkspaceService(clean_database)
    await _ensure_roles(clean_database)
    async with clean_database.transaction() as session:
        session.add(UserRoleRow(user_id=principal.user_id, role_id=_ROLE_IDS["USER"]))
    space = (await spaces.create_team(principal, "CI repair")).space
    reviewer = _principal(await _create_user(clean_database, ("USER",), tenant_id=principal.tenant_id),
                          "USER", tenant_id=principal.tenant_id)
    await spaces.put_member(principal, space.id, reviewer.user_id, "MEMBER")
    try:
        draft = await config.copy_workflow(principal, space.id, WorkflowCopy(
            template_id=TEMPLATE.workflow_id, template_version=TEMPLATE.workflow_version, name="Repair"))
        await config.save_workflow(principal, space.id, draft["id"], WorkflowEdit(
            name="Repair", expected_revision=1, runtime_instance_id="ci-repair"))
        published = await config.publish(principal, space.id, draft["id"], 2)
        assert [step["runtime_kind"] for step in published["versions"][0]["steps"]] == ["NATIVE", "OPENCODE", "REPLAY"]
        created = await service.create(principal, draft["id"], uuid4(), "1",
            incident_id=incident.incident_id, execution_id=source.execution_id, space_id=space.id)
        run_id = created["workflow_run_id"]

        async def finish_worker():
            worker = service._workers.get(run_id)
            if worker is not None:
                await asyncio.wait_for(asyncio.shield(worker), 30)
            return await service.get(principal, run_id)

        final = await finish_worker()
        if mode == "insufficient":
            assert not transport_calls and final["steps"][1]["platform_run_id"] is None
            assert final["status"] == "FAILED" and final["reason"] == "CI_REPAIR_INSUFFICIENT_EVIDENCE"
            return
        async with clean_database.session() as session:
            rows = (await session.scalars(select(InvestigationRow).where(InvestigationRow.incident_id == incident.incident_id))).all()
        investigation, = rows
        assert investigation.run_id == final["steps"][0]["platform_run_id"]
        assert investigation.session_id == f"{run_id}:diagnose" and investigation.projection_status == "READY"
        if mode == "wrong_worktree":
            assert final["status"] == "FAILED" and final["reason"] == "OPENCODE_REPAIR_WORKTREE_MISMATCH"
            assert transport_calls == [("GET", "/path")] and "approval" not in final
            return
        if mode == "unknown":
            assert final["status"] == "PAUSED" and "approval" not in final
            await service.resume(principal, run_id)
            await finish_worker()
            assert transport_calls.count(("POST", "/session/ses_repair/message")) == 1
            assert "approval" not in await service.get(principal, run_id)
            return
        if mode == "crlf":
            assert final["status"] == "FAILED" and final["reason"] == "CI_REPAIR_PATCH_INVALID"
            _, steps, approval = await service._repository.snapshot(run_id)
            assert steps[1].artifact_ref is None and approval is None
            assert steps[2].status == "PENDING" and steps[2].bound_input is None and steps[2].platform_run_id is None
            assert b"\r\n" in (root / artifacts.TARGET).read_bytes()
            return
        if mode == "inconsistent":
            assert final["status"] == "WAITING_APPROVAL"
            _, steps, approval = await service._repository.snapshot(run_id)
            patch = steps[1].artifact_ref
            kwargs = dict(binding_digest=approval.binding_digest, diff_digest=patch["diff_digest"])
            await spaces.put_member(principal, space.id, reviewer.user_id, "ADMIN")
            with pytest.raises(WorkflowError, match="CI_REPAIR_PATCH_INVALID"):
                await service.repair_artifact(reviewer, run_id)
            with pytest.raises(WorkflowError, match="CI_REPAIR_PATCH_INVALID"):
                await service.decide_approval(reviewer, run_id, approval.approval_id, "APPROVE", **kwargs)
            assert (await service.get(principal, run_id))["approval"]["status"] == "PENDING"
            # 仅在接纳旧批准时模拟历史校验，随后用生产校验检查真实持久行。
            with monkeypatch.context() as old_validation:
                old_validation.setattr(artifacts, "_verify_patch", lambda *_: None)
                # 历史批准时磁盘与封存的候选一致。
                (root / artifacts.TARGET).write_bytes(patch["after"].encode("utf-8"))
                approved = await service.decide_approval(reviewer, run_id, approval.approval_id, "APPROVE", **kwargs)
            assert approved["status"] == "APPROVED" and approved["consumed_at"] is None
            await service.resume(principal, run_id)
            invalidated = await finish_worker()
            assert invalidated["status"] == "PAUSED" and invalidated["reason"] == "CI_REPAIR_PATCH_INVALID"
            assert invalidated["approval"]["status"] == "INVALIDATED" and invalidated["approval"]["consumed_at"] is None
            assert invalidated["steps"][2]["platform_run_id"] is None
            return
        assert final["status"] == "WAITING_APPROVAL", final
        patch = await service.repair_artifact(reviewer, run_id)
        assert patch["opencode_run_id"] == final["steps"][1]["platform_run_id"]
        assert patch["external_session_id"] == "ses_repair"
        approval = final["approval"]
        kwargs = dict(binding_digest=approval["binding_digest"], diff_digest=patch["diff_digest"])
        with pytest.raises(AuthError):
            await service.decide_approval(reviewer, run_id, approval["approval_id"], "APPROVE", **kwargs)
        await spaces.put_member(principal, space.id, reviewer.user_id, "ADMIN")
        with pytest.raises(WorkflowError, match="CI_REPAIR_DIFF_MISMATCH"):
            await service.decide_approval(reviewer, run_id, approval["approval_id"], "APPROVE",
                binding_digest=kwargs["binding_digest"], diff_digest="0" * 64)
        decision = "REJECT" if mode == "rejected" else "APPROVE"
        decided = await service.decide_approval(reviewer, run_id, approval["approval_id"], decision, **kwargs)
        assert decided["consumed_at"] is None
        with pytest.raises(AuthError):
            await service.resume(reviewer, run_id)
        if mode == "drift":
            (root / artifacts.TARGET).write_bytes(b"{}\n")
            await service.resume(principal, run_id)
            drifted = await finish_worker()
            assert drifted["approval"]["status"] == "INVALIDATED"
            assert drifted["status"] == "PAUSED" and drifted["approval"]["consumed_at"] is None
            assert drifted["repair_replay"] is None and await service.repair_artifact(principal, run_id) == patch
            return
        if mode == "recover":
            async def interrupted(*args):
                await seal(*args)
                raise RuntimeError("controlled before commit")

            seal = ci_repair_replay.seal
            with monkeypatch.context() as fault:
                fault.setattr(ci_repair_replay, "seal", interrupted)
                await service.resume(principal, run_id)
                with pytest.raises(RuntimeError, match="controlled before commit"):
                    await finish_worker()
            interrupted_state = await service.get(principal, run_id)
            assert interrupted_state["repair_replay"] is None and interrupted_state["approval"]["consumed_at"] is None
        await service.resume(principal, run_id)
        if mode == "rejected":
            stopped = await service.get(principal, run_id)
            assert stopped["status"] == "REJECTED" and stopped["steps"][2]["platform_run_id"] is None
            return
        restored = await finish_worker()
        assert restored["status"] == "SUCCEEDED" and restored["steps"][2]["status"] == "SUCCEEDED", restored
        assert restored["approval"]["status"] == "APPROVED" and restored["approval"]["consumed_at"] is not None
        assert restored["steps"][2]["platform_run_id"] is None and transport_calls.count(("POST", "/session/ses_repair/message")) == 1
        replay = restored["repair_replay"]
        assert replay["technical_status"] == "COMPLETED" and replay["approval_id"] == approval["approval_id"]
        assert [v["oracle_status"] for v in replay["results"]["baseline"]] == ["FAIL", "PASS"]
        assert [v["oracle_status"] for v in replay["results"]["candidate"]] == ["PASS", "PASS"]
        assert replay["receipt_digest"] == sha256_digest({k: v for k, v in replay.items() if k != "receipt_digest"})
        async with clean_database.session() as session:
            assert (await session.get(IncidentRow, incident.incident_id)).status == "OPEN"
            assert (await session.get(CIExecutionRow, source.execution_id)).outcome == "FAIL"
        await service.resume(principal, run_id)
        assert await service.get(principal, run_id) == restored
        assert await service.repair_artifact(principal, run_id) == patch
    finally:
        await service.close()
        await adapter.close()
