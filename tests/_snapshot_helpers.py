#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Snapshot construction helpers shared by Stage6-WP1 PostgreSQL tests."""

from __future__ import annotations

from test_snapshot_contract import make_snapshot

from core.runtime.snapshot_contract import RunSnapshot


def build_snapshot(
    snapshot_id: str,
    run_id: str,
    *,
    salt: str | None = None,
) -> RunSnapshot:
    """Build a valid, digest-consistent RunSnapshot.

    ``salt`` changes the payload so two snapshots can share an ID with
    different content (used to exercise SNAPSHOT_ID_CONFLICT).
    """
    return make_snapshot(
        snapshot_id=snapshot_id,
        run_id=run_id,
        sensitive_text=salt if salt is not None else "SECRET_PROMPT_TEXT",
    )


__all__ = ["build_snapshot"]
