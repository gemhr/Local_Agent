"""应用启动时提交的完整业务注册清单。"""

from core.agent_platform.registry import AgentRegistrationBundle


# 新 Agent、Workflow、Tool 和符号 Provider 授权都在同一个 startup bundle
# 中声明；内置 Agent 仍由平台 seed 提供。
BUSINESS_REGISTRATION_BUNDLE = AgentRegistrationBundle(
    registrations=(),
    workflows=(),
    tools=(),
    model_profile_ids=frozenset({"default"}),
    retrieval_profile_ids=frozenset(),
    memory_profile_ids=frozenset(),
    tool_grants={},
)


__all__ = ["BUSINESS_REGISTRATION_BUNDLE"]
