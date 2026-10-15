#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""导出当前 Trace producer 的 descriptor 与六类合成 Golden vectors。"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import subprocess

from core.runtime.trace_contract import (
    RUNTIME_FINAL_MEMORY_COMMIT_SPAN,
    RUNTIME_OUTPUT_DELIVERY_SPAN,
    RUNTIME_PLANNING_SPAN,
    RUNTIME_RUN_SPAN,
    RUNTIME_STEP_SPAN,
    RUNTIME_SYNTHESIS_SPAN,
)
from core.runtime.trace_contract_fingerprint import TraceContractFingerprinter
from core.runtime.trace_export_contract import (
    export_contract_semantic_descriptor,
    project_span,
)
from core.runtime.trace_export_serialization import serialize_trace_export_envelope
from core.runtime.tracing import SpanRecord, SpanStatus


_FIXTURES: tuple[tuple[str, str, str | None, dict[str, object]], ...] = (
    (
        "runtime.run",
        RUNTIME_RUN_SPAN,
        None,
        {
            "runtime_mode": "COORDINATED",
            "step_count": 1,
            "selected_entry_agent_id": "core_router",
            "agent_version": "v1",
            "resolved_agent_id": "core_router",
            "workflow_id": "default",
            "workflow_version": "v1",
            "toolset_identity": "a" * 64,
            "resolved_model_profile_id": "default",
            "resolved_retrieval_profile_id": "default",
            "resolved_memory_profile_id": "default",
        },
    ),
    (
        "runtime.planning",
        RUNTIME_PLANNING_SPAN,
        None,
        {"planning_source": "deterministic", "schema_version": 1},
    ),
    (
        "runtime.step",
        RUNTIME_STEP_SPAN,
        "step-1",
        {
            "execution_kind": "AGENT",
            "dependency_count": 0,
            "preferred_agent": "core_router",
            "agent_version": "v1",
            "workflow_id": "default",
            "workflow_version": "v1",
        },
    ),
    (
        "runtime.synthesis",
        RUNTIME_SYNTHESIS_SPAN,
        "step-2",
        {"execution_kind": "SYNTHESIS"},
    ),
    (
        "runtime.output_delivery",
        RUNTIME_OUTPUT_DELIVERY_SPAN,
        "step-2",
        {"delivery_status": "DELIVERED", "publish_attempt_count": 1},
    ),
    (
        "runtime.final_memory_commit",
        RUNTIME_FINAL_MEMORY_COMMIT_SPAN,
        "step-2",
        {"persist_enabled": True, "memory_scope": "direct"},
    ),
)


def _canonical_payload(raw: bytes) -> bytes:
    """按 Trace Export canonical 规则规范化实例字段与精确 duration。"""
    payload = json.loads(raw, parse_float=float, parse_int=int)
    duration = payload["duration_ms"]
    if isinstance(duration, float):
        # 精确 binary64 fixed point，避免使用短浮点表示。
        ratio = Fraction.from_float(duration)
        scale = ratio.denominator.bit_length() - 1
        numerator = ratio.numerator * (5**scale)
        duration = str(numerator)
        if scale:
            duration = duration.rjust(scale + 1, "0")
            duration = duration[:-scale] + "." + duration[-scale:]
            duration = duration.rstrip("0").rstrip(".")
    elif duration == 0:
        duration = "0"
    payload["duration_ms"] = duration
    # Canonical payload encodes duration as a JSON string.
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def export_vectors(output: Path) -> None:
    descriptor = export_contract_semantic_descriptor()
    fingerprint = TraceContractFingerprinter.fingerprint()
    if descriptor.get("contract_version") not in (1, 2):
        raise ValueError("unsupported contract version")
    output.mkdir(parents=True, exist_ok=True)
    producer_revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (output / "descriptor.json").write_text(
        json.dumps(descriptor, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    vectors = []
    started = datetime(2026, 10, 15, tzinfo=UTC)
    for index, (name, operation, step_id, attributes) in enumerate(_FIXTURES, start=1):
        span = SpanRecord(
            trace_id="trace-vector-1",
            span_id=f"span-vector-{index}",
            parent_span_id="span-vector-1" if index > 1 else None,
            run_id="run-vector-1",
            step_id=step_id,
            component="runtime",
            operation=operation,
            started_at=started,
            completed_at=started + timedelta(milliseconds=index),
            duration_ms=float(index),
            status=SpanStatus.OK,
            error_code=None,
            attributes=attributes,
        )
        envelope = project_span(span)
        raw = serialize_trace_export_envelope(envelope)
        if envelope.contract_fingerprint != fingerprint:
            raise ValueError("producer fingerprint changed during vector export")
        canonical = _canonical_payload(raw)
        vectors.append(
            {
                "name": name,
                "raw_utf8": raw.decode("utf-8"),
                "wire_sha256": hashlib.sha256(raw).hexdigest(),
                "expected_canonical_utf8": canonical.decode("ascii"),
                "canonical_payload_sha256": hashlib.sha256(canonical).hexdigest(),
                "descriptor_fingerprint": fingerprint,
            }
        )
    manifest = {
        "contract_identity": descriptor["contract_identity"],
        "contract_version": descriptor["contract_version"],
        "descriptor_fingerprint": fingerprint,
        "producer_git_sha": producer_revision,
        "vectors": vectors,
    }
    (output / "golden.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    export_vectors(args.output)


if __name__ == "__main__":
    main()
