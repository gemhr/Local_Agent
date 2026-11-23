"""工作台配置和任务提交的显式输入合同。"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120, pattern=r"^[^\x00]+$")]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
Instruction = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000, pattern=r"^[^\x00]+$")]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000, pattern=r"^[^\x00]*$")]
Revision = Annotated[int, Field(strict=True, ge=1)]


class WorkspaceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentConfig(WorkspaceInput):
    name: Name
    description: Description = ""
    base_agent_id: Identifier
    base_agent_version: Identifier
    instructions: Instruction


class AgentUpdate(AgentConfig):
    expected_revision: Revision


class WorkflowCopy(WorkspaceInput):
    template_id: Identifier
    template_version: Identifier
    name: Name


class WorkflowEdit(WorkspaceInput):
    expected_revision: Revision
    name: Name
    description: Description = ""
    runtime_instance_id: Identifier | None = None
    external_instruction: Instruction | None = None
    native_instruction: Instruction | None = None


class WorkflowPublish(WorkspaceInput):
    expected_revision: Revision


class AgentTask(WorkspaceInput):
    kind: Literal["AGENT"]
    request_id: UUID
    config_id: UUID | None = None
    expected_revision: Revision | None = None
    agent_id: Identifier | None = None
    agent_version: Identifier | None = None
    runtime_kind: Literal["NATIVE", "OPENCODE"]
    runtime_instance_id: Identifier | None = None
    input: Instruction
    timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=120)] | None = None

    @model_validator(mode="after")
    def validate_target(self):
        if self.config_id is not None:
            if self.agent_id is not None or self.agent_version is not None or self.expected_revision is None:
                raise ValueError("保存的 Agent 要求精确修订号，不能同时选择注册 Agent")
            if self.runtime_kind != "NATIVE":
                raise ValueError("保存的 Agent 仅支持 Native")
        elif self.agent_id is None or self.agent_version is None or self.expected_revision is not None:
            raise ValueError("必须指定注册 Agent 与版本")
        if (self.runtime_kind == "OPENCODE") != (self.runtime_instance_id is not None):
            raise ValueError("仅 OpenCode 需要受信实例 ID")
        return self


class WorkflowTaskLaunch(WorkspaceInput):
    kind: Literal["WORKFLOW"]
    request_id: UUID
    workflow_id: Identifier
    version: Identifier
    incident_id: UUID | None = None
    execution_id: UUID | None = None

    @model_validator(mode="after")
    def validate_ci_ids(self):
        if (self.incident_id is None) != (self.execution_id is None):
            raise ValueError("CI 任务须同时提供 incident_id 和 execution_id")
        return self


TaskLaunch = Annotated[AgentTask | WorkflowTaskLaunch, Field(discriminator="kind")]
