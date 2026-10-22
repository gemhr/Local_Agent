"""两个具有真实 Run 上下文的只读工具。"""
import json
from core.runtime.tool_adapters import ToolAdapter, ToolAdapterInvocationError, ToolAdapterResponse
from core.runtime.tool_contract import ToolExecutionSpec, ToolSideEffectKind, ToolErrorCategory, ToolInvocation
from core.runtime.retry import OperationIdempotency
from core.runtime.tool_registry import ToolDescriptor, ToolRegistration
from core.runtime.tool_governance import ToolPolicy
from core.incidents.contracts import CIError, EvidencePayload, ExecutionContext
from core.incidents.service import AGENT_ID

TOOL_NAMES = ("ci_get_execution_context", "ci_read_execution_evidence")


def strict_object(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("重复 JSON 字段")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("非法数字")))


class CIDiagnosisTool(ToolAdapter):
    def __init__(self, name, service, bridge):
        self.service, self.bridge = service, bridge
        self.handler_binding_id = "ci_diagnosis." + name + ".v1"
        self.spec = ToolExecutionSpec(tool_name=name, side_effect_kind=ToolSideEffectKind.NONE,
            idempotency=OperationIdempotency.READ_ONLY, supports_cooperative_cancellation=True,
            default_timeout_seconds=10, max_output_bytes=16384)

    def llm_input_schema(self):
        reading = self.spec.tool_name == TOOL_NAMES[1]
        return dict(type="object", properties={"evidence_id": {"type": "string"}} if reading else {},
            required=["evidence_id"] if reading else [], additionalProperties=False)

    def build_invocation(self, argument_text):
        try:
            value = strict_object(argument_text)
            expected = {"evidence_id"} if self.spec.tool_name == TOOL_NAMES[1] else set()
            if not isinstance(value, dict) or set(value) != expected or (expected and (not isinstance(value["evidence_id"], str) or not value["evidence_id"])):
                raise ValueError("参数错误")
        except (ValueError, TypeError):
            raise ToolAdapterInvocationError(category=ToolErrorCategory.VALIDATION,
                safe_error_code="CI_TOOL_INPUT_INVALID", safe_message="工具参数无效") from None
        return ToolInvocation.create(tool_name=self.spec.tool_name, arguments=value)

    def invoke_once(self, invocation, context):
        context.raise_if_cancelled()
        try:
            result = self.bridge.run(lambda: self.service.bound_execution(context.run_context, invocation.arguments.get("evidence_id")),
                operation=self.spec.tool_name, timeout_seconds=min(10, context.remaining_seconds()))
        except CIError:
            raise ToolAdapterInvocationError(category=ToolErrorCategory.PERMISSION_DENIED,
                safe_error_code="CI_EVIDENCE_SCOPE_DENIED", safe_message="取证范围不允许") from None
        context.raise_if_cancelled()
        result = (EvidencePayload if self.spec.tool_name == TOOL_NAMES[1] else ExecutionContext).model_validate(result).model_dump()
        return ToolAdapterResponse(content=json.dumps(result, ensure_ascii=False, allow_nan=False),
            content_type="application/json", safe_summary="已读取授权执行证据")


def register_tools(registry, service, bridge):
    for name, description in zip(TOOL_NAMES, ("查询当前调查执行的上下文与证据目录", "读取当前调查执行的单条原始证据")):
        registry.register(ToolRegistration(ToolDescriptor(name=name, description=description), CIDiagnosisTool(name, service, bridge)))


def register_policies(catalog):
    for name in TOOL_NAMES:
        catalog.register(ToolPolicy(tool_name=name, allowed_agent_ids=frozenset({AGENT_ID})))
