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
    # WP1-A：proxy/trust-env 现在由项目显式控制；默认矩阵与 Production 不变量冻结。
    assert "LOCAL_AGENT_REMOTE_TRUST_ENV" in text
    assert "LOCAL_AGENT_ENVIRONMENT_PROFILE" in text
    assert "SETTINGS_SECURITY_POLICY_ERROR" in text
    assert "不得手工编辑 SQLite" in text


def test_configuration_examples_contain_no_real_absolute_path_or_provider_url() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert re.search(r"[A-Za-z]:\\", text) is None
    assert "api.deepseek" not in text.lower()
    assert "Bearer " not in text


def test_gpu_layers_contract_is_consistent_between_rules_and_table() -> None:
    """冻结 GPU layers 合同事实：总则与字段表、Settings 必须一致为 >=-1。

    防止未来总则退回 `GPU layers >=0`，或字段表/总则再次漂移。只锁定必要
    合同事实，不做整篇文档字符串快照。
    """
    text = DOC.read_text(encoding="utf-8")
    rules_line = next(
        line for line in text.splitlines() if "GPU layers" in line
    )
    table_row = next(
        line
        for line in text.splitlines()
        if line.startswith("| `LOCAL_AGENT_MODEL_GPU_LAYERS`")
    )
    # 总则必须与字段表/实现一致：>= -1，且不得退回 >=0。
    assert "GPU layers ≥-1" in rules_line
    assert "GPU layers ≥0" not in rules_line
    # 字段表必须保持 backend 合同语义。
    assert "integer ≥-1" in table_row
    assert "`-1`=全部层 offload" in table_row
    assert "`0`=CPU" in table_row
    assert "指定 offload 层数" in table_row
    assert "≥0" not in table_row

