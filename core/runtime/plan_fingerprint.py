#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The single owner of deterministic Plan fingerprints."""

from __future__ import annotations

from core.runtime.planning import Plan
from core.runtime.snapshot_contract import PlanSnapshot
from core.runtime.snapshot_serialization import sha256_digest


class PlanFingerprinter:
    """Fingerprint only the immutable Plan definition and safe content digests."""

    @classmethod
    def fingerprint(cls, plan: Plan) -> str:
        return cls.fingerprint_snapshot(PlanSnapshot.from_plan(plan))

    @staticmethod
    def fingerprint_snapshot(snapshot: PlanSnapshot) -> str:
        if not isinstance(snapshot, PlanSnapshot):
            raise TypeError("snapshot must be a PlanSnapshot")
        source = {
            "fingerprint_schema_version": 1,
            "plan_schema_version": snapshot.plan_schema_version,
            "plan_id": snapshot.plan_id,
            "plan_version": snapshot.plan_version,
            "source": snapshot.source,
            "task_summary": snapshot.task_summary,
            "steps": snapshot.steps,
        }
        return sha256_digest(source)


__all__ = ["PlanFingerprinter"]
