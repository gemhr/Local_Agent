"""消费 EvalOps 权限投影并持久交付；不判断模型质量、不重新计算 Gate。"""

from datetime import timedelta
from uuid import uuid4
import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from core.runtime.output_gate import DeliveryStatus
from core.runtime.tool_contract import ToolSideEffectState
from core.stage13.delivery_models import DeliveryRow, ControlledSinkRow
from core.stage13.triage_models import TriageRunRow, EvidenceReadRow
from core.stage13.triage_subject import digest, validate_output, strict_json
from core.stage13.contracts import sha256

VERSION = "stage13.delivery-authorization.v1"
PROTOCOL = "stage13.delivery.v1"


def delivery_identity(binding):
    return digest({"Domain": PROTOCOL, "Request": binding})


def authorization_digest(a):
    return digest(dict(a, authorization_digest=""))


class EvalOpsAuthorizationClient:
    """URL/project/API key 来自应用配置；caller 不能选择 authority。"""

    def __init__(self, client, url, project, key):
        self.client, self.url, self.project, self.key = client, url, project, key

    async def verify(self, binding):
        response = await self.client.post(
            self.url
            + f"/api/v1/projects/{self.project}/stage13/delivery-authorizations",
            json=binding,
            headers={"X-API-Key": self.key},
            timeout=5,
        )
        response.raise_for_status()
        return response.json()


