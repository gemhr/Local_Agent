"""冻结的纯确定性规则；只接收可见证据，没有 Provider GT/模型依赖。"""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from typing import Literal

from pydantic import Field

from core.stage13.contracts import (
    SafeID,
    WireModel,
    business_key,
    canonical_bytes,
    sha256,
)

NORMALIZER_VERSION = "stage13.signature.v1"
CONTROLLED_SUBJECT = {
    "schema_version": "stage13.controlled-analysis-subject.v1",
    "subject_id": "stage13-ci-triage-controlled-placeholder",
    "subject_version": "wp03-ready-only-v1",
    "execution_enabled": False,
    "binding_policy": "WP04_EXPLICIT_REAL_SUBJECT_REQUIRED",
    "input_schema_version": "stage13.triage-input.v1",
    "normalizer_version": NORMALIZER_VERSION,
}
SUBJECT_DIGEST = sha256(canonical_bytes(CONTROLLED_SUBJECT))

UUID_TOKEN = re.compile(
    r"(?<!\w)[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?!\w)", re.I
)
TIMESTAMP_TOKEN = re.compile(
    r"(?<!\w)\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})(?!\w)",
    re.I,
)
IP_TOKEN = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
HEX_TOKEN = re.compile(r"(?<!\w)0x[0-9a-f]+(?!\w)", re.I)
DECIMAL_TOKEN = re.compile(r"(?<![\w.-])\d+(?![\w.]|-\w)")


def normalize_excerpt(excerpt: str) -> str:
    """保留普通版本、标识符中的数字；仅替换合同规定的动态 token。"""

    def ip(match):
        try:
            ipaddress.IPv4Address(match.group())
        except ipaddress.AddressValueError:
            return match.group()
        return "<IPV4>"

    def timestamp(match):
        try:
            datetime.fromisoformat(match.group().upper().replace("Z", "+00:00"))
        except ValueError:
            return match.group()
        return "<TIMESTAMP>"

    value = UUID_TOKEN.sub("<UUID>", excerpt)
    value = TIMESTAMP_TOKEN.sub(timestamp, value)
    value = IP_TOKEN.sub(ip, value)
    value = HEX_TOKEN.sub("<HEX_ADDRESS>", value)
    value = DECIMAL_TOKEN.sub("<DECIMAL>", value)
    return " ".join(value.split()).upper()


class VisibleFailure(WireModel):
    provider_case_id: SafeID
    outcome: Literal["FAILED", "ERROR"]
    title: str = ""
    error_code: str | None = None
    error_excerpt: str | None = Field(default=None, max_length=4096)
    component: SafeID | None = None
    visible_change_refs: tuple[SafeID, ...] = ()
    artifact_refs: tuple[SafeID, ...] = ()

    def grouping(self):
        excerpt = normalize_excerpt(self.error_excerpt or "")
        signature = (
            f"{self.error_code or 'UNKNOWN'}:{excerpt}"
            if excerpt
            else f"NO_SIGNATURE:{self.provider_case_id}"
        )
        return signature, [self.component or "UNKNOWN_COMPONENT"]


def local_key(version_key, failure: VisibleFailure):
    signature, components = failure.grouping()
    return business_key(
        "cluster", version_key, NORMALIZER_VERSION, signature, components
    )


def incident_key(scope, project, suite, day, failure: VisibleFailure):
    signature, components = failure.grouping()
    return business_key(
        "incident",
        scope,
        project,
        suite,
        day,
        NORMALIZER_VERSION,
        signature,
        components,
    )


def choose_representatives(members, limit=8):
    """先取各 channel 首条，余位按同一排序补齐；输入最多各 channel 八条候选。"""
    ordered = sorted(
        members,
        key=lambda m: (m.channel, m.environment, m.ordinal, m.case_id, m.version_id),
    )
    selected, channels = [], set()
    for item in ordered:
        if item.channel not in channels:
            selected.append(item)
            channels.add(item.channel)
        if len(selected) == limit:
            break
    for item in ordered:
        if len(selected) == limit:
            break
        if (item.version_id, item.case_id) not in {
            (m.version_id, m.case_id) for m in selected
        }:
            selected.append(item)
    return sorted(
        selected,
        key=lambda m: (m.channel, m.environment, m.ordinal, m.case_id, m.version_id),
    )
