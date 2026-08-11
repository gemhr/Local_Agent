#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""项目统一配置模块。

单一 Application Configuration Owner。所有环境变量读取与 precedence 计算
只发生在本模块；server/client/scripts 各自进程启动时调用一次
``Settings.load()``，运行中不 reload。解析严格性不随 Environment Profile
改变：相同显式文本在三个 Profile 中要么得到相同 typed value，要么得到
相同的 ``SettingsValidationError`` 失败。
"""

import importlib.metadata
import ipaddress
import math
import ntpath
import os
import re
import tomllib
import warnings
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit

from core.runtime.runtime_mode import ChatRuntimeMode


SETTINGS_PARSE_ERROR = "SETTINGS_PARSE_ERROR"
SETTINGS_VALIDATION_ERROR = "SETTINGS_VALIDATION_ERROR"
SETTINGS_SECURITY_POLICY_ERROR = "SETTINGS_SECURITY_POLICY_ERROR"
STARTUP_CONFIGURATION_ERROR = "STARTUP_CONFIGURATION_ERROR"

SERVER_ROLE = "SERVER"
CLIENT_ROLE = "CLIENT"
SCRIPT_ROLE = "SCRIPT"
ALLOWED_ROLES = frozenset({SERVER_ROLE, CLIENT_ROLE, SCRIPT_ROLE})

_MODEL_PROFILE_NAMES = frozenset({"fast", "balanced", "deep"})
_VALID_BACKENDS = frozenset({"local", "remote", "hybrid"})

# 安全低基数 identifier：不能是 URL、路径或自由文本。
ENVIRONMENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SERVICE_VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){0,3}(?:[-+][0-9A-Za-z._-]+)?$")

_PROFILE_VERIFY_TLS_DEFAULT = {
    "LOCAL": False,
    "TEST": True,
    "PRODUCTION": True,
}
_PROFILE_TRUST_ENV_DEFAULT = {
    "LOCAL": True,
    "TEST": False,
    "PRODUCTION": False,
}
_PROFILE_KB_REQUIRED_DEFAULT = {
    "LOCAL": False,
    "TEST": False,
    "PRODUCTION": True,
}
_PROFILE_ENVIRONMENT_ID_DEFAULT = {
    "LOCAL": "local",
    "TEST": "test",
    "PRODUCTION": None,
}


class SettingsValidationError(ValueError):
    """Safe startup configuration error.

    只保存 allowlist 字段：``safe_error_code``、field/env name 与
    ``reason_code``。绝不保存 raw invalid value、secret、Provider URL 或绝对
    内部路径。保持 ``ValueError`` 子类以兼容既有调用方。
    """

    def __init__(self, safe_error_code: str, field: str, reason_code: str) -> None:
        if not safe_error_code or not field or not reason_code:
            raise ValueError("SettingsValidationError requires safe fields")
        self.safe_error_code = safe_error_code
        self.field = field
        self.reason_code = reason_code
        super().__init__(
            f"{safe_error_code}: field={field} reason={reason_code}"
        )


class EnvironmentProfile(str, Enum):
    """部署环境 Profile；只管理少量经批准的默认值与安全不变量。"""

    LOCAL = "LOCAL"
    TEST = "TEST"
    PRODUCTION = "PRODUCTION"

    @classmethod
    def parse(cls, value: object) -> "EnvironmentProfile":
        """忽略首尾空白、大小写归一；未知或显式空值 fail closed。"""
        if value is None:
            return cls.LOCAL
        if not isinstance(value, str):
            raise SettingsValidationError(
                SETTINGS_VALIDATION_ERROR,
                "LOCAL_AGENT_ENVIRONMENT_PROFILE",
                "not_a_string",
            )
        normalized = value.strip().upper()
        if not normalized:
            raise SettingsValidationError(
                SETTINGS_VALIDATION_ERROR,
                "LOCAL_AGENT_ENVIRONMENT_PROFILE",
                "blank_value",
            )
        try:
            return cls(normalized)
        except ValueError:
            raise SettingsValidationError(
                SETTINGS_VALIDATION_ERROR,
                "LOCAL_AGENT_ENVIRONMENT_PROFILE",
                "unknown_profile",
            ) from None


def _env_strict_bool(name: str, default: bool) -> bool:
    """显式 bool 严格解析：只接受 1/0/true/false，其余显式值 fail closed。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true"}:
        return True
    if normalized in {"0", "false"}:
        return False
    raise SettingsValidationError(SETTINGS_PARSE_ERROR, name, "invalid_boolean")


