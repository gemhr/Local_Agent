"""应用启动时提交的完整业务注册清单。"""

from core.agent_platform.registry import AgentRegistrationBundle
from core.agent_platform.contracts import AgentRegistration
from core.agent_platform.ci_diagnosis import definition
from core.agent_platform.ci_diagnosis_tools import TOOL_NAMES
from core.incidents.service import AGENT_ID
from core.feature_impact.agent import definition as feature_definition
from core.feature_impact.tools import TOOL_NAMES as FEATURE_TOOLS, AGENT_ID as FEATURE_AGENT


# 新 Agent、Workflow、Tool 和符号 Provider 授权都在同一个 startup bundle
# 中声明；内置 Agent 仍由平台 seed 提供。
BUSINESS_REGISTRATION_BUNDLE = AgentRegistrationBundle(
    registrations=(AgentRegistration(definition()), AgentRegistration(feature_definition())),
    workflows=(),
    tools=(),
    model_profile_ids=frozenset({"default"}),
    retrieval_profile_ids=frozenset(),
    memory_profile_ids=frozenset(),
    tool_grants={**{name: frozenset({AGENT_ID}) for name in TOOL_NAMES},
        **{name: frozenset({FEATURE_AGENT}) for name in FEATURE_TOOLS}},
)


__all__ = ["BUSINESS_REGISTRATION_BUNDLE"]
