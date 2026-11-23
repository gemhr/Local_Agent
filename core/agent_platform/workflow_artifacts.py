"""合成 CI Workflow 的固定结果、文件产物与 Native 输入合同。"""

import hashlib
import json
import os
from pathlib import Path
import stat
from uuid import UUID

from sqlalchemy import select

from core.agent_platform.ci_diagnosis_tools import strict_object
from core.agent_platform.ci_status import INPUT_SCHEMA, OUTPUT_SCHEMA
from core.persistence.models import ExternalRunRow, WorkflowStepRunRow
from core.runtime.business_output_schema import schema_matches
from core.runtime.run_control import WorkflowArtifactAdmissionRejected


CI_ARTIFACT_BINDING = "ci-artifact.v1"
_ARTIFACT_PATH = "workflow-runs/{workflow_run_id}/ci-execution.json"
_MAX_BYTES = 16_384
_OUTPUT_VERSION = "ci-handoff.v1"
_FILE_VERSION = "ci-execution-ref.v1"
_TASK_TYPE = "CI_EXECUTION_STATUS"
_PROMPT = "\n只在当前工作区内新建相对文件 {path}，内容为 {artifact}。完成写入后仅回复 JSON 对象 {output}。"
_OUTPUT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"schema_version": {"type": "string", "enum": [_OUTPUT_VERSION]},
                   "incident_id": {"type": "string"}, "artifact_path": {"type": "string"}},
    "required": ["schema_version", "incident_id", "artifact_path"],
}
_FILE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"schema_version": {"type": "string", "enum": [_FILE_VERSION]}, "execution_id": {"type": "string"}},
    "required": ["schema_version", "execution_id"],
}
CI_ARTIFACT_CONTRACT = {
    "binding": CI_ARTIFACT_BINDING, "output_schema": _OUTPUT_SCHEMA, "artifact_schema": _FILE_SCHEMA,
    "artifact_path": _ARTIFACT_PATH, "max_bytes": _MAX_BYTES,
    "native_input_schema": INPUT_SCHEMA, "native_output_schema": OUTPUT_SCHEMA,
    "task_type": _TASK_TYPE, "native_mode": "FAST_PATH", "prompt_template": _PROMPT,
}


class WorkflowArtifactError(ValueError):
    def __init__(self, code: str, *, retryable=False):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


