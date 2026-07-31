from __future__ import annotations

import pytest

from core.runtime import FaultPoint, OperationIdempotency
from tests.test_tool_post_commit_fault_injection import _execute
from tests.tool_fault_test_support import PhaseAwareToolAdapter, make_controller


@pytest.mark.asyncio
async def test_after_side_effect_commit_contract_is_not_approximated_by_return():
    adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT
    )
    exact = make_controller(FaultPoint.TOOL_AFTER_SIDE_EFFECT_COMMIT)
    alternative = make_controller(
        FaultPoint.TOOL_AFTER_AUTHORITATIVE_SIDE_EFFECT_RESOLUTION
    )

    exact_result, _, _ = await _execute(adapter, exact)
    alternative_adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT
    )
    alternative_result, _, _ = await _execute(alternative_adapter, alternative)

    assert exact_result.output.content == "ok"
    assert alternative_result.safe_error_code == "TOOL_POST_PROVIDER_FAILURE"
    assert exact.snapshot().counters[0].match_count == 0
    assert alternative.snapshot().counters[0].match_count == 1