class DeliveryService:
    """业务 delivery_id、短 lease/fencing 和 sink 唯一键限定于本受控本地交付。"""

    def __init__(
        self, database, authority, scope, project, destination_id, *, fault=None
    ):
        self.database, self.authority = database, authority
        self.scope, self.project, self.destination_id = scope, project, destination_id
        # 仅 TEST_SCOPE 调用方注入 seam；HTTP 没有 fault 参数。
        self.fault = fault

    async def _fault(self, point):
        if self.fault:
            await self.fault(point)

    async def _outcome(self, binding, authorization):
        o = binding["outcome"]
        async with self.database.session() as session:
            anchor = await session.get(TriageRunRow, o["anchor_run_id"])
            final = await session.get(TriageRunRow, o["selected_final_run_id"])
            if (
                not anchor
                or not final
                or anchor.scope != self.scope
                or final.scope != self.scope
            ):
                raise ValueError("SUBJECT_BINDING_MISMATCH")
            if (
                anchor.anchor_run_id != anchor.run_id
                or final.anchor_run_id != anchor.run_id
                or anchor.subject_digest != o["subject_manifest_digest"]
                or final.subject_digest != anchor.subject_digest
                or anchor.runtime_status != "SUCCEEDED"
                or final.runtime_status != "SUCCEEDED"
                or final.evaluation_attempt_id != anchor.run_id
                or not final.receipt
                or final.receipt["receipt_digest"] != o["actual_subject_receipt_digest"]
                or not final.raw_answer
                or sha256(final.raw_answer.encode()) != o["final_answer_digest"]
            ):
                raise ValueError("OUTCOME_BINDING_MISMATCH")
            r = dict(final.receipt)
            rd = r.pop("receipt_digest")
            if digest(r) != rd or r["final_answer_digest"] != o["final_answer_digest"]:
                raise ValueError("RECEIPT_BINDING_MISMATCH")
            payload = strict_json(anchor.query)
            reads = (
                (
                    await session.execute(
                        select(EvidenceReadRow).where(
                            EvidenceReadRow.run_id.in_([anchor.run_id, final.run_id]),
                            EvidenceReadRow.successful.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
            validation = validate_output(
                final.raw_answer, payload, [v.evidence for v in reads]
            )
            if validation["status"] != "VALID":
                raise ValueError("OUTPUT_INVALID")
            outcome = {
                "protocol_version": "stage13.triage-outcome.v1",
                "project_id": self.project,
                "source": "LOCALAGENT_TRIAGE_OUTCOME_V1",
                "source_run_id": anchor.run_id,
                "outcome_revision": 1,
                "incident_ref": payload.get("incident_ref"),
                "case_id": o["case_id"],
                "case_version": o["case_version"],
                "subject_manifest_digest": final.subject_digest,
                "anchor_run_id": anchor.run_id,
                "selected_final_run_id": final.run_id,
                "structured_triage_output": validation["output"],
                "final_answer_digest": o["final_answer_digest"],
                "actual_subject_receipt_digest": rd,
                "input_digest": final.input_digest,
                "evaluation_result_refs": authorization["evaluation_result_refs"],
                "comparison_ref": binding["gate"]["comparison_digest"],
                "gate": binding["gate"],
                "delivery_authorization_status": "AUTHORIZED",
                "authorization_digest": authorization["authorization_digest"],
                "retention_policy_ref": "stage13.controlled-explicit-retention.v1",
            }
            forbidden = {
                "hidden_root_id",
                "ground_truth",
                "GT",
                "ExpectedFailureCategory",
                "ExpectedTicketDecision",
                "AcceptableActions",
                "Criticality",
                "critical_evaluator_label",
            }

            def check(value):
                if isinstance(value, dict):
                    if forbidden.intersection(value):
                        raise ValueError("HIDDEN_GT_FORBIDDEN")
                    for v in value.values():
                        check(v)
                elif isinstance(value, list):
                    for v in value:
                        check(v)
                elif isinstance(value, str) and "hidden-root-" in value:
                    raise ValueError("HIDDEN_GT_FORBIDDEN")

            check(outcome)
            return outcome

    def _receipt(
        self,
        delivery_id,
        authorization,
        status,
        reason,
        started,
        terminal,
        sink=None,
        reconciled="NOT_REQUIRED",
    ):
        committed = status == DeliveryStatus.DELIVERED.value
        result = {
            "protocol_version": PROTOCOL,
            "delivery_id": delivery_id,
            "authorization_digest": authorization.get("authorization_digest"),
            "outcome": authorization.get("outcome"),
            "destination": authorization.get("destination"),
            "delivery_status": status,
            "reason": reason,
            "side_effect_state": (
                ToolSideEffectState.COMMITTED.value
                if committed
                else (
                    ToolSideEffectState.UNKNOWN.value
                    if status == DeliveryStatus.OUTCOME_UNKNOWN.value
                    else ToolSideEffectState.NOT_STARTED.value
                )
            ),
            "side_effect_identity": delivery_id,
            "started_at": started.isoformat(),
            "terminal_at": terminal.isoformat(),
            "sink_receipt": sink,
            "sink_result_digest": digest(sink) if sink else None,
            "reconciliation_status": reconciled,
        }
        result["delivery_receipt_digest"] = digest(result)
        return result

    async def deliver(self, request):
        authorization = request.get("gate_authorization")
        binding = request["binding"]
        delivery_id = delivery_identity(binding)
        if (
            request["protocol_version"] != PROTOCOL
            or request["delivery_id"] != delivery_id
        ):
            raise ValueError("DELIVERY_IDENTITY_MISMATCH")
        status, reason = "DENIED", "MISSING_GATE"
        verified = {}
        audit_authorization = {}
        if authorization:
            try:
                verified = await self.authority.verify(binding)
                if (
                    verified != authorization
                    or verified.get("authorization_version") != VERSION
                    or verified.get("project_id") != self.project
                    or verified.get("authorization_digest")
                    != authorization_digest(verified)
                    or verified.get("gate") != binding["gate"]
                    or verified.get("outcome") != binding["outcome"]
                    or verified.get("destination") != binding["destination"]
                ):
                    reason = "AUTHORIZATION_BINDING_MISMATCH"
                else:
                    audit_authorization = verified
                    status, reason = (
                        verified["authorization_status"],
                        verified["reason"],
                    )
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                reason = "GATE_UNAVAILABLE_OR_MALFORMED"
        if binding["destination"] != {
            "kind": "CONTROLLED_DELIVERY_SINK",
            "scope": "TEST_SCOPE",
            "id": self.destination_id,
        }:
            status, reason = "DENIED", "DESTINATION_DENIED"
        outcome = None
        if status == "AUTHORIZED":
            try:
                outcome = await self._outcome(binding, verified)
            except (ValueError, KeyError, TypeError):
                status, reason = "DENIED", "OUTCOME_BINDING_MISMATCH"
        # Denied audit 不包含 raw answer / RichOutcome，业务 payload 不进入 sink。
        intent_digest = digest(request)
        token = str(uuid4())
        async with self.database.transaction() as session:
            now = await session.scalar(select(func.clock_timestamp()))
            await session.execute(
                insert(DeliveryRow)
                .values(
                    delivery_id=delivery_id,
                    request_digest=intent_digest,
                    authorization=audit_authorization,
                    status=status,
                    epoch=0,
                    started_at=now,
                )
                .on_conflict_do_nothing()
            )
            row = await session.get(DeliveryRow, delivery_id, with_for_update=True)
            if row.request_digest != intent_digest:
                raise ValueError("DELIVERY_BINDING_CONFLICT")
            # 即使 replay 已交付，也必须先成功核验当前 authority 与全部绑定。
            if status != "AUTHORIZED":
                receipt = self._receipt(
                    delivery_id,
                    audit_authorization,
                    "DENIED",
                    reason,
                    row.started_at,
                    now,
                )
                if row.receipt is None:
                    row.receipt, row.status = receipt, "DENIED"
                return receipt
            if row.receipt is not None:
                return row.receipt
            if row.lease_until is not None and row.lease_until > now:
                return self._receipt(
                    delivery_id,
                    verified,
                    "OUTCOME_UNKNOWN",
                    "PUBLICATION_OWNER_ACTIVE",
                    row.started_at,
                    now,
                    reconciled="PENDING",
                )
            row.token, row.epoch, row.lease_until = (
                token,
                row.epoch + 1,
                now + timedelta(seconds=30),
            )
            row.status = "DELIVERING"
            epoch = row.epoch
        await self._fault("before_sink")
        try:
            # takeover 先查 sink；旧 owner 的写入在同一行锁/DB 时钟 fence 下被拒绝。
            async with self.database.transaction() as session:
                row = await self._fence(session, delivery_id, token, epoch)
                sink = await session.get(ControlledSinkRow, delivery_id)
                reconciled = sink is not None
                if sink is None:
                    sr = {
                        "delivery_id": delivery_id,
                        "payload_digest": digest(outcome),
                        "destination": binding["destination"],
                    }
                    session.add(
                        ControlledSinkRow(
                            delivery_id=delivery_id,
                            payload=outcome,
                            payload_digest=sr["payload_digest"],
                            receipt=sr,
                        )
                    )
                else:
                    if sink.payload_digest != digest(outcome):
                        raise ValueError("SINK_BINDING_CONFLICT")
                    sr = sink.receipt
            await self._fault("after_sink_commit")
            async with self.database.transaction() as session:
                row = await self._fence(session, delivery_id, token, epoch)
                now = await session.scalar(select(func.clock_timestamp()))
                receipt = self._receipt(
                    delivery_id,
                    verified,
                    DeliveryStatus.DELIVERED.value,
                    "SINK_COMMITTED",
                    row.started_at,
                    now,
                    sr,
                    "FOUND_EXISTING" if reconciled else "NOT_REQUIRED",
                )
                row.receipt, row.status, row.lease_until = receipt, "DELIVERED", None
        except (OSError, TimeoutError):
            # 不宣称 DELIVERED、不盲重发；下一请求通过 fence 后先查 sink。
            async with self.database.transaction() as session:
                row = await self._fence(session, delivery_id, token, epoch)
                now = await session.scalar(select(func.clock_timestamp()))
                row.status, row.lease_until = "OUTCOME_UNKNOWN", None
                return self._receipt(
                    delivery_id,
                    verified,
                    DeliveryStatus.OUTCOME_UNKNOWN.value,
                    "SINK_OR_RESPONSE_UNCONFIRMED",
                    row.started_at,
                    now,
                    reconciled="REQUIRED",
                )
        await self._fault("after_local_receipt")
        return receipt

    async def _fence(self, session, delivery_id, token, epoch):
        row = await session.get(DeliveryRow, delivery_id, with_for_update=True)
        now = await session.scalar(select(func.clock_timestamp()))
        if (
            row is None
            or row.token != token
            or row.epoch != epoch
            or row.lease_until is None
            or row.lease_until <= now
        ):
            raise ValueError("DELIVERY_OWNERSHIP_LOST")
        return row
