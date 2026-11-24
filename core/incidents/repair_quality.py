"""只读封存证据以及按 Workspace 关联的 AgentEvalOps Gate。"""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import field_validator

from core.auth import AuthorizationAction, require_scope
from core.incidents.contracts import CIError, StrictDTO
from core.runtime.snapshot_serialization import text_digest

SCOPE = "localagent:ci-repair:evidence"


class Grant(StrictDTO):
    project_id: str
    service_user_id: str
    tenant_id: str
    space_id: str
    owner_user_id: str


class GateSource(StrictDTO):
    api_origin: str
    web_origin: str
    project_id: str
    token_env: str

    @field_validator("project_id")
    @classmethod
    def validate_project(cls, value):
        return str(UUID(value))

    @field_validator("api_origin", "web_origin")
    @classmethod
    def validate_origin(cls, value):
        url = urlsplit(value)
        if (not url.hostname or url.username is not None or url.password is not None or url.query or url.fragment
                or url.path not in {"", "/"} or (url.scheme != "https" and
                    (url.scheme != "http" or url.hostname not in {"127.0.0.1", "::1", "localhost"}))):
            raise ValueError("CI_REPAIR_GATE_ORIGIN_INVALID")
        return value.rstrip("/")


class Configuration(StrictDTO):
    grants: list[Grant]
    gate_sources: list[GateSource] = []


def load_configuration(path):
    return Configuration.model_validate_json(Path(path).read_text(encoding="utf-8")) if path else Configuration(grants=[])


async def export(repository, config, principal, run_id, digest, project_id):
    require_scope(principal, SCOPE)
    grants = [g for g in config.grants if (g.project_id, g.service_user_id, g.tenant_id)
              == (project_id, str(principal.user_id), principal.tenant_id)]
    if not grants:
        raise CIError("CI_REPAIR_EVIDENCE_DENIED", 403)
    run, steps, approval = await repository.snapshot(run_id, lock=False)
    if run is None or not any((g.space_id, g.owner_user_id, g.tenant_id)
                              == (str(run.space_id), str(run.owner_user_id), run.tenant_id) for g in grants):
        raise CIError("CI_REPAIR_EVIDENCE_NOT_FOUND", 404)
    ref = (steps[2].result_ref or {}) if run.repair_baseline is not None and len(steps) == 3 else {}
    evidence = ref.get("evidence")
    if (run.status != "SUCCEEDED" or approval is None or approval.status != "APPROVED" or approval.consumed_at is None
            or ref.get("evidence_digest") != digest or not isinstance(evidence, str) or text_digest(evidence) != digest):
        raise CIError("CI_REPAIR_EVIDENCE_NOT_FOUND", 404)
    return evidence.encode("utf-8")


async def read_gate(workflow, config, principal, run_id, gate_id):
    await workflow._authorize(principal, run_id, AuthorizationAction.READ, share_workspace_read=True)
    run, steps, _ = await workflow._repository.snapshot(run_id, lock=False)
    if run.repair_baseline is None or run.status != "SUCCEEDED":
        raise CIError("CI_REPAIR_GATE_NOT_CONFIRMED", 404)
    evidence = (steps[2].result_ref or {}).get("evidence_digest")
    projects = {g.project_id for g in config.grants if (g.tenant_id, g.owner_user_id, g.space_id)
                == (run.tenant_id, str(run.owner_user_id), str(run.space_id))}
    sources = [s for s in config.gate_sources if s.project_id in projects]
    if len(sources) != 1 or not evidence:
        raise CIError("CI_REPAIR_GATE_NOT_CONFIRMED", 404)
    source, = sources
    token = os.getenv(source.token_env, "")
    if not token or any(c in token for c in "\r\n"):
        raise CIError("CI_REPAIR_GATE_NOT_CONFIRMED", 404)
    try:
        async with httpx.AsyncClient(timeout=4, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", f"{source.api_origin}/api/v1/projects/{source.project_id}/ci-repair/gates/{gate_id}",
                                     headers={"X-API-Key": token}) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 16384:
                        raise ValueError
        gate = json.loads(body)
        if ((gate["project_id"], gate["gate_id"], gate["workflow_run_id"], gate["evidence_digest"])
                != (source.project_id, gate_id, run_id, evidence) or gate["decision"] not in {"PASS", "FAIL", "BLOCKED"}):
            raise ValueError
        org, baseline, candidate = (str(UUID(gate[key])) for key in ("organization_id", "baseline_run_id", "candidate_run_id"))
    except (httpx.HTTPError, AttributeError, KeyError, TypeError, ValueError):
        raise CIError("CI_REPAIR_GATE_NOT_CONFIRMED", 404) from None
    return dict(gate_id=gate_id, decision=gate["decision"], baseline_run_id=baseline, candidate_run_id=candidate,
                url=f"{source.web_origin}/org/{org}/project/{source.project_id}/product/gates?gate_id={gate_id}")