def _env_strict_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """显式 int 严格解析 + contract range；非法或越界显式值 fail closed。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        raise SettingsValidationError(SETTINGS_PARSE_ERROR, name, "invalid_integer") from None
    if minimum is not None and value < minimum:
        raise SettingsValidationError(SETTINGS_VALIDATION_ERROR, name, "below_minimum")
    if maximum is not None and value > maximum:
        raise SettingsValidationError(SETTINGS_VALIDATION_ERROR, name, "above_maximum")
    return value


def _env_strict_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    positive: bool = False,
) -> float:
    """显式 float 严格解析；非有限值、越界或非正数显式值 fail closed。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        raise SettingsValidationError(SETTINGS_PARSE_ERROR, name, "invalid_number") from None
    if not math.isfinite(value):
        raise SettingsValidationError(SETTINGS_VALIDATION_ERROR, name, "not_finite")
    if positive and value <= 0:
        raise SettingsValidationError(SETTINGS_VALIDATION_ERROR, name, "must_be_positive")
    if minimum is not None and value < minimum:
        raise SettingsValidationError(SETTINGS_VALIDATION_ERROR, name, "below_minimum")
    if maximum is not None and value > maximum:
        raise SettingsValidationError(SETTINGS_VALIDATION_ERROR, name, "above_maximum")
    return value


def _env_runtime_mode() -> ChatRuntimeMode:
    raw = os.getenv("CHAT_RUNTIME_MODE")
    if raw is None:
        return ChatRuntimeMode.COORDINATED
    try:
        return ChatRuntimeMode.parse(raw)
    except (TypeError, ValueError):
        raise SettingsValidationError(
            SETTINGS_VALIDATION_ERROR,
            "CHAT_RUNTIME_MODE",
            "unsupported",
        ) from None


def _env_model_profile() -> str:
    """模型资源预设只管理 fast/balanced/deep；未知或显式空值 fail closed。"""
    raw = os.getenv("LOCAL_AGENT_MODEL_PROFILE")
    if raw is None:
        return "balanced"
    normalized = raw.strip().lower()
    if not normalized or normalized not in _MODEL_PROFILE_NAMES:
        raise SettingsValidationError(
            SETTINGS_VALIDATION_ERROR,
            "LOCAL_AGENT_MODEL_PROFILE",
            "unknown_profile",
        )
    return normalized


def _env_backend() -> str:
    """LLM backend 严格枚举：local/remote/hybrid。"""
    raw = os.getenv("LOCAL_AGENT_LLM_BACKEND")
    if raw is None:
        return "remote"
    normalized = raw.strip().lower()
    if normalized not in _VALID_BACKENDS:
        raise SettingsValidationError(
            SETTINGS_VALIDATION_ERROR,
            "LOCAL_AGENT_LLM_BACKEND",
            "unknown_backend",
        )
    return normalized


def _env_environment_id(profile: EnvironmentProfile) -> str:
    """安全低基数 identifier；PRODUCTION 必须显式配置。"""
    default = _PROFILE_ENVIRONMENT_ID_DEFAULT[profile.value]
    raw = os.getenv("LOCAL_AGENT_ENVIRONMENT_ID")
    if raw is None:
        if default is None:
            raise SettingsValidationError(
                SETTINGS_VALIDATION_ERROR,
                "LOCAL_AGENT_ENVIRONMENT_ID",
                "required_for_production",
            )
        return default
    normalized = raw.strip()
    if not normalized or ENVIRONMENT_ID_PATTERN.fullmatch(normalized) is None:
        raise SettingsValidationError(
            SETTINGS_VALIDATION_ERROR,
            "LOCAL_AGENT_ENVIRONMENT_ID",
            "invalid_identifier",
        )
    return normalized


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _strip_matching_outer_quotes(value: str, *, env_name: str) -> str:
    stripped = value.strip()
    if not stripped:
        return ""
    starts_quote = stripped[0] in "'\""
    ends_quote = stripped[-1] in "'\""
    if starts_quote or ends_quote:
        if len(stripped) < 2 or stripped[0] != stripped[-1] or not starts_quote:
            raise SettingsValidationError(
                SETTINGS_SECURITY_POLICY_ERROR, env_name, "invalid_root_syntax"
            )
        stripped = stripped[1:-1].strip()
    if not stripped or "\x00" in stripped or "'" in stripped or '"' in stripped:
        raise SettingsValidationError(
            SETTINGS_SECURITY_POLICY_ERROR, env_name, "invalid_root_syntax"
        )
    return stripped


