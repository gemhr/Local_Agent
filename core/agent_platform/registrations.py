"""应用启动时提交的完整业务注册清单。"""

from core.agent_platform.registry import AgentRegistrationBundle
from core.agent_platform.contracts import AgentRegistration, WorkflowDefinition, WorkflowTask
from core.agent_platform.ci_diagnosis import definition
from core.agent_platform.baseband_investigator import definition as baseband_definition
from core.ci_simulation.baseband import AGENT_ID as BASEBAND_AGENT
from core.agent_platform.ci_diagnosis_tools import TOOL_NAMES
from core.incidents.service import AGENT_ID
from core.agent_platform.ci_status import definition as status_definition, AGENT_ID as STATUS_AGENT, TOOL_NAME as STATUS_TOOL
from core.agent_platform.workflow_artifacts import CI_ARTIFACT_BINDING
from core.feature_impact.agent import definition as feature_definition
from core.feature_impact.tools import TOOL_NAMES as FEATURE_TOOLS, AGENT_ID as FEATURE_AGENT


# 新 Agent、Workflow、Tool 和符号 Provider 授权都在同一个 startup bundle
# 中声明；内置 Agent 仍由平台 seed 提供。
BUSINESS_REGISTRATION_BUNDLE = AgentRegistrationBundle(
    registrations=(AgentRegistration(definition()), AgentRegistration(baseband_definition()), AgentRegistration(feature_definition()), AgentRegistration(status_definition())),
    workflows=(WorkflowDefinition(
        "runtime_handoff", "1", execution_mode="SERIAL", tasks=(
            WorkflowTask("external", "core_router", "请仅回复 OpenCode 检查完成。",
                         agent_version="builtin-1", runtime_kind="OPENCODE", runtime_instance_id="workflow-opencode"),
            WorkflowTask("native", "core_router", "请仅回复 Native 检查完成。",
                         agent_version="builtin-1", runtime_kind="NATIVE"),
        ),
    ), WorkflowDefinition(
        "ci_artifact_handoff", "1", execution_mode="SERIAL", result_binding=CI_ARTIFACT_BINDING, tasks=(
            WorkflowTask("external", "core_router", "在指定路径写入 CI 执行引用文件，再回复约定的 JSON 对象。",
                         agent_version="builtin-1", runtime_kind="OPENCODE", runtime_instance_id="workflow-opencode",
                         requires_result_validation=True),
            WorkflowTask("native", STATUS_AGENT, "查询由交接结果绑定的 CI 执行状态。",
                         agent_version=status_definition().agent_version, runtime_kind="NATIVE"),
        ),
    )),
    tools=(),
    model_profile_ids=frozenset({"default"}),
    retrieval_profile_ids=frozenset(),
    memory_profile_ids=frozenset(),
    tool_grants={**{name: frozenset({AGENT_ID, FEATURE_AGENT}) for name in ("load_skill", "read_skill_resource")}, **{name: frozenset({AGENT_ID, BASEBAND_AGENT}) for name in TOOL_NAMES},
        **{name: frozenset({FEATURE_AGENT}) for name in FEATURE_TOOLS}, STATUS_TOOL: frozenset({STATUS_AGENT})},
)


__all__ = ["BUSINESS_REGISTRATION_BUNDLE"]
