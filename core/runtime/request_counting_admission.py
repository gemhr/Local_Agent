"""唯一受控 CI E2E 的计量豁免；不改变原始计量评估或预算 Owner。"""
from dataclasses import asdict, dataclass, is_dataclass
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator
from core.runtime.execution_aggregate import canonical_payload_digest as digest

STRATEGY = "E2E_TEST_UNVERIFIED_REQUEST_COUNTING"
AGENT = "ci_incident_investigator"
EXECUTION = "durable-agent-loop.v2"
LIMITS = dict(max_iterations=8, max_model_calls=8, max_tool_calls=8, max_retries=0,
    max_elapsed_seconds=180, max_batch_size=2, max_batch_concurrency=1,
    failure_mode="FAIL_FAST", total_tokens=32768, cost_units=65536)
Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class FrozenCI(_Strict):
    case_id: StrictStr
    case_version: StrictStr
    case_digest: Digest
    template_id: Literal["report_pipeline_failure"]
    template_version: Literal["2"]
    input_digest: Digest


class Policy(_Strict):
    schema_version: Literal["e2e-request-counting-policy.v1"]
    strategy: Literal["E2E_TEST_UNVERIFIED_REQUEST_COUNTING"]
    execution_env: Literal["E2E_TEST"]
    tenant_id: StrictStr = Field(min_length=1, max_length=255)
    project_id: StrictStr
    principal_id: StrictStr
    agent_id: Literal["ci_incident_investigator"]
    execution_policy: Literal["durable-agent-loop.v2"]
    frozen_ci: FrozenCI
    mode: Literal["CANDIDATE"]
    candidate_id: StrictStr = Field(min_length=1, max_length=255)
    candidate_manifest_digest: Digest
    prompt_digest: Digest
    profile_id: Literal["remote_advanced"]
    provider_kind: Literal["deepseek"]
    model_identity: Literal["deepseek-flash"]
    loop_limits: dict
    max_tokens: Literal[4096]
    provider_timeout_seconds: Literal[120]

    @model_validator(mode="after")
    def exact_limits(self):
        if self.loop_limits != LIMITS or any(type(self.loop_limits[k]) is not type(v) for k, v in LIMITS.items()):
            raise ValueError("E2E_ADMISSION_LIMITS_INVALID")
        for value in (self.project_id, self.principal_id, self.frozen_ci.case_id):
            if str(UUID(value)) != value:
                raise ValueError("E2E_ADMISSION_ID_INVALID")
        return self


class Authorization(_Strict):
    evaluation_run_id: StrictStr
    attempt_id: StrictStr
    request_id: StrictStr
    run_id: StrictStr
    execution_manifest_digest: Digest

    @model_validator(mode="after")
    def exact_ids(self):
        for value in (self.evaluation_run_id, self.attempt_id, self.request_id, self.run_id):
            if str(UUID(value)) != value:
                raise ValueError("E2E_ADMISSION_ID_INVALID")
        if self.run_id != self.attempt_id:
            raise ValueError("E2E_ADMISSION_ATTEMPT_INVALID")
        return self


class PairPolicy(_Strict):
    schema_version: Literal['e2e-request-counting-policy.v2']
    strategy: Literal['E2E_TEST_UNVERIFIED_REQUEST_COUNTING']
    execution_env: Literal['E2E_TEST']
    tenant_id: StrictStr
    project_id: StrictStr
    principal_id: StrictStr
    operator_id: StrictStr = Field(min_length=1,max_length=255)
    execution_source: Literal['OPERATOR_FROZEN_CI']
    plan_id: StrictStr
    plan_version: StrictStr
    agent_id: Literal['ci_incident_investigator']
    execution_policy: Literal['durable-agent-loop.v2']
    profile_id: Literal['remote_advanced']
    provider_kind: Literal['deepseek']
    model_identity: Literal['deepseek-flash']
    loop_limits: dict
    max_tokens: Literal[4096]
    provider_timeout_seconds: Literal[120]
    @model_validator(mode='after')
    def exact_limits(self):
        if self.loop_limits != LIMITS or any(type(self.loop_limits[k]) is not type(v) for k,v in LIMITS.items()):
            raise ValueError('E2E_ADMISSION_LIMITS_INVALID')
        for value in (self.project_id,self.principal_id,self.plan_id):
            if str(UUID(value))!=value: raise ValueError('E2E_ADMISSION_ID_INVALID')
        return self