def _is_drive_qualified_local_path(value: str) -> bool:
    normalized = value.replace("/", "\\")
    if normalized.lower().startswith(("\\\\", "\\?\\", "\\.\\")):
        return False
    drive, tail = ntpath.splitdrive(normalized)
    return bool(
        re.fullmatch(r"[A-Za-z]:", drive)
        and tail.startswith("\\")
        and not tail.startswith("\\\\")
    )


def _tool_allowed_read_roots(
    profile: EnvironmentProfile, project_root: str
) -> tuple[str, ...]:
    env_name = "LOCAL_AGENT_TOOL_ALLOWED_READ_ROOTS"
    raw = os.getenv(env_name)
    if raw is None:
        if profile is EnvironmentProfile.LOCAL:
            values = (project_root,)
        elif profile is EnvironmentProfile.TEST:
            values = ()
        else:
            raise SettingsValidationError(
                SETTINGS_SECURITY_POLICY_ERROR,
                env_name,
                "required_for_production",
            )
    elif not raw.strip():
        values = ()
    else:
        segments = raw.split(";")
        if any(not segment.strip() for segment in segments):
            raise SettingsValidationError(
                SETTINGS_SECURITY_POLICY_ERROR, env_name, "empty_root_segment"
            )
        values = tuple(
            _strip_matching_outer_quotes(segment, env_name=env_name)
            for segment in segments
        )
    if profile is EnvironmentProfile.PRODUCTION and not values:
        raise SettingsValidationError(
            SETTINGS_SECURITY_POLICY_ERROR, env_name, "required_for_production"
        )

    canonical: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not _is_drive_qualified_local_path(value):
            raise SettingsValidationError(
                SETTINGS_SECURITY_POLICY_ERROR, env_name, "invalid_local_root"
            )
        try:
            resolved = Path(value).resolve(strict=True)
            is_directory = resolved.is_dir()
        except (OSError, RuntimeError, ValueError):
            raise SettingsValidationError(
                SETTINGS_SECURITY_POLICY_ERROR, env_name, "root_unavailable"
            ) from None
        if not is_directory:
            raise SettingsValidationError(
                SETTINGS_SECURITY_POLICY_ERROR, env_name, "root_not_directory"
            )
        canonical_value = str(resolved)
        comparison_key = ntpath.normcase(ntpath.normpath(canonical_value))
        if comparison_key not in seen:
            seen.add(comparison_key)
            canonical.append(canonical_value)
    return tuple(canonical)


def _validate_production_local_api(
    profile: EnvironmentProfile, api_host: str, api_base_url: str
) -> None:
    if profile is not EnvironmentProfile.PRODUCTION:
        return
    try:
        host_ip = ipaddress.ip_address(api_host)
    except ValueError:
        raise SettingsValidationError(
            SETTINGS_SECURITY_POLICY_ERROR,
            "LOCAL_AGENT_API_HOST",
            "production_requires_numeric_loopback",
        ) from None
    if not host_ip.is_loopback:
        raise SettingsValidationError(
            SETTINGS_SECURITY_POLICY_ERROR,
            "LOCAL_AGENT_API_HOST",
            "production_requires_loopback",
        )
    try:
        parsed = urlsplit(api_base_url)
        base_host = parsed.hostname
        base_ip = ipaddress.ip_address(base_host or "")
        _ = parsed.port
    except (ValueError, TypeError):
        raise SettingsValidationError(
            SETTINGS_SECURITY_POLICY_ERROR,
            "LOCAL_AGENT_API_BASE_URL",
            "production_requires_loopback_http_url",
        ) from None
    if (
        parsed.scheme.lower() != "http"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or not base_ip.is_loopback
    ):
        raise SettingsValidationError(
            SETTINGS_SECURITY_POLICY_ERROR,
            "LOCAL_AGENT_API_BASE_URL",
            "production_requires_loopback_http_url",
        )


def _read_project_metadata() -> tuple[str | None, str | None]:
    """读取仓库 pyproject.toml 的 [project] name/version（source checkout 事实）。"""
    path = os.path.join(_project_root(), "pyproject.toml")
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except Exception:
        return None, None
    project = data.get("project", {})
    name = project.get("name")
    version = project.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        return None, None
    return name, version


