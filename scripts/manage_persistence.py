#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Persistence preflight / explicit migration operator CLI（SCRIPT_ROLE）。

```powershell
uv run python scripts/manage_persistence.py preflight
uv run python scripts/manage_persistence.py migrate --backup-confirmed
```

本脚本只做编排；Memory / Journal / Snapshot / Checkpoint 的 schema truth 与
SQL 保留在对应 Store module。绝不执行 backup / restore / rollback production
命令（三者保持 manual runbook）。
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional, Sequence

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def main(argv: Optional[Sequence[str]] = None) -> int:
    from core.persistence_migration import (
        PERSISTENCE_MIGRATION_FAILED,
        PersistenceError,
        PersistencePaths,
        PreflightMode,
        PreflightStatus,
        run_persistence_migration,
        run_persistence_preflight,
    )
    from core.settings import Settings, SCRIPT_ROLE, validate_role_configuration

    parser = argparse.ArgumentParser(description="LocalAgent 持久化 preflight / migrate")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preflight", help="只读检测全部 Store")
    migrate_parser = subparsers.add_parser("migrate", help="显式迁移已有数据")
    migrate_parser.add_argument(
        "--backup-confirmed",
        action="store_true",
        help="Operator 声明已在 Server stopped 后完成 pre-migration backup",
    )
    args = parser.parse_args(argv)

    settings = Settings.load()
    validate_role_configuration(settings, role=SCRIPT_ROLE)
    paths = PersistencePaths(
        memory_db_path=settings.memory_db_path,
        event_journal_db_path=settings.event_journal_db_path,
        observability_checkpoint_db_path=settings.observability_checkpoint_db_path,
        snapshot_store_db_path=(
            settings.snapshot_store_db_path
            if settings.snapshot_store_enabled
            else None
        ),
    )

    if args.command == "preflight":
        results = run_persistence_preflight(paths, mode=PreflightMode.FULL)
        for result in results:
            print(_format_preflight_line(result))
        healthy = all(
            result.status in {PreflightStatus.NEW, PreflightStatus.CURRENT}
            for result in results
        )
        return 0 if healthy else 1

    if args.command == "migrate":
        try:
            outcome = run_persistence_migration(
                paths, backup_confirmed=bool(args.backup_confirmed)
            )
        except PersistenceError as exc:
            print(f"PERSISTENCE {exc.error_code}")
            return 1
        for result in outcome.results:
            print(_format_migration_line(result))
        return 1 if outcome.failed else 0

    parser.error("未知命令")
    return 2


def _format_preflight_line(result) -> str:
    parts = [result.store_id.value, result.status.value]
    if result.detected_version:
        parts.append(f"detected={result.detected_version}")
    if result.target_version:
        parts.append(f"target={result.target_version}")
    if result.safe_error_code:
        parts.append(result.safe_error_code)
    return " ".join(parts)


def _format_migration_line(result) -> str:
    parts = [result.store_id.value, result.action.value]
    if result.committed:
        parts.append("COMMITTED")
    else:
        parts.append("NO_COMMIT")
    if result.safe_error_code:
        parts.append(result.safe_error_code)
    return " ".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
