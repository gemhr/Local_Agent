from concurrent.futures import ThreadPoolExecutor
import pytest
from core.runtime.budget import *

def test_validation_and_usage_semantics():
    assert RunBudget().max_model_calls is None
    for value in (-1, True):
        with pytest.raises(ValueError): RunBudget(max_model_calls=value)
    with pytest.raises(ValueError): RunBudget(max_elapsed_seconds=float('inf'))
    with pytest.raises(ValueError): BudgetUsage(input_tokens=1,total_tokens=0)

def test_reserve_commit_release_and_remaining():
    ledger=BudgetLedger(RunBudget(max_model_calls=2,max_total_tokens=10))
    r=ledger.reserve(BudgetUsage(model_calls=1,input_tokens=2,output_tokens=3,total_tokens=5),reservation_type='model')
    assert ledger.snapshot().remaining.model_calls==1
    ledger.commit(r,BudgetUsage(model_calls=1,input_tokens=1,output_tokens=1,total_tokens=2))
    assert ledger.snapshot().committed_usage.total_tokens==2
    r=ledger.reserve(BudgetUsage(model_calls=1),reservation_type='model'); ledger.release(r)
    with pytest.raises(BudgetReservationError): ledger.release(r)

def test_atomic_model_and_token_reservations():
    for budget, usage in ((RunBudget(max_model_calls=1),BudgetUsage(model_calls=1)),(RunBudget(max_total_tokens=1000),BudgetUsage(input_tokens=350,output_tokens=350,total_tokens=700))):
        ledger=BudgetLedger(budget)
        def reserve():
            try: return ledger.reserve(usage,reservation_type='test')
            except BudgetExceededError: return None
        with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(lambda _:reserve(),range(2)))
        assert sum(x is not None for x in results)==1

def test_time_and_policy():
    ledger=BudgetLedger(RunBudget(max_elapsed_seconds=0.001)); import time; time.sleep(.002)
    with pytest.raises(BudgetExceededError) as exc: ledger.reserve(BudgetUsage(),reservation_type='x')
    assert exc.value.error_code=='TIME_BUDGET_EXHAUSTED'

def usage(i=0,o=0,c=0): return BudgetUsage(input_tokens=i,output_tokens=o,total_tokens=i+o,cost_units=c)

def test_actual_over_reservation_closes_and_blocks_future_reserve():
    ledger=BudgetLedger(RunBudget(max_input_tokens=3200,max_output_tokens=1100,max_total_tokens=4200,max_cost_units=7))
    r=ledger.reserve(usage(3000,1000,5),reservation_type='model')
    snap=ledger.commit(r,usage(3200,1100,7))
    assert snap.committed_usage.total_tokens == 4300
    assert snap.remaining.total_tokens == 0 and 'total_tokens' in snap.exhausted_dimensions
    with pytest.raises(BudgetExceededError): ledger.reserve(BudgetUsage(model_calls=1),reservation_type='next')
    with pytest.raises(BudgetReservationError): ledger.commit(r)
    with pytest.raises(BudgetReservationError): ledger.release(r)

def test_actual_equal_and_smaller_return_unused_reservation():
    ledger=BudgetLedger(RunBudget(max_total_tokens=10))
    a=ledger.reserve(usage(3,3),reservation_type='x'); ledger.commit(a,usage(2,2)); assert ledger.snapshot().remaining.total_tokens==6
    b=ledger.reserve(usage(3,3),reservation_type='x'); ledger.commit(b,usage(3,3)); assert ledger.snapshot().committed_usage.total_tokens==10

class Lazy:
    def __init__(self, values=(), fail=False): self.values=iter(values); self.fail=fail; self.closed=False
    def __iter__(self): return self
    def __next__(self):
        if self.fail: raise RuntimeError('first')
        return next(self.values)
    def close(self): self.closed=True

def test_lazy_stream_release_before_start_and_commit_after_start():
    ledger=BudgetLedger(RunBudget(max_model_calls=1)); r=ledger.reserve(BudgetUsage(model_calls=1),reservation_type='model')
    stream=BudgetedModelStream(Lazy(['x']),ledger,r); stream.close(); assert ledger.snapshot().committed_usage.model_calls==0
    r=ledger.reserve(BudgetUsage(model_calls=1),reservation_type='model'); stream=BudgetedModelStream(Lazy(fail=True),ledger,r)
    with pytest.raises(RuntimeError): next(stream)
    assert ledger.snapshot().committed_usage.model_calls==1

def test_lazy_stream_partial_close_and_actual_usage():
    ledger=BudgetLedger(RunBudget(max_model_calls=2,max_total_tokens=20))
    r=ledger.reserve(BudgetUsage(model_calls=1,input_tokens=3,output_tokens=3,total_tokens=6),reservation_type='model')
    stream=BudgetedModelStream(Lazy(['x','y']),ledger,r); assert next(stream)=='x'; stream.close(); assert ledger.snapshot().committed_usage.total_tokens==6
    r=ledger.reserve(BudgetUsage(model_calls=1,input_tokens=3,output_tokens=3,total_tokens=6),reservation_type='model')
    stream=BudgetedModelStream(Lazy([]),ledger,r,lambda _: usage(5,5));
    with pytest.raises(StopIteration): next(stream)
    assert ledger.snapshot().committed_usage.total_tokens==16
