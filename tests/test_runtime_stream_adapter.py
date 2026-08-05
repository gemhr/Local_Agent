import json
import unittest

from core.runtime import (
    OutputDeltaPayload,
    PlanningStartedPayload,
    RunStartedPayload,
    RuntimeEventChannel,
    RuntimeEventDraft,
    RuntimeEventTextAdapter,
    RuntimeEventType,
)


class RuntimeStreamAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def make_event(self, event_type, payload, *, step=False):
        channel = RuntimeEventChannel(2, run_id="run-a")
        return await channel.publish(
            RuntimeEventDraft(
                "run-a",
                "trace-a",
                event_type,
                "component",
                payload,
                "step-a" if step else None,
                1 if step else None,
            )
        )

    async def test_output_delta_is_raw_text(self):
        event = await self.make_event(
            RuntimeEventType.OUTPUT_DELTA,
            OutputDeltaPayload('answer\n"escaped" [[ORCH]] as user text'),
            step=True,
        )
        self.assertEqual(
            RuntimeEventTextAdapter().encode(event),
            'answer\n"escaped" [[ORCH]] as user text',
        )

    async def test_non_output_event_uses_legacy_marker_and_valid_json(self):
        event = await self.make_event(
            RuntimeEventType.RUN_STARTED, RunStartedPayload("RUNNING")
        )
        encoded = RuntimeEventTextAdapter().encode(event)
        self.assertTrue(encoded.startswith("[[ORCH]]"))
        payload = json.loads(encoded.removeprefix("[[ORCH]]"))
        self.assertEqual(payload["event_type"], "RUN_STARTED")
        self.assertEqual(payload["payload"], {"status": "RUNNING"})

    async def test_planning_event_is_control_json_not_user_text(self):
        event = await self.make_event(
            RuntimeEventType.PLANNING_STARTED,
            PlanningStartedPayload(1, 15000),
        )
        encoded = RuntimeEventTextAdapter().encode(event)
        self.assertTrue(encoded.startswith("[[ORCH]]"))
        payload = json.loads(encoded.removeprefix("[[ORCH]]"))
        self.assertEqual(payload["event_type"], "PLANNING_STARTED")
        self.assertNotIn("query", encoded.lower())

    async def test_adapter_uses_only_safe_fields_and_escapes_json(self):
        event = await self.make_event(
            RuntimeEventType.RUN_STARTED,
            RunStartedPayload('RUNNING"\nvalue'),
        )
        encoded = RuntimeEventTextAdapter().encode(event)
        self.assertIn('\\"', encoded)
        self.assertNotIn("prompt", encoded.lower())
        self.assertNotIn("api_key", encoded.lower())
        json.loads(encoded.removeprefix("[[ORCH]]"))

    async def test_custom_text_chunks_do_not_claim_sse_frames(self):
        event = await self.make_event(
            RuntimeEventType.RUN_STARTED, RunStartedPayload("RUNNING")
        )
        encoded = RuntimeEventTextAdapter().encode(event)
        self.assertFalse(encoded.startswith("data:"))
        self.assertNotIn("\nevent:", encoded)
