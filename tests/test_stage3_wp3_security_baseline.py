from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from core.knowledge_base.wiki_crawler import WikiCrawler
from core.agent_router import AgentRouter
from core.runtime import (
    BudgetLedger,
    FilesystemResourcePolicy,
    ResourceAccessRequest,
    ResourceAuthorizationError,
    ResourceAuthorizationOutcome,
    ResourceAuthorizationService,
    ResourceKind,
    ResourceOperation,
    RunBudget,
    RunContext,
    ToolResourceExtractorCatalog,
    ToolResourceExtractorDescriptor,
)
from core.runtime.tool_registry import ToolRegistry
from core.settings import (
    SETTINGS_SECURITY_POLICY_ERROR,
    EnvironmentProfile,
    Settings,
    SettingsValidationError,
)
from tools.local_tools import analyze_excel_data, list_files_in_dir, parse_filesystem_argument
from tools.registry import register_all_tools
import server


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_all_tools(registry)
    registry.freeze()
    return registry


def _service(*roots: Path) -> ResourceAuthorizationService:
    catalog = ToolResourceExtractorCatalog()
    catalog.register(
        ToolResourceExtractorDescriptor(
            "list_files", "argument_text", ResourceKind.DIRECTORY, ResourceOperation.READ
        )
    )
    catalog.register(
        ToolResourceExtractorDescriptor(
            "analyze_excel", "argument_text", ResourceKind.FILE, ResourceOperation.READ
        )
    )
    catalog.validate(_registry())
    catalog.freeze()
    return ResourceAuthorizationService(
        FilesystemResourcePolicy(tuple(str(root.resolve()) for root in roots)), catalog
    )


def _request(path: object, kind: ResourceKind = ResourceKind.DIRECTORY):
    return ResourceAccessRequest(
        "list_files" if kind is ResourceKind.DIRECTORY else "analyze_excel",
        path if isinstance(path, str) else "",
        kind,
        ResourceOperation.READ,
    )


def test_catalog_is_exact_validated_and_frozen() -> None:
    service = _service()
    assert service is not None
    catalog = ToolResourceExtractorCatalog()
    catalog.freeze()
    with pytest.raises(RuntimeError):
        catalog.register(
            ToolResourceExtractorDescriptor(
                "list_files", "argument_text", ResourceKind.DIRECTORY, ResourceOperation.READ
            )
        )