class PairAuthorization(Authorization):
    plan_digest: Digest
    mode: Literal['BASELINE','CANDIDATE']
    frozen_ci: dict
    candidate_id: StrictStr
    candidate_manifest_digest: Digest
    prompt_digest: Digest
    runtime_config_digest: Digest
    definition_digest: Digest
    @model_validator(mode='after')
    def exact_case(self):
        from core.incidents.evaluation_input import FrozenPair
        frozen=FrozenPair.model_validate(self.frozen_ci)
        for value in (frozen.case_id,frozen.dataset_id,frozen.plan_id):
            if str(UUID(value))!=value: raise ValueError('E2E_ADMISSION_ID_INVALID')
        return self

def compile_policy(value):
    return (PairPolicy if value.get('schema_version')=='e2e-request-counting-policy.v2' else Policy).model_validate(value).model_dump()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class TrustedException:
    """以不可变序列化值冻结启动输入；属性返回副本，不能热替换内部字典。"""
    policy_json: str
    authorization_json: str | None

    @property
    def policy(self):
        return json.loads(self.policy_json)

    @property
    def authorization(self):
        return json.loads(self.authorization_json) if self.authorization_json else None


@dataclass(frozen=True)
class BoundAdmission:
    record_json: str

    @property
    def record(self):
        return json.loads(self.record_json)


def compile_exception(value, *, live=True):
    if value is None:
        return None
    if type(value) is not dict or set(value) - {"policy", "authorization"} or "policy" not in value:
        raise ValueError("E2E_ADMISSION_CONFIGURATION_INVALID")
    policy = compile_policy(value["policy"])
    authorization = (PairAuthorization if policy["schema_version"]=="e2e-request-counting-policy.v2" else Authorization).model_validate(value["authorization"]).model_dump() if value.get("authorization") is not None else None
    if live and authorization is None:
        raise ValueError("E2E_ADMISSION_AUTHORIZATION_REQUIRED")
    return TrustedException(_json(policy), _json(authorization) if authorization else None)


def match_request(trusted, request, principal, definition):
    """仅内部受信配置可调用；HTTP 请求不能构造 TrustedException。"""
    if trusted is None:
        return None
    from core.auth import EVALUATION_EXECUTE_SCOPE, CI_SYNTHETIC_EVALUATE_SCOPE, require_scope
    from core.runtime.agent_loop.quality_contract import catalog_digest
    require_scope(principal, EVALUATION_EXECUTE_SCOPE)
    require_scope(principal, CI_SYNTHETIC_EVALUATE_SCOPE)
    if trusted.policy['schema_version']=='e2e-request-counting-policy.v2':
        return match_pair_request(trusted,request,principal,definition)
    p, a, q = trusted.policy, trusted.authorization, request.quality
    selection = definition.business_options.get("ci_prompt_selection", {})
    if (a is None or principal.principal_kind != "SERVICE" or str(principal.user_id) != p["principal_id"]
            or principal.tenant_id != p["tenant_id"] or str(q.evaluation_project_id) != p["project_id"]
            or definition.agent_id != p["agent_id"] or q.agent_id != p["agent_id"]
            or definition.business_options.get("execution_policy") != p["execution_policy"]
            or dict(definition.business_options.get("loop_policy", {})) != p["loop_limits"]
            or request.mode != p["mode"] or request.frozen_ci.model_dump() != p["frozen_ci"]
            or (str(q.case_id), q.case_version, q.case_digest) !=
                (p["frozen_ci"]["case_id"], p["frozen_ci"]["case_version"], p["frozen_ci"]["case_digest"])
            or request.candidate_manifest_digest != p["candidate_manifest_digest"]
            or selection.get("candidate_id") != p["candidate_id"] or selection.get("mode") != p["mode"]
            or selection.get("manifest_digest") != p["candidate_manifest_digest"]
            or selection.get("prompt_digest") != p["prompt_digest"] or catalog_digest(definition.instructions) != p["prompt_digest"]
            or any(str(getattr(q, k)) != v for k, v in a.items())
            or q.timeout_milliseconds > p["loop_limits"]["max_elapsed_seconds"] * 1000):
        raise ValueError("E2E_ADMISSION_REQUEST_MISMATCH")
    return trusted