def _resolve_service_version() -> str:
    """从真实 project metadata 解析不可变 build version。

    优先 installed distribution metadata，否则使用 source checkout 的
    pyproject.toml 同版本；均无法安全解析或格式非法时 fail closed。
    不运行 git 命令，不写死猜测值。
    """
    name, pyproject_version = _read_project_metadata()
    resolved = None
    if name:
        try:
            resolved = importlib.metadata.version(name)
        except Exception:
            resolved = None
    if resolved is None:
        resolved = pyproject_version
    if resolved is None:
        raise SettingsValidationError(
            SETTINGS_VALIDATION_ERROR,
            "service_version",
            "unresolved",
        )
    if _SERVICE_VERSION_PATTERN.fullmatch(resolved) is None:
        raise SettingsValidationError(
            SETTINGS_VALIDATION_ERROR,
            "service_version",
            "invalid_version_format",
        )
    return resolved


def _warn_deprecated_observability_timeout() -> None:
    """DEPRECATED：只输出 env 名与 replacement，不输出配置值。"""
    if os.getenv("LOCAL_AGENT_OBSERVABILITY_SHUTDOWN_TIMEOUT_SECONDS") is None:
        return
    warnings.warn(
        "LOCAL_AGENT_OBSERVABILITY_SHUTDOWN_TIMEOUT_SECONDS is deprecated and "
        "unused; replacement is RUNTIME_COMPONENT_CLOSE_TIMEOUT_SECONDS",
        DeprecationWarning,
        stacklevel=2,
    )


def validate_role_configuration(settings: "Settings", *, role: str) -> None:
    """校验当前进程 role 的必填配置边界。

    Settings.load() 已执行 Parse + 通用 Semantic Validation；本步骤只补充
    当前 role 才消费的 required 字段检查，不做任何 I/O。必须在首个
    Application Resource 构造前由 entrypoint 调用。
    """
    if role not in ALLOWED_ROLES:
        raise ValueError("unsupported role")
    if role == SERVER_ROLE:
        _validate_server_role(settings)


def _validate_server_role(settings: "Settings") -> None:
    backend = settings.llm_backend
    if backend not in {"remote", "hybrid"}:
        return
    endpoint = settings.remote_api_base_url
    if not endpoint:
        raise SettingsValidationError(
            STARTUP_CONFIGURATION_ERROR,
            "LOCAL_AGENT_REMOTE_API_BASE_URL",
            "required_for_remote_backend",
        )
    if settings.environment_profile is EnvironmentProfile.PRODUCTION:
        if not endpoint.lower().startswith("https://"):
            raise SettingsValidationError(
                SETTINGS_SECURITY_POLICY_ERROR,
                "LOCAL_AGENT_REMOTE_API_BASE_URL",
                "production_requires_https",
            )
        if not settings.remote_verify_tls:
            raise SettingsValidationError(
                SETTINGS_SECURITY_POLICY_ERROR,
                "LOCAL_AGENT_REMOTE_VERIFY_TLS",
                "production_requires_tls_verification",
            )


