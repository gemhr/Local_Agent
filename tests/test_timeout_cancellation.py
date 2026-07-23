import pytest

from core.runtime import CancellationReason, CancellationSource, OperationType, RunCancelledError, create_run_context, effective_timeout_seconds


def test_token_reason_timestamp_and_raise():
    source = CancellationSource()
    assert source.cancel(CancellationReason.DEADLINE_EXCEEDED) is True
    assert source.cancel(CancellationReason.USER_CANCELLED) is False
    assert source.token.reason is CancellationReason.DEADLINE_EXCEEDED
    with pytest.raises(RunCancelledError): source.token.raise_if_cancelled()


def test_effective_timeout_uses_earliest_parent_deadline():
    context, _ = create_run_context(entry_agent_id="agent", timeout_seconds=10)
    assert effective_timeout_seconds(context, 2) <= 2
    assert effective_timeout_seconds(context, None) is not None
