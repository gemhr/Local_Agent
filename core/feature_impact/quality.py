"""受信评测的纯业务 HTTP 适配；不接受 DB 查询或隐藏参考。"""
import json
from core.feature_impact.knowledge import digest, read
from core.feature_impact.reports import rule_report, validate, RULE_VERSION
from core.feature_impact.service import BusinessError


def evaluate(body):
    """输入为 EO 已冻结的公开投影，服务凭据无权反查人类任务表。"""
    value = body["input"]
    if digest(value) != body["frozen_input_digest"]:
        raise BusinessError("FROZEN_INPUT_DIGEST_INVALID", 422)
    task = value["snapshot"]
    base = {k: v for k, v in task.items() if k != "input_digest"}
    if digest(base) != task["input_digest"] or digest(task["manifest"]) != task["corpus_digest"]:
        raise BusinessError("FROZEN_INPUT_DIGEST_INVALID", 422)
    for doc in task["documents"]:
        read(task, doc["source_ref"])
    if value["candidate_kind"] == "RULE":
        if value["candidate_version"] != RULE_VERSION or value.get("report") is not None:
            raise BusinessError("CANDIDATE_VERSION_INVALID", 422)
        report, sources = rule_report(task)
        envelope = dict(candidate_kind="RULE", candidate_version=RULE_VERSION, run_id=None, task_id=value["task_id"],
            task_version=task["task_version"], input_digest=task["input_digest"], corpus_digest=task["corpus_digest"], report_digest=digest(report))
    elif value["candidate_kind"] == "HUMAN":
        sealed = value["report"]
        if (sealed["candidate_kind"] != "HUMAN" or sealed["task_id"] != value["task_id"] or sealed["input_digest"] != task["input_digest"]
                or sealed["status"] != "SEALED" or sealed["report_digest"] != digest(sealed["report"]) or sealed["run_id"] is not None):
            raise BusinessError("HUMAN_REPORT_BINDING_INVALID", 422)
        report = validate(sealed["report"], task, kind="HUMAN", reads=[])
        sources = sealed["source_record"]
        envelope = {k: sealed[k] for k in ("candidate_kind", "candidate_version", "run_id", "task_id", "task_version", "input_digest", "corpus_digest", "report_digest")}
    else:
        raise BusinessError("REAL_AGENT_NOT_EXECUTED", 422)
    return dict(protocol="feature-impact-output.v1", **{k: body[k] for k in ("project_id", "run_id", "attempt_id", "request_id", "case_id", "frozen_input_digest")},
        envelope=envelope, report=report, source_record=sources)
