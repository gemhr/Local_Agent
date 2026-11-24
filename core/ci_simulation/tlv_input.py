"""文件驱动的合成 TLV 输入及版本化基线。"""
import json
import re
from pathlib import Path

from core.runtime.snapshot_serialization import text_digest

SCENARIO_KEY = "stale_case_input_file"
TEMPLATE_VERSION = "1"
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "tlv_input"
INTERFACE_PATH = "tlv-interface.json"
PRIMARY_CASE = "case-tlv-report"
CONTROL_CASE = "case-report-health"
SCRIPT_PATHS = {
    PRIMARY_CASE: "cases/tlv-report.json",
    CONTROL_CASE: "cases/report-health.json",
}
BASELINE_DIGESTS = {
    INTERFACE_PATH: "07c546452af2ab277dedd74df9f9ed91e43161a8459b0ffafa985fec2579acd0",
    SCRIPT_PATHS[PRIMARY_CASE]: "848463116a4e06c2d2386f3c350ecc1bf9f2218fd4d24bb8317499a1ac1a5d6f",
    SCRIPT_PATHS[CONTROL_CASE]: "53a0b4014d334606e24406b4f97c847525fbd3b76f78ff1430961f34637db4d3",
}
EXPECTED_VALUES = {PRIMARY_CASE: 7, CONTROL_CASE: 9}
STALE_ERROR = "TLV_INPUT_REVISION_STALE"
INPUT_FIELD = "request_tlv_hex"
REQUIRED_EVIDENCE_KINDS = frozenset({
    "PARSER_LOG", "ENVIRONMENT_RECORD", "TLV_INTERFACE_DEFINITION", "SCRIPT_BASELINE",
})


def is_file_execution(snapshot):
    return snapshot.get("synthetic_tlv_version") == TEMPLATE_VERSION


def load_baselines():
    """在创建场景时冻结受信文件的原始 UTF-8 字节，历史执行不重读活动文件。"""
    sources = {path: (FIXTURE_ROOT / path).read_bytes().decode("utf-8") for path in BASELINE_DIGESTS}
    if any(text_digest(sources[path]) != digest for path, digest in BASELINE_DIGESTS.items()):
        raise ValueError("CI_SYNTHETIC_TLV_BASELINE_INVALID")
    return sources


def decode_request(interface, script_content, case_id):
    """通过 type、长度和实际 value 字节识别 revision 和无符号大端数值。"""
    pairs = json.loads(script_content, object_pairs_hook=tuple)
    if not isinstance(pairs, tuple) or len(pairs) != 2:
        raise ValueError("CI_SYNTHETIC_TLV_SCRIPT_INVALID")
    script = dict(pairs)
    if script.keys() != {"case_id", INPUT_FIELD} or script["case_id"] != case_id:
        raise ValueError("CI_SYNTHETIC_TLV_SCRIPT_INVALID")
    encoded = script[INPUT_FIELD]
    if not isinstance(encoded, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}){3,4}", encoded):
        raise ValueError("CI_SYNTHETIC_TLV_FRAME_INVALID")
    tag, length, *value = bytes.fromhex(encoded)
    for revision, layout in interface["revisions"].items():
        if (tag, length, len(value)) == (layout["type"], layout["value_length_bytes"], layout["value_length_bytes"]):
            return int(revision), int.from_bytes(bytes(value), "big")
    raise ValueError("CI_SYNTHETIC_TLV_FRAME_INVALID")


def evidence_contents(sources, case_id, execution_id, error):
    """把固定文件原文、版本和解析事实绑定到当前业务执行。"""
    definition = sources[INTERFACE_PATH]
    script_path = SCRIPT_PATHS[case_id]
    script = sources[script_path]
    interface = json.loads(definition)
    revision, _ = decode_request(interface, script, case_id)
    common = dict(case_id=case_id, execution_id=execution_id)
    return dict(
        TLV_INTERFACE_DEFINITION=dict(**common, relative_path=INTERFACE_PATH, definition_content=definition,
            definition_version=interface["definition_version"], software_version=interface["software_version"],
            required_revision=interface["required_revision"], definition_sha256=text_digest(definition)),
        SCRIPT_BASELINE=dict(**common, relative_path=script_path, script_content=script,
            script_sha256=text_digest(script), input_revision=revision, precheck_error=error),
    )
