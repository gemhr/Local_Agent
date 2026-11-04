"""标准 Skill 的两个窄只读工具，执行和提交均由原 Tool Runtime 拥有。"""
import json
from hashlib import sha256
from pathlib import Path
from core.runtime.tool_adapters import ToolAdapter, ToolAdapterInvocationError, ToolAdapterResponse
from core.runtime.tool_contract import ToolExecutionSpec, ToolSideEffectKind, ToolErrorCategory, ToolInvocation
from core.runtime.retry import OperationIdempotency
from core.runtime.tool_registry import ToolDescriptor, ToolRegistration

TOOL_NAMES = ("load_skill", "read_skill_resource")


class SkillTool(ToolAdapter):
    def __init__(self, name, catalog):
        self.catalog = catalog
        self.handler_binding_id = "skills." + name + ".v1"
        self.spec = ToolExecutionSpec(tool_name=name, side_effect_kind=ToolSideEffectKind.NONE,
            idempotency=OperationIdempotency.READ_ONLY, supports_cooperative_cancellation=True,
            default_timeout_seconds=10, max_output_bytes=65536)

    def llm_input_schema(self):
        keys = ["name", "relative_path"] if self.spec.tool_name == "read_skill_resource" else ["name"]
        return dict(type="object", properties={k: {"type": "string"} for k in keys}, required=keys, additionalProperties=False)

    def build_invocation(self, argument_text):
        try:
            args = json.loads(argument_text)
            if not isinstance(args, dict) or set(args) != set(self.llm_input_schema()["required"]) or any(not isinstance(v, str) or not v for v in args.values()):
                raise ValueError()
        except (ValueError, TypeError):
            raise ToolAdapterInvocationError(category=ToolErrorCategory.VALIDATION,
                safe_error_code="SKILL_INPUT_INVALID", safe_message="Skill 工具参数无效") from None
        return ToolInvocation.create(tool_name=self.spec.tool_name, arguments=args)

    def invoke_once(self, invocation, context):
        context.raise_if_cancelled()
        run = context.run_context
        name = invocation.arguments["name"]
        identity = getattr(run, "loop_skill_identity", {})
        try:
            if name not in identity:
                raise ValueError("SKILL_NOT_VISIBLE")
            skill = self.catalog.get(name)
            relative = invocation.arguments.get("relative_path", "SKILL.md")
            path = (skill.package_path / relative).resolve()
            if Path(relative).is_absolute() or not path.is_relative_to(skill.package_path):
                raise ValueError("SKILL_RESOURCE_DENIED")
            key = path.relative_to(skill.package_path).as_posix()
            expected = identity[name]["files"].get(key)
            if expected is None:
                raise ValueError("SKILL_RESOURCE_DENIED")
            raw = path.read_bytes()
            if sha256(raw).hexdigest() != expected:
                raise ValueError("SKILL_CONTENT_CHANGED")
            content = raw.decode("utf-8")
        except (OSError, UnicodeError, ValueError):
            raise ToolAdapterInvocationError(category=ToolErrorCategory.PERMISSION_DENIED,
                safe_error_code="SKILL_READ_DENIED", safe_message="Skill 不可见、资源不允许或冻结内容已变化") from None
        context.raise_if_cancelled()
        result = dict(name=name, relative_path=key, sha256=expected, content=content,
            frame_id=run.loop_frame_id, agent_id=run.loop_skill_agent_id)
        return ToolAdapterResponse(content=json.dumps(result, ensure_ascii=False), content_type="application/json", safe_summary="已读取冻结 Skill 资源")


def register_tools(registry, catalog):
    for name, description in zip(TOOL_NAMES, ("读取当前 Frame 可见 Skill 的完整 SKILL.md 方法", "读取当前 Frame 可见 Skill 的 references Markdown 资源")):
        registry.register(ToolRegistration(ToolDescriptor(name=name, description=description), SkillTool(name, catalog)))


def register_policies(catalog, tool_grants):
    from core.runtime.tool_governance import ToolPolicy
    for name in TOOL_NAMES:
        catalog.register(ToolPolicy(tool_name=name, allowed_agent_ids=tool_grants.get(name, frozenset())))