@dataclass(frozen=True)
class Settings:
    """不可变运行时配置对象。"""

    project_root: str
    environment_profile: EnvironmentProfile
    api_host: str
    api_port: int
    api_base_url: str
    chat_runtime_mode: ChatRuntimeMode
    model_profile: str
    llm_backend: str
    model_path: str
    remote_model_name: str
    remote_provider_kind: str
    remote_api_base_url: str
    remote_api_key: str = field(repr=False)
    remote_timeout_seconds: int
    remote_verify_tls: bool
    remote_trust_env: bool
    client_trust_env: bool
    remote_enable_thinking: bool
    remote_context_window: int
    local_fixed_call_cost_units: int
    local_input_cost_units_per_1k_tokens: int
    local_output_cost_units_per_1k_tokens: int
    local_estimated_latency_ms: int
    remote_fixed_call_cost_units: int
    remote_input_cost_units_per_1k_tokens: int
    remote_output_cost_units_per_1k_tokens: int
    remote_estimated_latency_ms: int
    model_breaker_failure_threshold: int
    model_breaker_recovery_timeout_seconds: int
    model_breaker_half_open_max_calls: int
    model_breaker_count_rate_limited: bool
    model_threads: int
    model_context: int
    model_gpu_layers: int
    model_max_tokens: int
    history_window_size: int
    summary_trigger_messages: int
    summary_keep_recent: int
    summary_max_chars: int
    chroma_dir: str
    embedding_model_path: str
    embedding_query_prompt_name: str
    embedding_batch_size: int
    memory_db_path: str
    event_journal_db_path: str
    snapshot_store_enabled: bool
    snapshot_store_db_path: str
    observability_checkpoint_db_path: str
    observability_queue_capacity: int
    observability_shutdown_timeout_seconds: int
    runtime_disconnect_grace_seconds: float
    runtime_shutdown_grace_seconds: float
    runtime_component_close_timeout_seconds: float
    metrics_tool_name_allowlist: tuple[str, ...]
    knowledge_collection_name: str
    rag_top_k: int
    rag_min_score: float
    rag_doc_max_chars: int
    rag_context_max_chars: int
    orchestration_enabled: bool
    orchestration_max_agents: int
    sync_enabled: bool
    wiki_cookie: str = field(repr=False)
    local_knowledge_base_dir: str
    knowledge_base_required: bool
    environment_id: str
    service_version: str
    blocking_max_workers: int
    blocking_max_pending_tasks: int
    event_channel_capacity: int
    planning_timeout_seconds: float
    step_result_per_result_chars: int
    step_result_run_total_chars: int
    step_result_max_entries: int
    tool_allowed_read_roots: tuple[str, ...] = ()

    @classmethod
    def load(cls) -> "Settings":
        """从环境变量按 precedence 构建配置对象。

        precedence：code safe default < environment profile default < model
        resource preset < explicit environment variable < derived value。
        merge 只发生一次且只发生在本方法；其他模块不得二次 merge。
        """
        project_root = _project_root()
        environment_profile = EnvironmentProfile.parse(
            os.getenv("LOCAL_AGENT_ENVIRONMENT_PROFILE")
        )
        api_host = os.getenv("LOCAL_AGENT_API_HOST", "127.0.0.1").strip()
        api_port = _env_strict_int(
            "LOCAL_AGENT_API_PORT", 8000, minimum=1, maximum=65535
        )
        derived_api_host = f"[{api_host}]" if ":" in api_host else api_host
        api_base_url = os.getenv(
            "LOCAL_AGENT_API_BASE_URL", f"http://{derived_api_host}:{api_port}"
        ).strip()
        _validate_production_local_api(environment_profile, api_host, api_base_url)
        tool_allowed_read_roots = _tool_allowed_read_roots(
            environment_profile, project_root
        )
        chat_runtime_mode = _env_runtime_mode()

        # 预设参数已从 7B 本地 CPU 推理调优为更适配 27B 远端模型：
        # - 允许更长回复（model_max_tokens）
        # - 保留更长会话记忆窗口（history_window_size）
        # - 放宽摘要与 RAG 上下文容量，减少信息截断
        # - 提高远端等待时间，适配 27B 在复杂问题下更长的推理时延
        profile_name = _env_model_profile()
        presets = {
            "fast": {
                "model_threads": 8,
                "model_context": 3072,
                "model_max_tokens": 640,
                "history_window_size": 8,
                "summary_trigger_messages": 14,
                "summary_keep_recent": 8,
                "summary_max_chars": 1000,
                "rag_top_k": 3,
                "rag_doc_max_chars": 700,
                "rag_context_max_chars": 1400,
            },
            "balanced": {
                "model_threads": 10,
                "model_context": 4096,
                "model_max_tokens": 1024,
                "history_window_size": 12,
                "summary_trigger_messages": 20,
                "summary_keep_recent": 12,
                "summary_max_chars": 1600,
                "rag_top_k": 3,
                "rag_doc_max_chars": 1000,
                "rag_context_max_chars": 2400,
            },
            "deep": {
                "model_threads": 12,
                "model_context": 6144,
                "model_max_tokens": 1536,
                "history_window_size": 16,
                "summary_trigger_messages": 24,
                "summary_keep_recent": 14,
                "summary_max_chars": 2200,
                "rag_top_k": 4,
                "rag_doc_max_chars": 1200,
                "rag_context_max_chars": 3200,
            },
        }
        preset = presets[profile_name]
        llm_backend = _env_backend()

        # Environment Profile 只管理下列字段的默认值；显式 env 仍最高优先。
        remote_verify_tls = _env_strict_bool(
            "LOCAL_AGENT_REMOTE_VERIFY_TLS",
            _PROFILE_VERIFY_TLS_DEFAULT[environment_profile.value],
        )
        remote_trust_env = _env_strict_bool(
            "LOCAL_AGENT_REMOTE_TRUST_ENV",
            _PROFILE_TRUST_ENV_DEFAULT[environment_profile.value],
        )
        # Client HTTP Proxy 治理：控制 Desktop Client → LocalAgent Server
        # 传输是否继承进程系统代理。与 remote_trust_env 完全独立，属于两个
        # 不同 transport scope。默认 True 保持 requests 既有行为；不随
        # Environment Profile 改变默认值。
        client_trust_env = _env_strict_bool(
            "LOCAL_AGENT_CLIENT_TRUST_ENV", True
        )
        knowledge_base_required = _env_strict_bool(
            "LOCAL_AGENT_KB_REQUIRED",
            _PROFILE_KB_REQUIRED_DEFAULT[environment_profile.value],
        )
        environment_id = _env_environment_id(environment_profile)
        service_version = _resolve_service_version()

        # 7 个批准进入 Settings 的 Runtime Application Knob；默认保持当前行为。
        blocking_max_workers = _env_strict_int(
            "LOCAL_AGENT_BLOCKING_MAX_WORKERS", 4, minimum=1
        )
        blocking_max_pending_tasks = _env_strict_int(
            "LOCAL_AGENT_BLOCKING_MAX_PENDING_TASKS", 8, minimum=0
        )
        event_channel_capacity = _env_strict_int(
            "LOCAL_AGENT_EVENT_CHANNEL_CAPACITY", 32, minimum=1
        )
        planning_timeout_seconds = _env_strict_float(
            "LOCAL_AGENT_PLANNING_TIMEOUT_SECONDS", 15.0, positive=True
        )
        step_result_per_result_chars = _env_strict_int(
            "LOCAL_AGENT_STEP_RESULT_PER_RESULT_CHARS", 20000, minimum=1
        )
        step_result_run_total_chars = _env_strict_int(
            "LOCAL_AGENT_STEP_RESULT_RUN_TOTAL_CHARS", 60000, minimum=1
        )
        step_result_max_entries = _env_strict_int(
            "LOCAL_AGENT_STEP_RESULT_MAX_ENTRIES", 16, minimum=1
        )
        if step_result_run_total_chars < step_result_per_result_chars:
            raise SettingsValidationError(
                SETTINGS_VALIDATION_ERROR,
                "LOCAL_AGENT_STEP_RESULT_RUN_TOTAL_CHARS",
                "run_total_below_per_result",
            )

        # DEPRECATED env 只产生一次安全 warning，不改变行为。
        _warn_deprecated_observability_timeout()

        return cls(
            project_root=project_root,
            environment_profile=environment_profile,
            api_host=api_host,
            api_port=api_port,
            api_base_url=api_base_url,
            chat_runtime_mode=chat_runtime_mode,
            model_profile=profile_name,
            llm_backend=llm_backend,
            model_path=os.getenv(
                "LOCAL_AGENT_MODEL_PATH",
                os.path.join(project_root, "data", "models", "qwen2.5-7b-instruct-q4_k_m.gguf"),
            ),
            remote_model_name=os.getenv("LOCAL_AGENT_REMOTE_MODEL_NAME", "Qwen3.5-27B"),
            remote_provider_kind=os.getenv(
                "LOCAL_AGENT_REMOTE_PROVIDER_KIND", "openai_compatible"
            ).lower(),
            remote_api_base_url=os.getenv("LOCAL_AGENT_REMOTE_API_BASE_URL", ""),
            remote_api_key=os.getenv("LOCAL_AGENT_REMOTE_API_KEY", ""),
            remote_timeout_seconds=_env_strict_int(
                "LOCAL_AGENT_REMOTE_TIMEOUT_SECONDS", 120, minimum=1
            ),
            remote_verify_tls=remote_verify_tls,
            remote_trust_env=remote_trust_env,
            client_trust_env=client_trust_env,
            remote_enable_thinking=_env_strict_bool(
                "LOCAL_AGENT_REMOTE_ENABLE_THINKING", False
            ),
            remote_context_window=_env_strict_int(
                "LOCAL_AGENT_REMOTE_CONTEXT_WINDOW", 32768, minimum=1
            ),
            local_fixed_call_cost_units=_env_strict_int(
                "LOCAL_AGENT_LOCAL_FIXED_CALL_COST_UNITS", 1, minimum=0
            ),
            local_input_cost_units_per_1k_tokens=_env_strict_int(
                "LOCAL_AGENT_LOCAL_INPUT_COST_UNITS_PER_1K_TOKENS", 1, minimum=0
            ),
            local_output_cost_units_per_1k_tokens=_env_strict_int(
                "LOCAL_AGENT_LOCAL_OUTPUT_COST_UNITS_PER_1K_TOKENS", 1, minimum=0
            ),
            local_estimated_latency_ms=_env_strict_int(
                "LOCAL_AGENT_LOCAL_ESTIMATED_LATENCY_MS", 1000, minimum=1
            ),
            remote_fixed_call_cost_units=_env_strict_int(
                "LOCAL_AGENT_REMOTE_FIXED_CALL_COST_UNITS", 10, minimum=0
            ),
            remote_input_cost_units_per_1k_tokens=_env_strict_int(
                "LOCAL_AGENT_REMOTE_INPUT_COST_UNITS_PER_1K_TOKENS", 2, minimum=0
            ),
            remote_output_cost_units_per_1k_tokens=_env_strict_int(
                "LOCAL_AGENT_REMOTE_OUTPUT_COST_UNITS_PER_1K_TOKENS", 4, minimum=0
            ),
            remote_estimated_latency_ms=_env_strict_int(
                "LOCAL_AGENT_REMOTE_ESTIMATED_LATENCY_MS", 3000, minimum=1
            ),
            model_breaker_failure_threshold=_env_strict_int(
                "LOCAL_AGENT_MODEL_BREAKER_FAILURE_THRESHOLD", 3, minimum=1
            ),
            model_breaker_recovery_timeout_seconds=_env_strict_int(
                "LOCAL_AGENT_MODEL_BREAKER_RECOVERY_TIMEOUT_SECONDS", 30, minimum=1
            ),
            model_breaker_half_open_max_calls=_env_strict_int(
                "LOCAL_AGENT_MODEL_BREAKER_HALF_OPEN_MAX_CALLS", 1, minimum=1
            ),
            model_breaker_count_rate_limited=_env_strict_bool(
                "LOCAL_AGENT_MODEL_BREAKER_COUNT_RATE_LIMITED", True
            ),
            model_threads=_env_strict_int(
                "LOCAL_AGENT_MODEL_THREADS", preset["model_threads"], minimum=1
            ),
            model_context=_env_strict_int(
                "LOCAL_AGENT_MODEL_CONTEXT", preset["model_context"], minimum=1
            ),
            model_gpu_layers=_env_strict_int(
                "LOCAL_AGENT_MODEL_GPU_LAYERS", 0, minimum=-1
            ),
            model_max_tokens=_env_strict_int(
                "LOCAL_AGENT_MODEL_MAX_TOKENS", preset["model_max_tokens"], minimum=1
            ),
            history_window_size=_env_strict_int(
                "LOCAL_AGENT_HISTORY_WINDOW_SIZE",
                preset["history_window_size"],
                minimum=1,
            ),
            summary_trigger_messages=_env_strict_int(
                "LOCAL_AGENT_SUMMARY_TRIGGER_MESSAGES",
                preset["summary_trigger_messages"],
                minimum=1,
            ),
            summary_keep_recent=_env_strict_int(
                "LOCAL_AGENT_SUMMARY_KEEP_RECENT",
                preset["summary_keep_recent"],
                minimum=1,
            ),
            summary_max_chars=_env_strict_int(
                "LOCAL_AGENT_SUMMARY_MAX_CHARS", preset["summary_max_chars"], minimum=1
            ),
            chroma_dir=os.getenv(
                "LOCAL_AGENT_CHROMA_DIR", os.path.join(project_root, "chroma_db")
            ),
            embedding_model_path=os.getenv(
                "LOCAL_AGENT_EMBEDDING_MODEL_PATH",
                os.path.join(project_root, "data", "models", "bge-large-zh-v1.5"),
            ),
            embedding_query_prompt_name=os.getenv(
                "LOCAL_AGENT_EMBEDDING_QUERY_PROMPT_NAME", ""
            ),
            embedding_batch_size=_env_strict_int(
                "LOCAL_AGENT_EMBEDDING_BATCH_SIZE", 8, minimum=1
            ),
            memory_db_path=os.getenv(
                "LOCAL_AGENT_MEMORY_DB_PATH",
                os.path.join(project_root, "data", "database", "agent_memory.db"),
            ),
            event_journal_db_path=os.getenv(
                "LOCAL_AGENT_EVENT_JOURNAL_DB_PATH",
                os.path.join(
                    project_root, "data", "database", "runtime_event_journal.db"
                ),
            ),
            snapshot_store_enabled=_env_strict_bool(
                "LOCAL_AGENT_SNAPSHOT_ENABLED", False
            ),
            snapshot_store_db_path=os.getenv(
                "LOCAL_AGENT_SNAPSHOT_DB_PATH",
                os.path.join(
                    project_root, "data", "database", "runtime_snapshots.db"
                ),
            ),
            observability_checkpoint_db_path=os.getenv(
                "LOCAL_AGENT_OBSERVABILITY_CHECKPOINT_DB_PATH",
                os.path.join(
                    project_root,
                    "data",
                    "database",
                    "runtime_observability_checkpoint.db",
                ),
            ),
            observability_queue_capacity=_env_strict_int(
                "LOCAL_AGENT_OBSERVABILITY_QUEUE_CAPACITY", 256, minimum=1
            ),
            observability_shutdown_timeout_seconds=_env_strict_int(
                "LOCAL_AGENT_OBSERVABILITY_SHUTDOWN_TIMEOUT_SECONDS", 5, minimum=1
            ),
            runtime_disconnect_grace_seconds=_env_strict_float(
                "RUNTIME_DISCONNECT_GRACE_SECONDS", 0.75, minimum=0.0
            ),
            runtime_shutdown_grace_seconds=_env_strict_float(
                "RUNTIME_SHUTDOWN_GRACE_SECONDS", 5.0, minimum=0.0
            ),
            runtime_component_close_timeout_seconds=_env_strict_float(
                "RUNTIME_COMPONENT_CLOSE_TIMEOUT_SECONDS", 5.0, minimum=0.0
            ),
            metrics_tool_name_allowlist=tuple(
                value.strip()
                for value in os.getenv(
                    "LOCAL_AGENT_METRICS_TOOL_NAME_ALLOWLIST", ""
                ).split(",")
                if value.strip()
            ),
            knowledge_collection_name=os.getenv(
                "LOCAL_AGENT_KB_COLLECTION", "huawei_wiki_collection"
            ),
            rag_top_k=_env_strict_int(
                "LOCAL_AGENT_RAG_TOP_K", preset["rag_top_k"], minimum=1
            ),
            rag_min_score=_env_strict_float(
                "LOCAL_AGENT_RAG_MIN_SCORE", 0.55, minimum=0.0, maximum=1.0
            ),
            rag_doc_max_chars=_env_strict_int(
                "LOCAL_AGENT_RAG_DOC_MAX_CHARS",
                preset["rag_doc_max_chars"],
                minimum=1,
            ),
            rag_context_max_chars=_env_strict_int(
                "LOCAL_AGENT_RAG_CONTEXT_MAX_CHARS",
                preset["rag_context_max_chars"],
                minimum=1,
            ),
            orchestration_enabled=_env_strict_bool(
                "LOCAL_AGENT_ORCHESTRATION_ENABLED", True
            ),
            orchestration_max_agents=_env_strict_int(
                "LOCAL_AGENT_ORCHESTRATION_MAX_AGENTS", 3, minimum=1
            ),
            sync_enabled=_env_strict_bool("LOCAL_AGENT_SYNC_ENABLED", False),
            wiki_cookie=os.getenv("LOCAL_AGENT_WIKI_COOKIE", ""),
            local_knowledge_base_dir=os.getenv(
                "LOCAL_AGENT_LOCAL_KB_DIR",
                os.path.join(project_root, "data", "knowledge_base"),
            ),
            knowledge_base_required=knowledge_base_required,
            environment_id=environment_id,
            service_version=service_version,
            blocking_max_workers=blocking_max_workers,
            blocking_max_pending_tasks=blocking_max_pending_tasks,
            event_channel_capacity=event_channel_capacity,
            planning_timeout_seconds=planning_timeout_seconds,
            step_result_per_result_chars=step_result_per_result_chars,
            step_result_run_total_chars=step_result_run_total_chars,
            step_result_max_entries=step_result_max_entries,
            tool_allowed_read_roots=tool_allowed_read_roots,
        )
