from pathlib import Path
import re


DOC = Path("docs/runtime/runtime_configuration_reference.md")
SETTINGS = Path("core/settings.py")


def _documented_configuration_names() -> set[str]:
    names = set()
    for line in DOC.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| `"):
            continue
        names.add(line.split("`")[1])
    return names


def _settings_environment_names() -> set[str]:
    source = SETTINGS.read_text(encoding="utf-8")
    prefixes = ("LOCAL_AGENT_", "CHAT_RUNTIME_MODE", "RUNTIME_")
    return {
        token
        for token in re.findall(r'["\']([A-Z][A-Z0-9_]+)["\']', source)
        if token.startswith(prefixes)
    }


def test_every_documented_configuration_name_comes_from_real_settings() -> None:
    documented = _documented_configuration_names()
    real = _settings_environment_names()

    assert documented
    assert documented <= real
    assert {
        "CHAT_RUNTIME_MODE",
        "LOCAL_AGENT_EVENT_JOURNAL_DB_PATH",
        "LOCAL_AGENT_SNAPSHOT_ENABLED",
        "RUNTIME_SHUTDOWN_GRACE_SECONDS",
    } <= documented


def test_configuration_reference_freezes_runtime_and_fault_boundaries() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert "默认 `COORDINATED`" in text
    assert "不会跨 Runtime fallback" in text
    assert "Snapshot 默认关闭" in text
    assert "生产配置入口：无" in text
    assert "默认 `controller=None`" in text
    assert "没有项目级 trust-environment/proxy Settings 开关" in text
    assert "不得手工编辑 SQLite" in text


def test_configuration_examples_contain_no_real_absolute_path_or_provider_url() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert re.search(r"[A-Za-z]:\\", text) is None
    assert "api.deepseek" not in text.lower()
    assert "Bearer " not in text

