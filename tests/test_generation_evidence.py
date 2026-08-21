import hashlib
import uuid

import pytest

from core.runtime.generation_evidence import (
    FINAL_ANSWER_MAX_BYTES,
    FinalAnswerEvidenceError,
    FinalAnswerEvidenceV1,
)


def test_final_answer_evidence_uses_exact_utf8_content_and_identity() -> None:
    run_id = uuid.uuid4().hex
    content = " final\n答案 "

    evidence = FinalAnswerEvidenceV1.from_delivered_output(
        run_id=run_id,
        content=content,
    )

    assert evidence.content == content
    assert evidence.run_id == evidence.attempt_id == run_id
    assert evidence.evidence_id == f"final-answer://{run_id}"
    assert evidence.content_sha256 == hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_final_answer_evidence_rejects_utf8_byte_overflow() -> None:
    with pytest.raises(FinalAnswerEvidenceError) as exc_info:
        FinalAnswerEvidenceV1.from_delivered_output(
            run_id=uuid.uuid4().hex,
            content="中" * ((FINAL_ANSWER_MAX_BYTES // 3) + 1),
        )
    assert exc_info.value.args == ("FINAL_ANSWER_CONTENT_LIMIT_EXCEEDED",)