def issue_record(trusted, binding, receipt):
    p, a = trusted.policy, trusted.authorization
    record = dict(schema_version="e2e-request-counting-admission.v2" if p["schema_version"]=="e2e-request-counting-policy.v2" else "e2e-request-counting-admission.v1", policy=p, policy_digest=digest(p),
        authorization=a, session_id=binding["session_id"], binding=binding,
        receipt_correlation_digest=digest(receipt.correlation),
        REQUEST_COUNTING_RELIABILITY="UNVERIFIED", RELIABLE_TOKEN_ADMISSION="NOT_VERIFIED")
    record["admission_digest"] = digest(record)
    return record


def reference(record):
    return dict(strategy=STRATEGY, policy_digest=record["policy_digest"], admission_digest=record["admission_digest"],
        RELIABLE_TOKEN_ADMISSION="NOT_VERIFIED")


def validate_record(record, *, root=None, frame=None, receipt=None):
    """验证原持久 Owner 的绑定；无记录默认拒绝，历史读取不授予新执行权限。"""
    if type(record) is not dict:
        return False
    try:
        trusted = compile_exception({"policy": record["policy"], "authorization": record["authorization"]})
        p, a, b = trusted.policy, trusted.authorization, record["binding"]
        pair=p['schema_version']=='e2e-request-counting-policy.v2'
        if pair:
            if not validate_pair_binding(p,a,b):return False
            compared={**p,'mode':a['mode'],'candidate_manifest_digest':a['candidate_manifest_digest'],'frozen_ci':a['frozen_ci']}
        else:compared=p
        expected = dict(record)
        expected.pop("admission_digest")
        if (set(record) != {"schema_version", "policy", "policy_digest", "authorization", "session_id", "binding",
                "receipt_correlation_digest", "REQUEST_COUNTING_RELIABILITY", "RELIABLE_TOKEN_ADMISSION", "admission_digest"}
                or record["schema_version"] != ("e2e-request-counting-admission.v2" if pair else "e2e-request-counting-admission.v1")
                or record["REQUEST_COUNTING_RELIABILITY"] != "UNVERIFIED" or record["RELIABLE_TOKEN_ADMISSION"] != "NOT_VERIFIED"
                or digest(expected) != record["admission_digest"] or digest(p) != record["policy_digest"]
                or b["run_id"] != a["run_id"] or b["agent_id"] != p["agent_id"] or b["mode"] != compared["mode"]
                or b["manifest_digest"] != compared["candidate_manifest_digest"] or b["frozen_ci"] != compared["frozen_ci"]
                or record["session_id"] != b["session_id"] or b["source_type"] != "SYNTHETIC"
                or any(str(b["quality_correlation"].get(k)) != a[k] for k in ("evaluation_run_id","attempt_id","request_id","run_id","execution_manifest_digest"))
                or digest(b["quality_correlation"]) != record["receipt_correlation_digest"]):
            return False
        if receipt is not None and (receipt.evaluation_capture.get("e2e_test_admission") != record
                or receipt.remote_run_id != a["run_id"] or receipt.tenant_id != p["tenant_id"]
                or str(receipt.principal_id) != p["principal_id"] or receipt.evaluation_project_id != p["project_id"]
                or digest(receipt.correlation) != record["receipt_correlation_digest"]):
            return False
        if root is not None:
            r, loop = root.resume_input, root.resume_input.get("agent_loop") or {}
            if (root.run_id != a["run_id"] or root.execution_policy != p["execution_policy"]
                    or r.get("resolved_agent_id") != p["agent_id"] or r.get("session_id") != record["session_id"]
                    or loop.get("e2e_test_admission") != record or loop.get("pinned_config", {}).get("request_counting_exception") != p
                    or loop.get("correlation") != {k: a[k] for k in ("evaluation_run_id", "attempt_id", "request_id")}):
                return False
        if frame is not None and (frame.frame_role != "ROOT" or frame.schema_version != 2
                or frame.run_id != a["run_id"] or frame.execution_agent_id != p["agent_id"]
                or frame.pinned_config.get("request_counting_exception") != p):
            return False
        if pair and frame is not None:
            from core.runtime.agent_loop.quality_contract import quality_runtime_config_digest
            if quality_runtime_config_digest(frame.pinned_config)!=a['runtime_config_digest']:return False
        return True
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def bind_context(trusted, receipt, context, access, *, rehydrated_root=None):
    record = (receipt.evaluation_capture or {}).get("e2e_test_admission") if receipt else None
    historical = ((rehydrated_root.resume_input.get("agent_loop") or {}).get("e2e_test_admission") if rehydrated_root else None)
    if record is None and historical is None:
        return None
    if (trusted is None or not validate_record(record, receipt=receipt, root=rehydrated_root)
            or record["policy"] != trusted.policy or record["authorization"] != trusted.authorization
            or access is None or access.principal_kind != "SERVICE" or str(access.user_id) != trusted.policy["principal_id"]
            or access.tenant_id != trusted.policy["tenant_id"] or access.agent_id != AGENT
            or context.run_id != record["authorization"]["run_id"] or context.session_id != record["session_id"]
            or access.session_id != record["session_id"] or context.execution_policy != EXECUTION
            or context.loop_correlation != {k: record["authorization"][k] for k in ("evaluation_run_id", "attempt_id", "request_id")}):
        raise ValueError("E2E_ADMISSION_CONTEXT_MISMATCH")
    return BoundAdmission(_json(record))


