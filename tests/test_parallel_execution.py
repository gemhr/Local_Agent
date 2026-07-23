#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""并行执行生命周期、容量和稳定聚合测试。"""
import asyncio
from datetime import UTC, datetime
import unittest

from core.runtime import (AgentState, AgentStateMachine, ParallelExecutor, ParallelFailureMode,
    RunEventType, RunStateEvent, StepConcurrencySpec, StepExecutionMode, StepStatus,
    create_run_context)
from core.runtime.scheduler import StepClaim
from core.runtime.planning import TaskCapabilityRequirements


def claims(state, *ids):
    machine = AgentStateMachine()
    machine.apply_run_event(state, RunStateEvent(RunEventType.STARTED))
    result=[]
    for step_id in ids:
        machine.add_step(state, step_id=step_id, name=step_id)
        machine.apply_step_event(state, __import__('core.runtime', fromlist=['StepStateEvent']).StepStateEvent(__import__('core.runtime', fromlist=['StepEventType']).StepEventType.STARTED, step_id))
        result.append(StepClaim('p', 1, step_id, state.steps[step_id].started_at, TaskCapabilityRequirements(), 'agent'))
    return tuple(result)

class ParallelExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_best_effort_stable_order_and_state_terminal(self):
        state=AgentState.for_run_context('run'); source, _ = create_run_context(entry_agent_id='test'); state.run_id=source.run_id
        batch=claims(state, 'first','second','third')
        gates={item: asyncio.Event() for item in ('first','second','third')}
        class Driver:
            async def execute(self, claim, context):
                if claim.step_id == 'second': raise RuntimeError('private secret')
                await gates[claim.step_id].wait(); return claim.step_id
        task=asyncio.create_task(ParallelExecutor(max_concurrency=3).execute(claims=batch,state=state,run_context=source,driver=Driver(),failure_mode=ParallelFailureMode.BEST_EFFORT))
        await asyncio.sleep(0)
        gates['third'].set(); gates['first'].set()
        report=await task
        self.assertEqual(tuple(x.step_id for x in report.outcomes), ('first','second','third'))
        self.assertEqual(report.failed_step_ids, ('second',)); self.assertEqual(state.active_step_ids,set())
        self.assertNotIn('private', report.outcomes[1].error_message or '')

    async def test_resource_and_global_limits(self):
        state=AgentState.for_run_context('run'); context, _=create_run_context(entry_agent_id='test'); state.run_id=context.run_id
        batch=claims(state,'a','b','c'); entered=asyncio.Event(); release=asyncio.Event(); active=0; max_active=0; same=0; max_same=0
        class Driver:
            async def execute(self, claim, context):
                nonlocal active,max_active,same,max_same
                active+=1; max_active=max(max_active,active)
                if claim.step_id in ('a','b'): same+=1; max_same=max(max_same,same)
                entered.set(); await release.wait()
                active-=1
                if claim.step_id in ('a','b'): same-=1
        task=asyncio.create_task(ParallelExecutor(max_concurrency=2).execute(claims=batch,state=state,run_context=context,driver=Driver(),failure_mode=ParallelFailureMode.BEST_EFFORT,concurrency_specs={'a':StepConcurrencySpec('model',1),'b':StepConcurrencySpec('model',1),'c':StepConcurrencySpec('tool',2)}))
        await entered.wait(); await asyncio.sleep(0); release.set(); await task
        self.assertLessEqual(max_active,2); self.assertEqual(max_same,1)

    async def test_sync_driver_is_threaded(self):
        import threading
        state=AgentState.for_run_context('run'); context,_=create_run_context(entry_agent_id='test'); state.run_id=context.run_id; batch=claims(state,'a')
        caller=threading.get_ident()
        class Driver:
            def execute(self, claim, context): return threading.get_ident()
        report=await ParallelExecutor().execute(claims=batch,state=state,run_context=context,driver=Driver(),execution_mode=StepExecutionMode.SYNC_BLOCKING)
        self.assertNotEqual(report.outcomes[0].result,caller)

