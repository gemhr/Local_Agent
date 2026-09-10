#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Root pytest configuration.

Stage6-WP1: relational persistence is PostgreSQL-only, so the suite talks to a
real PostgreSQL **test** database instead of SQLite. Credentials are supplied by
the operator through environment variables; an optional git-ignored
``.env.test`` at the project root is loaded first so repeated local runs do not
need the values re-exported by hand. No credential is ever committed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_LOCAL_ENV_FILE = PROJECT_ROOT / ".env.test"


def _load_local_env_file(path: Path) -> None:
    """Minimal KEY=VALUE loader; never overrides an existing environment value."""
    if not path.is_file():
        return
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


_load_local_env_file(_LOCAL_ENV_FILE)

from tests._pg_fixtures import test_database_url  # noqa: E402

# Every test process composes persistence against the test database. Operators
# may override with an explicit LOCAL_AGENT_DATABASE_URL.
os.environ.setdefault("LOCAL_AGENT_DATABASE_URL", test_database_url())

pytest_plugins = ("tests._pg_fixtures",)
