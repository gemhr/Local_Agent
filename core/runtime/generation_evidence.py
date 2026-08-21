#!/usr/bin/env python
"""Evaluation protocol v2 的 delivered final answer evidence。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


FINAL_ANSWER_EVIDENCE_SCHEMA_VERSION = "final-answer-evidence.v1"
FINAL_ANSWER_MEDIA_TYPE = "text/plain; charset=utf-8"
FINAL_ANSWER_MAX_BYTES = 64 * 1024


class FinalAnswerEvidenceError(ValueError):
    """Final answer 无法在冻结的 evidence 边界内表示。"""


@dataclass(frozen=True, slots=True)
class FinalAnswerEvidenceV1:
    """由实际 delivered output 直接构造的不可变 final answer evidence。"""

    schema_version: str
    evidence_id: str
    run_id: str
    attempt_id: str
    media_type: str
    content_sha256: str
    content: str

    @classmethod
    def from_delivered_output(
        cls,
        *,
        run_id: str,
        content: str,
    ) -> "FinalAnswerEvidenceV1":
        if not isinstance(run_id, str) or not run_id:
            raise FinalAnswerEvidenceError("FINAL_ANSWER_INVALID_RUN_ID")
        if not isinstance(content, str):
            raise FinalAnswerEvidenceError("FINAL_ANSWER_DELIVERED_OUTPUT_MISSING")
        encoded = content.encode("utf-8")
        if len(encoded) > FINAL_ANSWER_MAX_BYTES:
            raise FinalAnswerEvidenceError("FINAL_ANSWER_CONTENT_LIMIT_EXCEEDED")
        return cls(
            schema_version=FINAL_ANSWER_EVIDENCE_SCHEMA_VERSION,
            evidence_id=f"final-answer://{run_id}",
            run_id=run_id,
            attempt_id=run_id,
            media_type=FINAL_ANSWER_MEDIA_TYPE,
            content_sha256=hashlib.sha256(encoded).hexdigest(),
            content=content,
        )

    def to_wire_dict(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "evidence_id": self.evidence_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "media_type": self.media_type,
            "content_sha256": self.content_sha256,
            "content": self.content,
        }