def _uuid(value, code):
    try:
        parsed = str(UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise WorkflowArtifactError(code) from None
    if value != parsed:
        raise WorkflowArtifactError(code)
    return parsed


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _parse(value, schema, error_code):
    try:
        size = len(value.encode("utf-8")) if isinstance(value, str) and len(value) <= _MAX_BYTES else len(value)
        if size > _MAX_BYTES:
            raise ValueError
        text = value.decode("utf-8", errors="strict") if isinstance(value, bytes) else value
        result = strict_object(text)
    except (TypeError, ValueError, RecursionError):
        raise WorkflowArtifactError(error_code) from None
    if not schema_matches(schema, result):
        raise WorkflowArtifactError(error_code)
    return result


def initial_query(incident_id, execution_id):
    return {"incident_id": _uuid(incident_id, "WORKFLOW_CI_INPUT_INVALID"),
            "execution_id": _uuid(execution_id, "WORKFLOW_CI_INPUT_INVALID"), "task_type": _TASK_TYPE}


def artifact_path(workflow_run_id):
    return _ARTIFACT_PATH.format(workflow_run_id=_uuid(workflow_run_id, "WORKFLOW_ARTIFACT_PATH_INVALID"))


def producer_input(instruction, run):
    path = artifact_path(run.workflow_run_id)
    return instruction + _PROMPT.format(
        path=path, artifact=_dump({"schema_version": _FILE_VERSION, "execution_id": run.handoff_input["execution_id"]}),
        output=_dump({"schema_version": _OUTPUT_VERSION, "incident_id": run.handoff_input["incident_id"], "artifact_path": path}),
    )


def _lstat(path, mode):
    metadata = path.lstat()
    if not mode(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise WorkflowArtifactError("WORKFLOW_ARTIFACT_PATH_INVALID")
    return metadata


def _workspace_file(workspace, run_id):
    root = Path(workspace)
    if not root.is_absolute() or root.drive.startswith("\\\\") or ".." in root.parts:
        raise WorkflowArtifactError("WORKFLOW_ARTIFACT_PATH_INVALID")
    for ancestor in (*reversed(root.parents), root):
        _lstat(ancestor, stat.S_ISDIR)
    return root / artifact_path(run_id)


def _directory_identity(parent):
    info = _lstat(parent, stat.S_ISDIR)
    return [info.st_dev, info.st_ino]


def prepare_output(workspace, run_id, *, first, expected_directory):
    """首次接纳前原子创建专属目录。续领只接受原目录和仍空缺的产物位置。"""
    try:
        path = _workspace_file(workspace, run_id)
        parent = path.parent
        if first:
            parent.parent.mkdir(exist_ok=True)
            _lstat(parent.parent, stat.S_ISDIR)
            parent.mkdir()
        _lstat(parent.parent, stat.S_ISDIR)
        directory = _directory_identity(parent)
        if not first and directory != expected_directory:
            raise WorkflowArtifactError("WORKFLOW_ARTIFACT_CHANGED")
        try:
            path.lstat()
        except FileNotFoundError:
            return directory
        raise WorkflowArtifactError("WORKFLOW_ARTIFACT_PREEXISTS")
    except FileExistsError:
        raise WorkflowArtifactError("WORKFLOW_ARTIFACT_PREEXISTS") from None
    except OSError:
        raise WorkflowArtifactError("WORKFLOW_ARTIFACT_UNAVAILABLE", retryable=True) from None


def _read(workspace, run_id, expected_directory):
    try:
        path = _workspace_file(workspace, run_id)
        _lstat(path.parent.parent, stat.S_ISDIR)
        if _directory_identity(path.parent) != expected_directory:
            raise WorkflowArtifactError("WORKFLOW_ARTIFACT_CHANGED")
        before = _lstat(path, stat.S_ISREG)
        if before.st_nlink != 1 or not 0 < before.st_size <= _MAX_BYTES:
            raise WorkflowArtifactError("WORKFLOW_ARTIFACT_FILE_INVALID")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not os.path.samestat(before, opened):
                raise WorkflowArtifactError("WORKFLOW_ARTIFACT_CHANGED")
            raw = stream.read(_MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (len(raw) != before.st_size or after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns
                or not os.path.samestat(before, _lstat(path, stat.S_ISREG))):
            raise WorkflowArtifactError("WORKFLOW_ARTIFACT_CHANGED")
    except OSError:
        raise WorkflowArtifactError("WORKFLOW_ARTIFACT_UNAVAILABLE", retryable=True) from None
    return raw, [before.st_dev, before.st_ino, before.st_mtime_ns]


def _artifact_binding(run, producer):
    return {
        "producer_run_id": producer.platform_run_id, "step_id": producer.step_id, "attempt": producer.attempt,
        "workflow_run_id": run.workflow_run_id, "runtime_instance_id": producer.runtime_instance_id,
        "workspace_target_digest": producer.runtime_target_digest, "relative_path": artifact_path(run.workflow_run_id),
    }


def validate_output(workspace, run, producer, output):
    value = _parse(output, _OUTPUT_SCHEMA, "WORKFLOW_OUTPUT_INVALID")
    incident_id = _uuid(value["incident_id"], "WORKFLOW_OUTPUT_INVALID")
    if incident_id != run.handoff_input["incident_id"]:
        raise WorkflowArtifactError("WORKFLOW_CI_BINDING_MISMATCH")
    binding = _artifact_binding(run, producer)
    if value["artifact_path"] != binding["relative_path"]:
        raise WorkflowArtifactError("WORKFLOW_ARTIFACT_PATH_INVALID")
    raw, file_identity = _read(workspace, run.workflow_run_id, producer.artifact_directory)
    artifact = _parse(raw, _FILE_SCHEMA, "WORKFLOW_ARTIFACT_JSON_INVALID")
    execution_id = _uuid(artifact["execution_id"], "WORKFLOW_ARTIFACT_JSON_INVALID")
    if execution_id != run.handoff_input["execution_id"]:
        raise WorkflowArtifactError("WORKFLOW_CI_BINDING_MISMATCH")
    return ({**binding, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "file_identity": file_identity},
            initial_query(incident_id, execution_id))


def freeze_native_input(query):
    return _dump(query)


def verify_frozen_input(workspace, run, producer, native_input):
    artifact_ref = producer.artifact_ref
    binding = _artifact_binding(run, producer)
    if not artifact_ref or any(artifact_ref.get(key) != value for key, value in binding.items()):
        raise WorkflowArtifactError("WORKFLOW_ARTIFACT_BINDING_INVALID")
    raw, file_identity = _read(workspace, run.workflow_run_id, producer.artifact_directory)
    if (len(raw) != artifact_ref["size"] or hashlib.sha256(raw).hexdigest() != artifact_ref["sha256"]
            or file_identity != artifact_ref["file_identity"]):
        raise WorkflowArtifactError("WORKFLOW_ARTIFACT_CHANGED")
    value = _parse(raw, _FILE_SCHEMA, "WORKFLOW_ARTIFACT_JSON_INVALID")
    query = _parse(native_input, INPUT_SCHEMA, "WORKFLOW_BOUND_INPUT_INVALID")
    from_file = initial_query(query["incident_id"], value["execution_id"])
    if _dump(from_file) != native_input or query != run.handoff_input:
        raise WorkflowArtifactError("WORKFLOW_BOUND_INPUT_INVALID")


async def verify_native_admission(session, run, native_step, native_input, *, runtime_instances):
    """在首次接纳已持有的 Native Run 锁内，从持久记录解析原产物。"""
    producer = await session.scalar(select(WorkflowStepRunRow).where(
        WorkflowStepRunRow.workflow_run_id == native_step.workflow_run_id, WorkflowStepRunRow.position == 0,
    ))
    binding_error = "WORKFLOW_ARTIFACT_BINDING_INVALID"
    if (producer is None or producer.runtime_kind != "OPENCODE" or producer.status != "SUCCEEDED"
            or producer.runtime_status != "SUCCEEDED" or producer.platform_run_id is None):
        raise WorkflowArtifactAdmissionRejected(binding_error)
    external = await session.get(ExternalRunRow, producer.platform_run_id)
    if (external is None or external.terminal_status != "SUCCEEDED" or external.error_code is not None
            or external.instance_id != producer.runtime_instance_id or external.agent_id != producer.agent_id
            or external.agent_version != producer.agent_version or external.tenant_id != run.tenant_id
            or external.user_id != str(run.owner_user_id)
            or external.session_id != f"{run.workflow_run_id}:{producer.step_id}"
            or not all((external.external_session_id, external.user_message_id, external.assistant_message_id))
            or producer.result_ref != {
                "kind": "OPENCODE_MESSAGE", "instance_id": external.instance_id, "session_id": external.external_session_id,
                "user_message_id": external.user_message_id, "message_id": external.assistant_message_id,
            }):
        raise WorkflowArtifactAdmissionRejected(binding_error)
    adapter = runtime_instances.get(external.instance_id)
    if (adapter is None or adapter.target_digest != external.target_digest
            or adapter.workflow_target_digest != producer.runtime_target_digest):
        raise WorkflowArtifactAdmissionRejected(binding_error)
    from core.agent_platform.runtime_adapter import RuntimeAdapterError

    try:
        workspace = adapter.workflow_artifact_workspace()
        verify_frozen_input(workspace, run, producer, native_input)
    except WorkflowArtifactError as exc:
        raise WorkflowArtifactAdmissionRejected(exc.code) from None
    except RuntimeAdapterError as exc:
        raise WorkflowArtifactAdmissionRejected(exc.error_code) from None


def verify_native_output(native_input, output):
    query = _parse(native_input, INPUT_SCHEMA, "WORKFLOW_BOUND_INPUT_INVALID")
    result = _parse(output, OUTPUT_SCHEMA, "WORKFLOW_NATIVE_OUTPUT_INVALID")
    if any(result[key] != query[key] for key in ("incident_id", "execution_id")):
        raise WorkflowArtifactError("WORKFLOW_NATIVE_OUTPUT_INVALID")
