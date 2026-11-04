"""调查期间与最终报告共用的已观察证据来源规则。"""
from sqlalchemy import select

from core.agent_platform.ci_diagnosis_tools import TOOL_NAMES, strict_object
from core.ci_simulation.models import CIExecutionRow
from core.incidents.models import InvestigationRow, IncidentRow
from core.incidents.service import AGENT_ID
from core.runtime.snapshot_serialization import sha256_digest
from core.runtime.tool_contract import canonical_json_digest

REQUIRED_KINDS = {"PARSER_LOG", "ENVIRONMENT_RECORD", "CASE_REQUIREMENTS"}


def observed_sources(image, actions, frame, incident_id, execution_id, *, before_iteration=None):
    observed = {i.iteration_no: i for i in image.loop_iterations if i.frame_id == frame.frame_id
        and i.state == "OBSERVED" and (before_iteration is None or i.iteration_no < before_iteration) and i.observation is not None}
    tools = {t.invocation_id: t for t in image.tool_invocations if t.state == "COMMITTED"}
    seen = {}
    for action in actions:
        tool, iteration = tools.get(action.invocation_id), observed.get(action.iteration_no)
        if (action.frame_id != frame.frame_id or iteration is None or tool is None or tool.tool_name != TOOL_NAMES[1]
                or action.source_digest != tool.committed_result_digest or canonical_json_digest(tool.committed_result) != tool.committed_result_digest
                or action.result_digest != sha256_digest(action.result_projection)):
            continue
        result = tool.committed_result
        if result["status"] != "SUCCEEDED":
            continue
        projection = {"outcome": result["status"], "content": result["output"]["content"]}
        if action.result_projection != projection or not any(r.get("result") == projection for r in iteration.observation.get("results", [])):
            continue
        try:
            evidence = strict_object(result["output"]["content"])
        except (TypeError, ValueError):
            continue
        if isinstance(evidence, dict) and evidence.get("incident_id") == incident_id and evidence.get("execution_id") == execution_id:
            seen[evidence["evidence_id"]] = evidence
    return seen


def canonical_sources(seen, incident_id, execution):
    originals = {e["evidence_id"]: e for e in execution.evidence_manifest}
    return {key: value for key, value in seen.items() if key in originals
        and originals[key]["availability"] == "AVAILABLE" and value == dict(incident_id=incident_id, **originals[key])}


def minimum_coverage(seen):
    kinds = {e["kind"] for e in seen.values()}
    environment = next((e["content"] for e in seen.values() if e["kind"] == "ENVIRONMENT_RECORD"), None)
    return REQUIRED_KINDS <= kinds and isinstance(environment, dict) and all(
        environment.get(k) is not None for k in ("parser_supported_field_revision", "decoder_supported_field_revision"))


async def investigation_evidence(session, root, frame, image, actions):
    """只查原调查绑定与不可变执行 manifest，不以 Memory 或模型文本构造授权。"""
    row = await session.scalar(select(InvestigationRow).where(InvestigationRow.run_id == root.run_id))
    identity = root.resume_input
    if (row is None or row.agent_id != AGENT_ID or frame.execution_agent_id != AGENT_ID
            or identity.get("resolved_agent_id") != row.agent_id or identity.get("agent_version") != row.agent_version
            or identity.get("session_id") != row.session_id):
        return {}, False
    incident = await session.get(IncidentRow, row.incident_id)
    execution = await session.get(CIExecutionRow, incident.execution_id)
    seen = canonical_sources(observed_sources(image, actions, frame, incident.incident_id, execution.execution_id), incident.incident_id, execution)
    # 当前唯一覆盖规则是上述三类及环境能力；没有通用自然语言反证推断。
    return seen, minimum_coverage(seen)
