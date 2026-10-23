"""统一报告校验和基于公开依赖的有限规则。"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from core.feature_impact.knowledge import digest, read, search
from core.feature_impact.resource_simulation import controls

RULE_VERSION = "dependency-lifecycle.v1"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Reference(Strict):
    source_ref: str
    version: str
    chunk: str
    content_digest: str
    basis: Literal["TASK_INPUT", "EVIDENCE_READ"] = "TASK_INPUT"


class TestSuggestion(Strict):
    test_id: str
    preconditions: str = Field(min_length=1)
    operations: list[dict] = Field(min_length=1, max_length=40)
    observation: str = Field(min_length=1)
    expected: str = Field(min_length=1)
    policy: Literal["OLD", "NEW"]
    historical_cases: list[str]
    coverage_gap: str


class Finding(Strict):
    finding_id: str
    historical_feature_id: str
    impact_mechanism: str
    trigger_conditions: list[str] = Field(min_length=1)
    coverage_class: Literal["MUST_TEST", "REASONABLE_CHECK", "INSUFFICIENT_EVIDENCE", "UNSUPPORTED"]
    rationale: str = Field(min_length=1)
    supporting_evidence: list[Reference] = Field(min_length=1)
    counter_evidence: list[Reference]
    uncertainties: list[str]
    existing_coverage: Literal["FULL", "PARTIAL", "NONE", "UNKNOWN"]
    recommended_tests: list[TestSuggestion]
    clarification_questions: list[str]


class Report(Strict):
    schema_version: Literal["feature-impact-report.v1"]
    findings: list[Finding] = Field(min_length=1, max_length=30)


def validate(raw, task, *, kind, reads, task_inputs=()):
    report = Report.model_validate(raw)
    ids, tests = set(), set()
    for finding in report.findings:
        if finding.finding_id in ids:
            raise ValueError("DUPLICATE_FINDING_ID")
        ids.add(finding.finding_id)
        if finding.coverage_class in {"MUST_TEST", "REASONABLE_CHECK"} and not finding.recommended_tests and finding.existing_coverage != "FULL":
            raise ValueError("TEST_SUGGESTION_REQUIRED")
        if finding.coverage_class == "INSUFFICIENT_EVIDENCE" and (not finding.uncertainties or not finding.clarification_questions):
            raise ValueError("CLARIFICATION_REQUIRED")
        for ref in finding.supporting_evidence + finding.counter_evidence:
            doc = read(task, ref.source_ref)
            if any(getattr(ref, k) != doc[k] for k in ("version", "chunk", "content_digest")):
                raise ValueError("REPORT_EVIDENCE_INVALID")
            if ((kind == "RULE" or ref.basis == "EVIDENCE_READ") and ref.source_ref not in reads
                    or kind == "AGENT" and ref.basis == "TASK_INPUT" and ref.source_ref not in task_inputs):
                raise ValueError("REPORT_READ_PROOF_INVALID")
        for test in finding.recommended_tests:
            if test.test_id in tests:
                raise ValueError("DUPLICATE_TEST_ID")
            tests.add(test.test_id)
            from core.feature_impact.resource_simulation import validate_operations
            validate_operations(test.operations)
    return report.model_dump()


def _existing_coverage(cases, mechanism):
    """核对公开用例已有的 operations/observation，coverage 标签不作证明。"""
    if mechanism == "OWNERSHIP_UNKNOWN":
        return "UNKNOWN", "数据所有权与消费时点不足，无法判断历史覆盖"
    if not cases:
        return "NONE", "没有相关历史用例"
    assessments = []
    aliases = {"PRODUCER_COMPLETE": "COMPLETE", "SUBMIT_OTHER": "OTHER_WRITE", "WRITE_OTHER": "OTHER_WRITE",
        "SUBSEQUENT_WRITE": "OTHER_WRITE", "REUSE_WRITE": "OTHER_WRITE", "DELAYED_CONSUME": "CONSUME",
        "CONSUME_DELAYED": "CONSUME", "DELIVER_SHARED": "DELIVER", "DELIVER_COPY": "DELIVER"}
    for case in cases:
        facts = case["facts"]
        operations = facts.get("operations")
        if not isinstance(operations, list) or not operations:
            assessments.append(("UNKNOWN", "历史用例缺少可判断的操作资料"))
            continue
        events, unknown, submitted = [], False, False
        target = next((o.get("request_id") for o in operations if isinstance(o, dict) and o.get("action") == "SUBMIT"), None)
        gaps = []
        for op in operations:
            if isinstance(op, str):
                event = aliases.get(op.upper(), op.upper())
                if ((op.upper() == "DELIVER_COPY" and mechanism == "SHARED_DELAYED_REUSE")
                        or (op.upper() == "DELIVER_SHARED" and mechanism == "COPY_ISOLATION")):
                    gaps.append("交付的数据所有权与待验证机制不一致")
                if event == "SUBMIT" and submitted:
                    event = "OTHER_WRITE"
            elif isinstance(op, dict) and isinstance(op.get("action"), str):
                event = op["action"].upper()
                if event == "SUBMIT" and op.get("request_id") != target:
                    event = "OTHER_WRITE"
                    original = next(o for o in operations if isinstance(o, dict) and o.get("action") == "SUBMIT")
                    if op.get("payload") == original.get("payload"):
                        gaps.append("缺少与原输入可区分的后续写入内容")
                elif op.get("request_id") != target:
                    continue
                if event == "DELIVER" and mechanism in {"SHARED_DELAYED_REUSE", "COPY_ISOLATION"}:
                    required_mode = "SHARED" if mechanism == "SHARED_DELAYED_REUSE" else "COPY"
                    if op.get("mode") != required_mode:
                        gaps.append("交付的数据所有权与待验证机制不一致")
            else:
                unknown = True
                continue
            if event not in {"SUBMIT", "DELIVER", "COMPLETE", "OTHER_WRITE", "CONSUME", "REPORT"}:
                unknown = True
            submitted |= event == "SUBMIT"
            events.append(event)
        required = ["SUBMIT", "COMPLETE", "OTHER_WRITE", "CONSUME"]
        if target is not None:
            required.insert(1, "DELIVER")
        labels = dict(SUBMIT="缺少原任务提交", DELIVER="缺少数据交付", COMPLETE="缺少生产完成",
            OTHER_WRITE="缺少后续公共资源写入", CONSUME="缺少延迟消费")
        missing = [labels[event] for event in required if event not in events]
        gaps.extend(missing)
        cursor = 0
        for event in required:
            try:
                cursor = events.index(event, cursor) + 1
            except ValueError:
                if not missing:
                    gaps.append("缺少生产完成→其他写入→消费的关键时序")
                break
        observation = facts.get("observation", "")
        expected = facts.get("expected", observation)
        if not isinstance(observation, str) or not any(word in observation.casefold() for word in ("report", "上报")):
            gaps.append("缺少业务上报观察点")
        if not isinstance(expected, str) or not any(word in expected.casefold() for word in ("report equals input", "report == input", "上报等于输入", "上报等于原始输入")):
            gaps.append("缺少上报等于原始输入的预期判据")
        assessments.append(("UNKNOWN", "历史操作含无法确定的业务步骤") if unknown else
            ("PARTIAL", "；".join(dict.fromkeys(gaps))) if gaps else ("FULL", "已有用例完整覆盖，可复用，无需新增重复测试"))
    return next((a for a in assessments if a[0] == "FULL"),
        next((a for a in assessments if a[0] == "PARTIAL"), assessments[0]))


def rule_report(task):
    """只读公开依赖，未知信息不补成确定事实。"""
    catalog = search(task, "")
    docs = [read(task, d["source_ref"]) for d in catalog]
    reads = [d["source_ref"] for d in docs]
    design = next(d for d in docs if d["kind"] == "DESIGN")
    dependency = next(d for d in docs if d["kind"] == "DEPENDENCY")
    edges = dependency["facts"]["dependencies"]
    changed = design["facts"]["changed_resource"]

    def resource(module):
        visited = set()
        while module in edges and module not in visited:
            visited.add(module)
            module = edges[module]
        return module

    def ref(doc):
        return {k: doc[k] for k in ("source_ref", "version", "chunk", "content_digest")} | {"basis": "EVIDENCE_READ"}

    findings = []
    for doc in (d for d in docs if d["kind"] == "HISTORY"):
        facts = doc["facts"]
        feature = facts["feature_id"]
        cases = [d for d in docs if d["kind"] == "CASE" and d["facts"]["feature_id"] == feature]
        mode, shared = facts["ownership"], resource(facts["module"]) == changed
        if not shared:
            classification, mechanism, triggers = "UNSUPPORTED", "RESOURCE_ISOLATION", ["INDEPENDENT_RESOURCE"]
            reason, scenario = "公开资源链落在独立资源，缺乏公共资源覆盖影响的依据。", None
        elif mode == "COPY":
            classification, mechanism, triggers = "REASONABLE_CHECK", "COPY_ISOLATION", ["COPY_AT_DELIVERY", "LATER_PUBLIC_WRITE"]
            reason, scenario = "交付时已复制；建议确认后续公共资源写入不改变副本。", "C4"
        elif mode == "SHARED" and facts["delayed"] and design["facts"]["subsequent_writes"]:
            classification, mechanism, triggers = "MUST_TEST", "SHARED_DELAYED_REUSE", ["DELAYED_CONSUME", "PRODUCER_COMPLETE", "SUBSEQUENT_WRITE"]
            reason, scenario = "引用延迟读取与生产者完成后复用存在时间交集。", "C3"
        else:
            classification, mechanism, triggers = "INSUFFICIENT_EVIDENCE", "OWNERSHIP_UNKNOWN", ["OWNERSHIP_OR_TIMING_MISSING"]
            reason, scenario = "公开关联存在，但数据所有权与消费时点缺失。", None
        suggestions = []
        coverage, gap = _existing_coverage(cases, mechanism)
        if scenario and coverage != "FULL":
            operation = controls()[scenario]
            suggestions.append(dict(test_id="test-" + feature, preconditions="提交原始统计数据，允许控制消费时点与插入其他公共资源业务。",
                **operation, observation="读取原任务业务上报", expected="原任务上报必须等于原输入；差异应作为兼容性故障。",
                historical_cases=[case["source_ref"] for case in cases], coverage_gap=gap))
        unknown = classification == "INSUFFICIENT_EVIDENCE"
        findings.append(dict(finding_id="finding-" + feature, historical_feature_id=feature, impact_mechanism=mechanism,
            trigger_conditions=triggers, coverage_class=classification, rationale=reason + " " + gap,
            supporting_evidence=[ref(design), ref(dependency), ref(doc)] + [ref(case) for case in cases],
            counter_evidence=[ref(doc)] if mode in {"COPY", "INDEPENDENT"} else [],
            uncertainties=["数据所有权与消费时点未确认"] if unknown else [], existing_coverage=coverage,
            recommended_tests=suggestions, clarification_questions=["交付后持有副本还是共享引用？何时消费？"] if unknown else []))
    report = dict(schema_version="feature-impact-report.v1", findings=findings)
    return validate(report, task, kind="RULE", reads=reads), dict(query="", read_sources=reads, rule_version=RULE_VERSION, input_digest=task["input_digest"])