@pytest.mark.parametrize("raw", ["", "   ", "'unterminated", '"mismatch\''])
def test_shared_parser_rejects_ambiguous_input(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_filesystem_argument(raw)


def test_shared_parser_removes_one_matching_outer_pair(tmp_path: Path) -> None:
    expected = str(tmp_path.resolve())
    assert parse_filesystem_argument(f'  "{expected}"  ') == expected
    invocation = _registry().require("list_files").adapter.build_invocation(
        f'  "{expected}"  '
    )
    assert invocation.arguments["argument_text"] == expected


def test_authorized_real_file_tools(tmp_path: Path) -> None:
    marker = tmp_path / "marker.txt"
    marker.write_text("x", encoding="utf-8")
    csv = tmp_path / "sample.csv"
    csv.write_text("name,value\na,1\n", encoding="utf-8")
    service = _service(tmp_path)
    assert service.authorize(_request(str(tmp_path))).outcome is ResourceAuthorizationOutcome.ALLOW
    assert service.authorize(_request(str(csv), ResourceKind.FILE)).outcome is ResourceAuthorizationOutcome.ALLOW
    assert "marker.txt" in list_files_in_dir(str(tmp_path))
    assert "rows" in analyze_excel_data(str(csv))


def test_outside_real_file_tools_denied_before_business_access(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    secret = outside / "secret.csv"
    secret.write_text("secret\nWP3_OUTSIDE_MARKER\n", encoding="utf-8")
    service = _service(allowed)
    for request in (_request(str(outside)), _request(str(secret), ResourceKind.FILE)):
        with pytest.raises(ResourceAuthorizationError) as captured:
            service.require_authorized(request)
        assert captured.value.safe_error_code == "TOOL_RESOURCE_DENIED"
        assert str(outside) not in captured.value.safe_message
        assert "secret.csv" not in captured.value.safe_message


@pytest.mark.parametrize(
    ("tool_name", "kind", "leaf"),
    [
        ("list_files", ResourceKind.DIRECTORY, None),
        ("analyze_excel", ResourceKind.FILE, "secret.csv"),
    ],
)
def test_router_outside_denial_never_calls_execution_or_business(
    tmp_path: Path, tool_name: str, kind: ResourceKind, leaf: str | None
) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    target = outside
    if leaf is not None:
        target = outside / leaf
        target.write_text("secret\nvalue\n", encoding="utf-8")
    registry = server._populate_tool_registry()
    registration = registry.require(tool_name)
    business_calls = 0

    def forbidden_business_call(_argument: str) -> str:
        nonlocal business_calls
        business_calls += 1
        raise AssertionError("business filesystem access must not start")

    registration.adapter._function = forbidden_business_call

    class _ExecutionOracle:
        calls = 0

        def execute_sync(self, **kwargs):
            self.calls += 1
            raise AssertionError("ToolExecutionService must not be called")

    router = AgentRouter.__new__(AgentRouter)
    router.tool_registry = registry
    router.tool_governance_service = server._build_tool_governance(registry)
    router.resource_authorization_service = _service(allowed)
    execution = _ExecutionOracle()
    router.tool_execution_service = execution
    router._build_messages = lambda **_: [{"role": "system", "content": "base"}]
    router._plan_tool_call = lambda *_args, **_kwargs: (tool_name, str(target.resolve()))
    context = RunContext.create(entry_agent_id="core_router")
    context.attach_budget_ledger(BudgetLedger(RunBudget()))
    with pytest.raises(ResourceAuthorizationError):
        router._prepare_answer_messages("core_router", "query", run_context=context)
    assert execution.calls == 0
    assert business_calls == 0


def test_request_contract_mismatch_denies(tmp_path: Path) -> None:
    service = _service(tmp_path)
    forged = ResourceAccessRequest(
        "get_system_status",
        str(tmp_path),
        ResourceKind.DIRECTORY,
        ResourceOperation.READ,
    )
    assert service.authorize(forged).outcome is ResourceAuthorizationOutcome.DENY


def test_policy_vectors_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    child = root / "child"
    child.mkdir()
    prefix = tmp_path / "workspace-secret"
    prefix.mkdir()
    service = _service(root)
    assert service.authorize(_request(str(child))).outcome is ResourceAuthorizationOutcome.ALLOW
    denied = [
        str(root / "child" / ".." / ".." / "workspace-secret"),
        str(prefix),
        "relative/path",
        "C:relative",
        r"\\server\share\folder",
        r"\\?\C:\Windows",
        r"\\.\PhysicalDrive0",
        str(root / "missing"),
        str(root / "child") + "'",
    ]
    for candidate in denied:
        assert service.authorize(_request(candidate)).outcome is ResourceAuthorizationOutcome.DENY
    assert service.authorize(_request(str(root / "missing"), ResourceKind.FILE)).outcome is ResourceAuthorizationOutcome.DENY
    assert service.authorize(_request(str(child), ResourceKind.FILE)).outcome is ResourceAuthorizationOutcome.DENY
    assert _service().authorize(_request(str(child))).outcome is ResourceAuthorizationOutcome.DENY


def test_windows_case_semantics(tmp_path: Path) -> None:
    root = tmp_path / "CaseRoot"
    root.mkdir()
    service = _service(root)
    assert service.authorize(_request(str(root).swapcase())).outcome is ResourceAuthorizationOutcome.ALLOW


def test_real_link_escape_denied_when_supported(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip(
                f"ENVIRONMENT_BLOCKED: symlink_and_junction_unavailable:{exc.winerror}"
            )
    assert _service(root).authorize(_request(str(link))).outcome is ResourceAuthorizationOutcome.DENY


def _clear_security_env(monkeypatch) -> None:
    for name in (
        "LOCAL_AGENT_ENVIRONMENT_PROFILE",
        "LOCAL_AGENT_ENVIRONMENT_ID",
        "LOCAL_AGENT_TOOL_ALLOWED_READ_ROOTS",
        "LOCAL_AGENT_API_HOST",
        "LOCAL_AGENT_API_BASE_URL",
        "LOCAL_AGENT_REMOTE_API_KEY",
        "LOCAL_AGENT_WIKI_COOKIE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_settings_profile_roots_and_secret_repr(monkeypatch, tmp_path: Path) -> None:
    _clear_security_env(monkeypatch)
    marker = "WP3_TEST_SECRET_9F31"
    monkeypatch.setenv("LOCAL_AGENT_REMOTE_API_KEY", marker)
    monkeypatch.setenv("LOCAL_AGENT_WIKI_COOKIE", marker)
    local = Settings.load()
    assert local.tool_allowed_read_roots == (str(Path(local.project_root).resolve()),)
    assert marker not in repr(local)
    assert marker not in str(local)

    monkeypatch.setenv("LOCAL_AGENT_ENVIRONMENT_PROFILE", "TEST")
    assert Settings.load().tool_allowed_read_roots == ()
    monkeypatch.setenv("LOCAL_AGENT_TOOL_ALLOWED_READ_ROOTS", str(tmp_path))
    assert Settings.load().tool_allowed_read_roots == (str(tmp_path.resolve()),)


@pytest.mark.parametrize("explicit", [None, "", "   "])
def test_production_missing_or_empty_roots_fail_closed(
    monkeypatch, explicit: str | None
) -> None:
    _clear_security_env(monkeypatch)
    monkeypatch.setenv("LOCAL_AGENT_ENVIRONMENT_PROFILE", "PRODUCTION")
    monkeypatch.setenv("LOCAL_AGENT_ENVIRONMENT_ID", "prod")
    if explicit is not None:
        monkeypatch.setenv("LOCAL_AGENT_TOOL_ALLOWED_READ_ROOTS", explicit)
    with pytest.raises(SettingsValidationError) as captured:
        Settings.load()
    assert captured.value.field == "LOCAL_AGENT_TOOL_ALLOWED_READ_ROOTS"


def test_settings_roots_quote_and_case_deduplicate(monkeypatch, tmp_path: Path) -> None:
    _clear_security_env(monkeypatch)
    monkeypatch.setenv("LOCAL_AGENT_ENVIRONMENT_PROFILE", "TEST")
    value = str(tmp_path.resolve())
    monkeypatch.setenv(
        "LOCAL_AGENT_TOOL_ALLOWED_READ_ROOTS",
        f' "{value}" ; {value.swapcase()} ',
    )
    assert Settings.load().tool_allowed_read_roots == (value,)


def test_settings_file_cannot_be_root(monkeypatch, tmp_path: Path) -> None:
    _clear_security_env(monkeypatch)
    marker = tmp_path / "not-a-directory.txt"
    marker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("LOCAL_AGENT_ENVIRONMENT_PROFILE", "TEST")
    monkeypatch.setenv("LOCAL_AGENT_TOOL_ALLOWED_READ_ROOTS", str(marker))
    with pytest.raises(SettingsValidationError) as captured:
        Settings.load()
    assert captured.value.reason_code == "root_not_directory"


@pytest.mark.parametrize("raw", ["relative", r"C:relative", r"\\server\share", r"\\?\C:\x", ";", "C:\\x;;D:\\y"])
def test_settings_invalid_roots_are_safe(monkeypatch, raw: str) -> None:
    _clear_security_env(monkeypatch)
    monkeypatch.setenv("LOCAL_AGENT_ENVIRONMENT_PROFILE", "TEST")
    monkeypatch.setenv("LOCAL_AGENT_TOOL_ALLOWED_READ_ROOTS", raw)
    with pytest.raises(SettingsValidationError) as captured:
        Settings.load()
    assert captured.value.safe_error_code == SETTINGS_SECURITY_POLICY_ERROR
    assert raw not in str(captured.value)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.2", "example.test", "localhost"])
def test_production_rejects_non_loopback_host(monkeypatch, tmp_path: Path, host: str) -> None:
    _clear_security_env(monkeypatch)
    monkeypatch.setenv("LOCAL_AGENT_ENVIRONMENT_PROFILE", "PRODUCTION")
    monkeypatch.setenv("LOCAL_AGENT_ENVIRONMENT_ID", "prod")
    monkeypatch.setenv("LOCAL_AGENT_TOOL_ALLOWED_READ_ROOTS", str(tmp_path))
    monkeypatch.setenv("LOCAL_AGENT_API_HOST", host)
    with pytest.raises(SettingsValidationError) as captured:
        Settings.load()
    assert captured.value.safe_error_code == SETTINGS_SECURITY_POLICY_ERROR


@pytest.mark.parametrize("host", ["127.0.0.1", "127.2.3.4", "::1"])
def test_production_accepts_numeric_loopback(monkeypatch, tmp_path: Path, host: str) -> None:
    _clear_security_env(monkeypatch)
    monkeypatch.setenv("LOCAL_AGENT_ENVIRONMENT_PROFILE", "PRODUCTION")
    monkeypatch.setenv("LOCAL_AGENT_ENVIRONMENT_ID", "prod")
    monkeypatch.setenv("LOCAL_AGENT_TOOL_ALLOWED_READ_ROOTS", str(tmp_path))
    monkeypatch.setenv("LOCAL_AGENT_API_HOST", host)
    settings = Settings.load()
    assert settings.environment_profile is EnvironmentProfile.PRODUCTION
    if host == "::1":
        assert settings.api_base_url.startswith("http://[::1]:")


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8000",
        "http://user@127.0.0.1:8000",
        "http://127.0.0.1:8000/path",
        "http://127.0.0.1:8000/?q=1",
        "http://localhost:8000",
        "http://10.0.0.1:8000",
    ],
)
def test_production_rejects_invalid_api_base_url(monkeypatch, tmp_path: Path, url: str) -> None:
    _clear_security_env(monkeypatch)
    monkeypatch.setenv("LOCAL_AGENT_ENVIRONMENT_PROFILE", "PRODUCTION")
    monkeypatch.setenv("LOCAL_AGENT_ENVIRONMENT_ID", "prod")
    monkeypatch.setenv("LOCAL_AGENT_TOOL_ALLOWED_READ_ROOTS", str(tmp_path))
    monkeypatch.setenv("LOCAL_AGENT_API_BASE_URL", url)
    with pytest.raises(SettingsValidationError):
        Settings.load()


class _Response:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_wiki_remote_filename_and_containment(monkeypatch, tmp_path: Path, caplog) -> None:
    root = tmp_path / "wiki"
    outside = tmp_path / "outside"
    outside.mkdir()
    crawler = WikiCrawler("synthetic", str(root))
    monkeypatch.setattr(
        crawler.session,
        "post",
        lambda *args, **kwargs: _Response(
            {"status": "success", "data": {"docContent": {"content": "safe"}}}
        ),
    )
    crawler._fetch_and_save_article("../outside/marker", "title", str(root))
    assert list(outside.iterdir()) == []
    assert any(
        record.__dict__.get("safe_error_code") == "WIKI_REMOTE_FILENAME_INVALID"
        for record in caplog.records
    )

    escaped_save_dir = outside
    crawler._fetch_and_save_article("article-1", "title", str(escaped_save_dir))
    assert list(outside.iterdir()) == []
    assert any(
        record.__dict__.get("safe_error_code") == "WIKI_OUTPUT_PATH_DENIED"
        for record in caplog.records
    )

    crawler._fetch_and_save_article("article-2", "bad\x00title.", str(root))
    written = list(root.glob("article-2_*.md"))
    assert len(written) == 1
    assert written[0].read_text(encoding="utf-8") == "safe"