def context_record(context):
    bound = getattr(context, "request_counting_admission", None)
    if (not isinstance(bound, BoundAdmission) or getattr(context, "delegation_id", None)
            or context.execution_policy != EXECUTION):
        return None
    record = bound.record
    if (not validate_record(record) or context.run_id != record["authorization"]["run_id"]
            or context.session_id != record["session_id"]
            or context.loop_correlation != {k: record["authorization"][k] for k in ("evaluation_run_id", "attempt_id", "request_id")}):
        return None
    return record


def eligible(assessment, record=None, *, profile=None, frame=None, purpose="DECISION"):
    a = asdict(assessment) if is_dataclass(assessment) else assessment
    if not a:
        return False
    if a.get("admissible") is True:
        return True
    if not validate_record(record, frame=frame) or purpose not in {"DECISION", "COMPACTION"}:
        return False
    p = record["policy"]
    if (a.get("mode") != "HEURISTIC" or a.get("reliable") is not False or a.get("admissible") is not False
            or a.get("profile_id") != p["profile_id"]):
        return False
    if profile is not None:
        if is_dataclass(profile):
            if (not profile.effective_is_remote or profile.provider_kind != p["provider_kind"]
                    or profile.model_identity != p["model_identity"] or profile.request_counting_mode != "HEURISTIC"):
                return False
            maximum, window = profile.max_output_tokens, profile.context_window
        else:
            if (profile.get("is_remote") is not True or profile.get("provider_kind") != p["provider_kind"]
                    or profile.get("model_identity") != p["model_identity"] or profile.get("request_counting_mode") != "HEURISTIC"):
                return False
            maximum, window = profile["max_output_tokens"], profile["context_window"]
    else:
        return False
    return (all(type(a.get(k)) is int and a[k] >= 0 for k in ("input_bound", "output_reserve", "required_window", "window"))
        and 0 < a["output_reserve"] <= min(maximum, p["max_tokens"])
        and a["input_bound"] + a["output_reserve"] <= a["required_window"] <= a["window"] == window)


