"""受信 SERVICE 对已封存基带诊断的只读重验。"""
from typing import Literal
from pathlib import Path
from pydantic import Field, field_validator
from sqlalchemy import select
from core.incidents.contracts import StrictDTO, CIError
from core.incidents.models import IncidentRow, InvestigationRow
from core.ci_simulation.models import CIExecutionRow
from core.persistence.models import ObjectOwnershipRow, RuntimeLoopActionRow
from core.runtime.snapshot_serialization import sha256_digest, text_digest
from core.ci_simulation.baseband import AGENT_ID, validate
from core.agent_platform.ci_diagnosis import validate_report
from core.incidents.baseband_diagnosis import rule_report
from core.auth import require_scope

SCOPE = "localagent:baseband:evaluate"


class Grant(StrictDTO):
    project_id: str
    service_user_id: str
    tenant_id: str
    owner_user_id: str
    incident_ids: list[str] = Field(min_length=1)


class Configuration(StrictDTO):
    grants: list[Grant]


def load_configuration(path):
    return Configuration.model_validate_json(Path(path).read_text(encoding="utf-8")) if path else Configuration(grants=[])


class Selector(StrictDTO):
    incident_id: str
    investigation_id: str | None
    run_id: str | None
    candidate_kind: Literal["RULE", "AGENT"]


class Evaluate(StrictDTO):
    protocol: Literal["baseband-quality-request.v1"]
    project_id: str
    run_id: str
    attempt_id: str
    request_id: str
    case_id: str
    frozen_input_digest: str = Field(pattern="^[0-9a-f]{64}$")
    input: dict
    selector: Selector

    @field_validator("input")
    @classmethod
    def frozen_input(cls, value):
        return validate(value)


class EvaluateV2(Evaluate):
    protocol: Literal["baseband-quality-request.v2"]

    @field_validator("input")
    @classmethod
    def frozen_input(cls, value):
        from core.ci_simulation.oracle_chain import validate
        return validate(value)


async def evaluate(service, repository, config, principal, body):
    require_scope(principal, SCOPE)
    if principal.principal_kind != "SERVICE":
        raise CIError("BASEBAND_SERVICE_REQUIRED", 403)
    grant = next((g for g in config.grants if g.project_id == body.project_id and g.service_user_id == str(principal.user_id)
        and g.tenant_id == principal.tenant_id and body.selector.incident_id in g.incident_ids), None)
    if grant is None:
        raise CIError("BASEBAND_SOURCE_DENIED", 403)
    joint = isinstance(body, EvaluateV2)
    from core.ci_simulation.oracle_chain import validate as validate_joint
    if (validate_joint if joint else validate)(body.input) != body.input or sha256_digest(body.input) != body.frozen_input_digest:
        raise CIError("BASEBAND_INPUT_INVALID", 422)
    selector = body.selector
    async with service.database.session() as session:
        incident = await session.get(IncidentRow, selector.incident_id)
        if incident is None or incident.tenant_id != grant.tenant_id or str(incident.owner_user_id) != grant.owner_user_id:
            raise CIError("BASEBAND_SOURCE_DENIED", 403)
        execution = await session.get(CIExecutionRow, incident.execution_id)
        if execution.execution_snapshot.get("baseband") != body.input:
            raise CIError("BASEBAND_INPUT_BINDING_INVALID", 422)
        source = dict(candidate_kind=selector.candidate_kind, incident_id=incident.incident_id,
            execution_id=execution.execution_id, input_digest=body.frozen_input_digest,
            evidence_digest=execution.evidence_digest, agent_id=None, agent_version=None,
            investigation_id=None, run_id=None, session_id=None, final_status=None, raw_output_digest=None,
            model_calls=0, committed_tools=0, observed_batches=0)
        if selector.candidate_kind == "RULE":
            if selector.run_id is not None or selector.investigation_id is not None:
                raise CIError("BASEBAND_RULE_SELECTOR_INVALID", 422)
            from core.incidents.joint_diagnosis import rule_report as joint_rule
            report = (joint_rule if joint else rule_report)(incident.incident_id, execution)
            source["rule_version"] = "baseband-rule.v2" if joint else "baseband-rule.v1"
        else:
            row = await session.get(InvestigationRow, selector.investigation_id)
            if (row is None or row.incident_id != incident.incident_id or row.run_id != selector.run_id
                    or row.agent_id != AGENT_ID or row.agent_version != ("1.1.0" if joint else "1.0.0") or row.projection_status != "READY"):
                raise CIError("BASEBAND_AGENT_BINDING_INVALID", 422)
            from core.runtime.memory_scope import MemoryAccessContext
            access = MemoryAccessContext(incident.tenant_id, str(incident.owner_user_id), AGENT_ID, row.session_id)
            for kind, identity in (("RUN", row.run_id), ("CONVERSATION", access.conversation_id)):
                owner = await session.get(ObjectOwnershipRow, (kind, identity))
                if owner is None or owner.owner_user_id != incident.owner_user_id or owner.tenant_id != incident.tenant_id:
                    raise CIError("BASEBAND_AGENT_OWNER_INVALID", 422)
            image = await repository.load(row.run_id, session=session)
            if image is None or image.root.status != "SUCCEEDED":
                raise CIError("BASEBAND_AGENT_NOT_COMPLETE", 422)
            actions = (await session.scalars(select(RuntimeLoopActionRow).where(RuntimeLoopActionRow.run_id == row.run_id))).all()
            steps = [s for s in image.steps if s.status == "SUCCEEDED"]
            raw = steps[0].typed_result_payload.get("content", "") if len(steps) == 1 else ""
            report = validate_report(raw, row, incident, execution, image, actions)
            if report != row.report_payload or text_digest(raw) != row.runtime_result_digest:
                raise CIError("BASEBAND_SEALED_REPORT_CHANGED", 422)
            source.update(agent_id=row.agent_id, agent_version=row.agent_version, investigation_id=row.investigation_id,
                run_id=row.run_id, session_id=row.session_id, final_status="DELIVERED", raw_output_digest=text_digest(raw),
                model_calls=sum(m.state == "COMPLETED" for m in image.models),
                committed_tools=sum(t.state == "COMMITTED" for t in image.tool_invocations),
                observed_batches=sum(i.state == "OBSERVED" for i in image.loop_iterations))
        if joint:
            source["observed_reads"] = []
            if selector.candidate_kind == "AGENT":
                from core.agent_platform.ci_evidence import observed_sources
                frame = next(f for f in image.loop_frames if f.frame_role == "ROOT")
                final = next(i for i in image.loop_iterations if i.decision_id == frame.result_decision_id)
                seen = observed_sources(image, actions, frame, incident.incident_id, execution.execution_id,
                    before_iteration=final.iteration_no, observed_reads=source["observed_reads"])
                originals = {e["evidence_id"]: e for e in execution.evidence_manifest}
                if len(source["observed_reads"]) > 8 or any(v != dict(incident_id=incident.incident_id, **originals[k]) for k,v in seen.items()):
                    raise CIError("CI_REPORT_SOURCE_INVALID")
        return dict(protocol="baseband-quality-output.v2" if joint else "baseband-quality-output.v1", **{k: getattr(body, k) for k in
            ("project_id", "run_id", "attempt_id", "request_id", "case_id", "frozen_input_digest")},
            input=body.input, source=source, report=report, report_digest=sha256_digest(report),
            evidence=execution.evidence_manifest)
