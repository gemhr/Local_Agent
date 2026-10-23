"""使用现有 ToolAdapter/bridge，可信 Run 绑定任务，模型只提供查询或来源。"""
import json
from core.agent_platform.ci_diagnosis_tools import strict_object
from core.runtime.tool_adapters import ToolAdapter, ToolAdapterInvocationError, ToolAdapterResponse
from core.runtime.tool_contract import ToolExecutionSpec, ToolSideEffectKind, ToolErrorCategory, ToolInvocation
from core.runtime.retry import OperationIdempotency
from core.runtime.tool_registry import ToolDescriptor, ToolRegistration
from core.runtime.tool_governance import ToolPolicy

AGENT_ID = "feature_impact_investigator"
TOOL_NAMES = ("search_feature_history", "read_feature_evidence")


class FeatureEvidenceTool(ToolAdapter):
    def __init__(self, name, service, bridge):
        self.service, self.bridge = service, bridge
        self.handler_binding_id = "feature_impact." + name + ".v1"
        self.spec = ToolExecutionSpec(tool_name=name, side_effect_kind=ToolSideEffectKind.NONE,
            idempotency=OperationIdempotency.READ_ONLY, supports_cooperative_cancellation=True,
            default_timeout_seconds=10, max_output_bytes=16384)

    def llm_input_schema(self):
        key = "query" if self.spec.tool_name == TOOL_NAMES[0] else "source_ref"
        return dict(type="object", properties={key: dict(type="string", maxLength=300)}, required=[key], additionalProperties=False)

    def build_invocation(self, argument_text):
        key = "query" if self.spec.tool_name == TOOL_NAMES[0] else "source_ref"
        try:
            value = strict_object(argument_text)
            if not isinstance(value, dict) or set(value) != {key} or not isinstance(value[key], str) or len(value[key]) > 300:
                raise ValueError()
        except (ValueError, TypeError):
            raise ToolAdapterInvocationError(category=ToolErrorCategory.VALIDATION, safe_error_code="FEATURE_TOOL_INPUT_INVALID", safe_message="工具参数无效") from None
        return ToolInvocation.create(tool_name=self.spec.tool_name, arguments=value)

    def invoke_once(self, invocation, context):
        context.raise_if_cancelled()
        try:
            result = self.bridge.run(lambda: self.service.bound_evidence(context.run_context, invocation.arguments),
                operation=self.spec.tool_name, timeout_seconds=min(10, context.remaining_seconds()))
        except ValueError:
            raise ToolAdapterInvocationError(category=ToolErrorCategory.PERMISSION_DENIED, safe_error_code="FEATURE_EVIDENCE_SCOPE_DENIED", safe_message="资料不在当前任务授权范围") from None
        context.raise_if_cancelled()
        return ToolAdapterResponse(content=json.dumps(result, ensure_ascii=False, allow_nan=False), content_type="application/json", safe_summary="已查询冻结公开资料")


def register_tools(registry, service, bridge):
    for name, description in zip(TOOL_NAMES, ("搜索当前冻结任务的公开历史资料、用例和依赖", "精确读取当前冻结任务的公开证据版本")):
        registry.register(ToolRegistration(ToolDescriptor(name=name, description=description), FeatureEvidenceTool(name, service, bridge)))


def register_policies(catalog):
    for name in TOOL_NAMES:
        catalog.register(ToolPolicy(tool_name=name, allowed_agent_ids=frozenset({AGENT_ID})))
