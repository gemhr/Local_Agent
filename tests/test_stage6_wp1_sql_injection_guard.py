from __future__ import annotations

from pathlib import Path

from core.persistence.sql_guard import scan_paths, scan_source


def test_postgres_production_sql_uses_static_or_expression_construction():
    root = Path(__file__).resolve().parents[1]
    paths = (
        root / "core/persistence/database.py",
        root / "core/persistence/memory.py",
        root / "core/persistence/repositories/runtime.py",
    )
    assert scan_paths(paths) == ()


def test_guard_rejects_dynamic_text_and_driver_execute():
    source = """
from sqlalchemy import text
def bad(session, connection, value):
    sql = 'SELECT * FROM users WHERE name = ' + value
    session.execute(text(f'SELECT {value}'))
    connection.execute(sql)
"""
    findings = scan_source(source, path="synthetic.py")
    assert {finding.sink for finding in findings} == {"text", "execute"}


def test_guard_allows_bind_parameters_and_sqlalchemy_expressions():
    source = """
from sqlalchemy import select, text
def good(session, value):
    session.execute(text('SELECT * FROM users WHERE name = :value'), {'value': value})
    return session.execute(select(User).where(User.name == value))
"""
    assert scan_source(source) == ()
