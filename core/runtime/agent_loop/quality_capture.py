"""在真实 Provider 准入时提取有界合成标记；不复制输入正文。"""
import re

from .quality_contract import catalog_digest, canonical_bytes
from core.runtime.execution_aggregate import canonical_payload_digest

EXTRACTION = "synthetic-key-value.v1"
SCAN_LIMIT = 2 * 1024 * 1024
SAFE_PARAMETER_ENUMS = frozenset({"LARGE", "SMALL"})


def scan_facts(material, profile, source_ref):
    # 仅固定 public synthetic 格式 key=value；不支持任意正则或语义推断。
    if isinstance(material, str):
        text = material
    else:
        import json
        text = json.dumps(material, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(text.encode("utf-8")) > SCAN_LIMIT:
        return [], False
    facts = []
    for key in profile["fact_keys"]:
        values = re.findall(r"(?<![A-Za-z0-9_])" + re.escape(key) + r"=([A-Za-z0-9_-]{1,64})(?![A-Za-z0-9_-])", text)
        if len(values) > 64:
            return [], False
        facts.extend({"key": key, "value_digest": catalog_digest(v), "source_ref": source_ref,
                      "extraction_version": EXTRACTION} for v in sorted(set(values)))
    return facts, True


def request_capture(messages, *, profile, request_digest, purpose, snapshot_id):
    facts, complete = scan_facts(messages, profile, "actual-request:" + request_digest)
    return {"request_digest": request_digest, "messages_digest": canonical_payload_digest(messages),
        "message_digests": [canonical_payload_digest(m) for m in messages], "purpose": purpose,
        "snapshot_id": snapshot_id, "facts": facts, "scan_complete": complete}


def parameter_projection(arguments, paths):
    projections = []
    for path in paths:
        value = arguments
        for part in path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        supported = (type(value) is bool or type(value) is int and -(2**63) <= value < 2**63
            or type(value) is str and value in SAFE_PARAMETER_ENUMS)
        projections.append({"path": path, "supported": supported, "value": value if supported else None})
    # 精确匹配使用整个参数的 Canonical Digest；非整数参数不伪造可比摘要。
    try:
        digest = catalog_digest(arguments)
    except ValueError:
        digest = None
    return digest, projections

def capture_ci_prompt_request(config, messages, request_digest):
    """原模型准入事务使用的最终 messages 检查，不生成发送成功结论。"""
    agent = config.get('agent', {})
    pin = agent.get('business_options', {}).get('ci_prompt_selection')
    if pin is None:
        return None
    instructions = agent['instructions']
    if agent['agent_id'] != 'ci_incident_investigator' or catalog_digest(instructions) != pin['prompt_digest']:
        raise ValueError('CI_PROMPT_PIN_INVALID')
    system = [m['content'] for m in messages if m.get('role') == 'system' and isinstance(m.get('content'), str)]
    if sum(instructions in text for text in system) != 1:
        raise ValueError('CI_PROMPT_ACTUAL_INSTRUCTIONS_MISMATCH')
    return dict(**pin, model_request_digest=request_digest, messages_digest=catalog_digest(messages),
        system_digest=catalog_digest(system), evidence_scope='DURABLE_REQUEST_PREPARED')
