"""WP1-A P1-2：Settings Ownership Regression Guards（Round 2）。

锁定：
1. `Settings` 实例不可变（frozen dataclass，`FrozenInstanceError`）。
2. 生产 Python 源码中 raw environment read 只能存在于唯一允许位置
   `core/settings.py`。

Scanner 必须识别以下全部形式（含 os alias 绑定）：
- ``import os`` + ``os.getenv("X")`` / ``os.environ["X"]`` / ``os.environ.get("X")``
- ``import os as <alias>`` + ``<alias>.getenv(...)`` / ``<alias>.environ[...]``
- ``from os import getenv`` / ``from os import environ``

测试代码自身不算 production source；`.venv`/build/cache 不扫描。scripts 按
正式配置 Owner 合同调用 `Settings.load()`，不应直接读 env，因此同样纳入扫描。
"""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from core.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_ENV_READER = Path("core/settings.py")

_PRODUCTION_FILES = (
    [Path("server.py"), Path("main.py")]
    + sorted(Path("core").rglob("*.py"))
    + sorted(Path("tools").rglob("*.py"))
    + sorted(Path("ui").rglob("*.py"))
    + sorted(Path("scripts").rglob("*.py"))
)


def _scan_source_env_reads(source: str) -> list[str]:
    """返回源码中 raw environment read 事件；`import os` 本身不算 read。"""
    tree = ast.parse(source)
    events: list[str] = []
    os_bindings: set[str] = {"os"}

    # 第一遍：登记 `import os [as <alias>]` 与 `from os import getenv/environ`。
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os":
                    os_bindings.add(alias.asname or "os")
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name in {"getenv", "environ", "*"}:
                    events.append(f"from os import {alias.name}")

    # 第二遍：os 绑定上的 .getenv / .environ 视为 raw environment read。
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in {"getenv", "environ"}:
            value = node.value
            if isinstance(value, ast.Name) and value.id in os_bindings:
                events.append(f"{value.id}.{node.attr}")
    return events


def _file_env_read_events(path: Path) -> list[str]:
    """读取文件并对每个事件附加路径前缀；无法解析按失败处理。"""
    source = path.read_text(encoding="utf-8")
    try:
        events = _scan_source_env_reads(source)
    except SyntaxError:
        return [f"{path}: <syntax-error>"]
    return [f"{path}: {event}" for event in events]


def test_settings_instance_is_immutable() -> None:
    settings = Settings.load()
    with pytest.raises(FrozenInstanceError):
        settings.api_host = "10.0.0.9"
    with pytest.raises(FrozenInstanceError):
        settings.remote_api_key = "overwritten"


def test_only_core_settings_reads_environment_in_production_source() -> None:
    offenders: list[str] = []
    for path in _PRODUCTION_FILES:
        if not path.exists():
            continue
        if path == ALLOWED_ENV_READER:
            continue
        offenders.extend(_file_env_read_events(path))
    assert not offenders, f"second production env reader: {offenders}"


def test_core_settings_is_the_single_allowlisted_env_reader() -> None:
    assert ALLOWED_ENV_READER.exists()
    events = _file_env_read_events(ALLOWED_ENV_READER)
    assert events, "core/settings.py must be the env reader owner"
    assert any("os.getenv" in event or "os.environ" in event for event in events)


def test_scripts_do_not_read_environment_directly() -> None:
    script_offenders: list[str] = []
    for path in sorted(Path("scripts").rglob("*.py")):
        script_offenders.extend(_file_env_read_events(path))
    assert not script_offenders, (
        f"scripts must call Settings.load(), not read env: {script_offenders}"
    )


# ---- scanner source-snippet self-tests（防止 guard 被轻易绕过）----

_SNIPPET_MATRIX = (
    ("os.getenv(\"X\")", "os.getenv"),
    ("os.environ[\"X\"]", "os.environ"),
    ("os.environ.get(\"X\")", "os.environ"),
    ("import os\nos.getenv(\"X\")", "os.getenv"),
    ("from os import getenv\ngetenv(\"X\")", "from os import getenv"),
    ("from os import environ\nenviron[\"X\"]", "from os import environ"),
    ("from os import environ\nenviron.get(\"X\")", "from os import environ"),
    ("import os as _os\n_os.getenv(\"X\")", "_os.getenv"),
    ("import os as environment\nenvironment.environ[\"X\"]", "environment.environ"),
    (
        "import os as _os\n"
        "def f():\n"
        "    return _os.getenv(\"X\")\n",
        "_os.getenv",
    ),
)


@pytest.mark.parametrize(("snippet", "expected_token"), _SNIPPET_MATRIX)
def test_scanner_detects_each_env_read_form(snippet, expected_token) -> None:
    events = _scan_source_env_reads(snippet)
    assert events, f"scanner missed all reads in: {snippet!r}"
    assert any(expected_token in event for event in events), (
        f"scanner missed {expected_token!r} in: {snippet!r} (events={events})"
    )


def test_scanner_does_not_flag_plain_import_os() -> None:
    assert _scan_source_env_reads("import os\nimport os.path\n") == []
    assert _scan_source_env_reads("import os as os_alias\n") == []
    assert _scan_source_env_reads("from os import path\npath.join('a', 'b')\n") == []


def test_scanner_detects_os_alias_requires_an_actual_read() -> None:
    # 只 import 不读：不算 offender。
    assert _scan_source_env_reads("import os as _os\nvalue = _os.getcwd()\n") == []


# ---- 真实 production source scan（期望只有 core/settings.py 被 allowlist）----

def test_real_production_source_scan_only_allows_core_settings() -> None:
    scanned = [path for path in _PRODUCTION_FILES if path.exists()]
    allowed = {ALLOWED_ENV_READER}
    detected: list[str] = []
    for path in scanned:
        events = _file_env_read_events(path)
        if events:
            if path in allowed:
                continue
            detected.extend(events)
    assert not detected, f"unexpected production env readers: {detected}"
    # allowlist 位置必须真实存在且有 raw read。
    assert any(
        "os.getenv" in event or "os.environ" in event
        for event in _file_env_read_events(ALLOWED_ENV_READER)
    )