class ParallelExecutionCancellationAndPreflightTests(unittest.IsolatedAsyncioTestCase):
    def _batch(self, *ids):
        state = AgentState.for_run_context('run')
        context, _ = create_run_context(entry_agent_id='test')
        state.run_id = context.run_id
        return state, context, claims(state, *ids)

    async def test_fail_fast_cancels_global_semaphore_waiter(self):
        state, context, batch = self._batch('a', 'b')
        entered = asyncio.Event(); release = asyncio.Event()
        class Driver:
            async def execute(self, claim, context):
                if claim.step_id == 'a':
                    entered.set(); await release.wait(); raise RuntimeError()
                raise AssertionError('等待全局许可的步骤不得执行')
        task = asyncio.create_task(ParallelExecutor(max_concurrency=1).execute(claims=batch, state=state, run_context=context, driver=Driver()))
        await entered.wait(); release.set(); report = await task
        self.assertEqual(report.failed_step_ids, ('a',)); self.assertEqual(report.cancelled_step_ids, ('b',)); self.assertFalse(state.active_step_ids)

    async def test_fail_fast_cancels_resource_semaphore_waiter(self):
        state, context, batch = self._batch('a', 'b')
        entered = asyncio.Event(); release = asyncio.Event()
        class Driver:
            async def execute(self, claim, context):
                if claim.step_id == 'a': entered.set(); await release.wait(); raise RuntimeError()
                raise AssertionError('等待资源许可的步骤不得执行')
        specs = {key: StepConcurrencySpec('local-model', 1) for key in ('a', 'b')}
        task = asyncio.create_task(ParallelExecutor(max_concurrency=2).execute(claims=batch, state=state, run_context=context, driver=Driver(), concurrency_specs=specs))
        await entered.wait(); release.set(); report = await task
        self.assertEqual(report.cancelled_step_ids, ('b',)); self.assertFalse(state.active_step_ids)

    async def test_parent_cancel_cleans_semaphore_waiter_and_reraises(self):
        state, context, batch = self._batch('a', 'b')
        entered = asyncio.Event(); release = asyncio.Event()
        class Driver:
            async def execute(self, claim, context):
                entered.set(); await release.wait()
        task = asyncio.create_task(ParallelExecutor(max_concurrency=1).execute(claims=batch, state=state, run_context=context, driver=Driver()))
        await entered.wait(); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(state.active_step_ids)

    async def test_resource_conflict_and_driver_modes_cleanup_claims(self):
        state, context, batch = self._batch('a', 'b')
        class AsyncDriver:
            async def execute(self, claim, context): return 'ok'
        with self.assertRaisesRegex(Exception, 'RESOURCE_LIMIT_CONFLICT'):
            await ParallelExecutor().execute(claims=batch, state=state, run_context=context, driver=AsyncDriver(), concurrency_specs={'a': StepConcurrencySpec('model', 1), 'b': StepConcurrencySpec('model', 2)})
        self.assertFalse(state.active_step_ids)
        state, context, batch = self._batch('a')
        class SyncDriver:
            def execute(self, claim, context): return 'ok'
        with self.assertRaisesRegex(Exception, 'DRIVER_MODE_MISMATCH'):
            await ParallelExecutor().execute(claims=batch, state=state, run_context=context, driver=SyncDriver())
        self.assertFalse(state.active_step_ids)

class ParallelExecutionPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_ready_uses_one_policy_for_claim_and_execution_capacity(self):
        from core.runtime import Plan, PlanSource, PlanStep, SerialScheduler, TaskCapabilityRequirements
        context, _ = create_run_context(entry_agent_id='test')
        state = AgentState.for_run_context(context.run_id)
        AgentStateMachine().apply_run_event(state, RunStateEvent(RunEventType.STARTED))
        requirements = TaskCapabilityRequirements()
        plan = Plan('policy-plan', 1, '安全摘要', (PlanStep('a', 'a', '说明', (), '完成', 'agent', requirements), PlanStep('b', 'b', '说明', (), '完成', 'agent', requirements)), datetime.now(UTC), PlanSource.DETERMINISTIC)
        class Driver:
            async def execute(self, claim, context): return claim.step_id
        from core.runtime import ParallelExecutionPolicy
        report = await ParallelExecutor(max_concurrency=9).execute_ready(scheduler=SerialScheduler(), plan=plan, state=state, occurred_at=datetime.now(UTC), run_context=context, driver=Driver(), policy=ParallelExecutionPolicy(1, ParallelFailureMode.BEST_EFFORT))
        self.assertEqual(report.succeeded_step_ids, ('a',))
        self.assertEqual(state.steps['b'].status, StepStatus.PENDING)
