"""开发演练公开资料；不包含 Coverage Truth 或新版执行结果。"""
from copy import deepcopy
from hashlib import sha256
import json


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def document(ref, kind, title, content, facts):
    body = dict(title=title, content=content, facts=facts)
    return dict(source_ref=ref, version="1", chunk="whole", published_at="2026-10-01T00:00:00Z", kind=kind,
        content_digest=digest(body), **body)


def development_input():
    docs = [
        document("design:F:1", "DESIGN", "公共资源回收策略调整", "公共缓冲区从消费者结束后回收，调整为生产者完成后即可复用。释放本身不清空数据。其他业务可以申请并写入公共资源。", dict(changed_resource="public-buffer", reclamation="producer-complete", subsequent_writes=True)),
        document("meeting:1", "CLARIFICATION", "开发澄清摘要", "调用方可控制延后上报、完成生产、插入诊断任务和触发消费。归档遥测的数据所有权尚未确认。开发提示异步诊断应检查兼容性。", dict(known_impacts=["H2"])),
        document("resources:1", "DEPENDENCY", "公共资源与模块依赖", "统计服务和诊断服务依赖采集模块；采集模块使用 public-buffer。独立计数器使用 private-counter。", dict(dependencies={"statistics": "collector", "diagnostics": "collector", "collector": "public-buffer", "counter": "private-counter", "archive": "collector"})),
    ]
    for feature, title, module, mode, delayed, content in [
        ("H1", "周期统计", "statistics", "SHARED", True, "周期统计保存采集模块数据引用，在周期窗口结束时读取并上报。生产者可早于窗口结束。"),
        ("H2", "异步诊断", "diagnostics", "SHARED", True, "异步诊断保存采集模块引用，工作队列稍后读取；开发提示检查回收变更。"),
        ("H3", "快照导出", "statistics", "COPY", True, "快照在交付时复制完整数据；导出阶段只读取自己持有的副本。"),
        ("H4", "独立计数", "counter", "INDEPENDENT", False, "计数器持有 private-counter，与采集公共资源隔离。"),
        ("H5", "归档遥测", "archive", "UNKNOWN", None, "归档从采集模块获取数据。未说明复制、引用还是读取时点，需开发补充。"),
    ]:
        docs.append(document(f"history:{feature}:1", "HISTORY", title, content,
            dict(feature_id=feature, module=module, ownership=mode, delayed=delayed)))
        docs.append(document(f"case:{feature}:1", "CASE", title + "历史正常用例", "前置：生产正常；提交数据后立即消费并上报；观察上报等于输入。未覆盖延后消费之间的其他公共资源写入。",
            dict(feature_id=feature, operations=["submit", "consume", "complete"], observation="report equals input", coverage="PARTIAL")))
    manifest = [{k: d[k] for k in ("source_ref", "version", "chunk", "content_digest")} for d in docs]
    value = dict(task_version=1, title="公共资源回收变更的历史影响分析", feature_id="F", planning_cutoff="2026-10-02T00:00:00Z",
        known_impacts=["H2"], documents=docs, manifest=manifest, corpus_digest=digest(manifest), development_exposed=True)
    value["input_digest"] = digest(value)
    return value


def read(task, ref):
    doc = next((d for d in task["documents"] if d["source_ref"] == ref), None)
    if doc is None or doc["published_at"] > task["planning_cutoff"]:
        raise ValueError("EVIDENCE_SCOPE_DENIED")
    identity = {k: doc[k] for k in ("source_ref", "version", "chunk", "content_digest")}
    if identity not in task["manifest"] or digest({k: doc[k] for k in ("title", "content", "facts")}) != doc["content_digest"]:
        raise ValueError("EVIDENCE_DIGEST_INVALID")
    return deepcopy(doc)


def search(task, query):
    terms = query.casefold().split()
    result = []
    for doc in task["documents"]:
        if not terms or any(t in json.dumps(doc, ensure_ascii=False).casefold() for t in terms):
            d = read(task, doc["source_ref"])
            result.append({k: d[k] for k in ("source_ref", "version", "chunk", "content_digest", "kind", "title", "facts")} | {"summary": d["content"][:250]})
    return result[:20]