def context_eligible(assessment, context, profiles, *, frame=None, purpose=None):
    profile = next((p for p in profiles if p.profile_id.value == assessment.profile_id), None)
    return eligible(assessment, context_record(context), profile=profile, frame=frame,
        purpose=purpose or context.loop_model_purpose)


def persisted_eligible(assessment, root, frame, receipt=None, *, purpose="DECISION"):
    if frame is None:
        return False
    record = (root.resume_input.get("agent_loop") or {}).get("e2e_test_admission")
    if not validate_record(record, root=root, frame=frame, receipt=receipt):
        return False
    profile = next((p for p in frame.pinned_config.get("profiles", []) if p["profile_id"] == assessment.get("profile_id")), None)
    return (assessment.get("e2e_test_admission") == reference(record)
        and eligible(assessment, record, profile=profile, frame=frame, purpose=purpose))


def persisted_validation(validation, root, frame, receipt=None):
    if frame is None:
        return False
    record = (root.resume_input.get("agent_loop") or {}).get("e2e_test_admission")
    return (validation.get("budget") is False and validate_record(record, root=root, frame=frame, receipt=receipt)
        and validation.get("e2e_test_admission") == reference(record))


def validate_pair_binding(p,a,b):
    f=a['frozen_ci']
    q=b['quality_correlation']
    return (b['contract_version']=='ci-synthetic-evaluation-execute.v2' and f['plan_id']==p['plan_id']
        and f['plan_version']==p['plan_version'] and b['plan_digest']==a['plan_digest'] and b['frozen_ci']==f and b['mode']==a['mode']
        and q['evaluation_project_id']==p['project_id'] and q['agent_id']==p['agent_id']
        and (q['case_id'],q['case_version'],q['case_digest'])==(f['case_id'],f['case_version'],f['case_digest']))

def match_pair_request(trusted,request,principal,definition):
    from core.runtime.agent_loop.quality_contract import catalog_digest
    p,a,q=trusted.policy,trusted.authorization,request.quality
    pin=definition.business_options.get('ci_prompt_selection',{})
    frozen=request.frozen_ci.model_dump(exclude_none=True)
    if (a is None or request.contract_version!='ci-synthetic-evaluation-execute.v2'
            or principal.principal_kind!='SERVICE' or str(principal.user_id)!=p['principal_id']
            or principal.tenant_id!=p['tenant_id'] or str(q.evaluation_project_id)!=p['project_id']
            or definition.agent_id!=p['agent_id'] or q.agent_id!=p['agent_id']
            or definition.business_options.get('execution_policy')!=p['execution_policy']
            or dict(definition.business_options.get('loop_policy',{}))!=p['loop_limits']
            or frozen!=a['frozen_ci'] or frozen['plan_id']!=p['plan_id'] or frozen['plan_version']!=p['plan_version']
            or request.plan_digest!=a['plan_digest'] or request.mode!=a['mode'] or request.candidate_manifest_digest!=a['candidate_manifest_digest']
            or any(pin.get(k)!=a[{'manifest_digest':'candidate_manifest_digest'}.get(k,k)] for k in ('candidate_id','manifest_digest','mode','prompt_digest'))
            or catalog_digest(definition.instructions)!=a['prompt_digest']
            or any(str(getattr(q,k))!=a[k] for k in ('evaluation_run_id','attempt_id','request_id','run_id','execution_manifest_digest'))
            or (str(q.case_id),q.case_version,q.case_digest)!=(frozen['case_id'],frozen['case_version'],frozen['case_digest'])
            or q.timeout_milliseconds>p['loop_limits']['max_elapsed_seconds']*1000):
        raise ValueError('E2E_PAIR_ADMISSION_REQUEST_MISMATCH')
    from core.runtime.agent_loop.context import _pin_value
    if catalog_digest(_pin_value(definition))!=a['definition_digest']:
        raise ValueError('E2E_PAIR_DEFINITION_MISMATCH')
    return trusted
