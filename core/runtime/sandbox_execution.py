#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tool execution backend seam and the fixed Docker sandbox backend.

The backend owns containment only. Governance, approval, retry, idempotency and
the public ToolExecutionResult contract remain in the existing Runtime.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Awaitable, Callable, Protocol
from uuid import uuid4

from core.runtime.cancellation import RunCancelledError
from core.runtime.tool_adapters import (
    ToolAdapter,
    ToolAdapterInvocationError,
    ToolAdapterResponse,
)
from core.runtime.tool_contract import (
    SandboxExecutionMode,
    SandboxExecutionPolicy,
    SandboxNetworkMode,
    ToolErrorCategory,
    ToolExecutionPhase,
    ToolExecutionSpec,
    ToolExecutionStatus,
    ToolInvocation,
    ToolSideEffectState,
)


class ToolExecutionBackend(Protocol):
    async def execute(
        self,
        *,
        adapter: ToolAdapter,
        invocation: ToolInvocation,
        spec: ToolExecutionSpec,
        execution_context: object,
        invoke_in_process: Callable[[], Awaitable[ToolAdapterResponse]],
    ) -> ToolAdapterResponse:
        """执行一次 attempt；backend 内部拥有其生命周期。"""


class TrustedInProcessExecutionBackend:
    """现有 adapter.invoke_once 行为的统一 backend 包装。"""

    async def execute(
        self,
        *,
        adapter: ToolAdapter,
        invocation: ToolInvocation,
        spec: ToolExecutionSpec,
        execution_context: object,
        invoke_in_process: Callable[[], Awaitable[ToolAdapterResponse]],
    ) -> ToolAdapterResponse:
        return await invoke_in_process()


class ToolExecutionBackendResolver:
    """根据 immutable ToolExecutionSpec 选择唯一 backend。"""

    def __init__(
        self,
        *,
        trusted_backend: ToolExecutionBackend | None = None,
        isolated_backend: ToolExecutionBackend | None = None,
    ) -> None:
        self.trusted_backend = trusted_backend or TrustedInProcessExecutionBackend()
        self.isolated_backend = isolated_backend or DockerIsolatedExecutionBackend()

    def resolve(self, spec: ToolExecutionSpec) -> ToolExecutionBackend:
        policy = spec.sandbox_policy
        if not isinstance(policy, SandboxExecutionPolicy):
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.VALIDATION,
                safe_error_code="SANDBOX_POLICY_INVALID",
                safe_message="Sandbox execution policy 无效。",
                phase=ToolExecutionPhase.VALIDATION,
            )
        if policy.mode is SandboxExecutionMode.TRUSTED_IN_PROCESS:
            return self.trusted_backend
        if policy.mode is SandboxExecutionMode.ISOLATED:
            return self.isolated_backend
        raise ToolAdapterInvocationError(
            category=ToolErrorCategory.VALIDATION,
            safe_error_code="SANDBOX_POLICY_UNSUPPORTED",
            safe_message="Sandbox execution mode 不受支持。",
            phase=ToolExecutionPhase.VALIDATION,
        )


@dataclass(frozen=True, slots=True)
class _ContainerPaths:
    input_root: Path
    output_root: Path


class DockerIsolatedExecutionBackend:
    """固定 worker/image 的 Docker execution backend。

    目前只接受 ``sandbox_execution_demo`` 的 code-owned payload。这里不
    提供任意 command、Python callback、image 或 entrypoint 注入能力。
    """

    IMAGE = "ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58"
    WORKER_STAGING_DIRECTORY = ".localagent-runtime"
    WORKER_CONTAINER_PATH = "/sandbox/input/.localagent-runtime/worker.py"
    INPUT_CONTAINER_PATH = "/sandbox/input"
    OUTPUT_CONTAINER_PATH = "/sandbox/output"
    DOCKER_EXECUTABLE = "docker"
    _FIXED_RUNTIME_ENV = {"PYTHONUNBUFFERED": "1", "LC_ALL": "C.UTF-8"}

    def __init__(
        self,
        *,
        input_root: str | os.PathLike[str] | None = None,
        docker_executable: str = DOCKER_EXECUTABLE,
        worker_source: str | os.PathLike[str] | None = None,
        temp_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self._docker_executable = docker_executable
        source = Path(worker_source) if worker_source else Path(__file__).parents[2] / "tools" / "sandbox_worker.py"
        self._worker_source = source.resolve()
        if not self._worker_source.is_file():
            raise ValueError("sandbox worker source 不存在")
        self._input_root = Path(input_root).resolve() if input_root else None
        if self._input_root is not None and not self._input_root.is_dir():
            raise ValueError("sandbox input root 必须是目录")
        self._temp_root = Path(temp_root).resolve() if temp_root else None
        self._last_cleanup_status = "NOT_RUN"

    @property
    def last_cleanup_status(self) -> str:
        return self._last_cleanup_status

    async def execute(
        self,
        *,
        adapter: ToolAdapter,
        invocation: ToolInvocation,
        spec: ToolExecutionSpec,
        execution_context: object,
        invoke_in_process: Callable[[], Awaitable[ToolAdapterResponse]],
    ) -> ToolAdapterResponse:
        del invoke_in_process
        self._last_cleanup_status = "NOT_RUN"
        if invocation.tool_name != "sandbox_execution_demo" or not callable(
            getattr(adapter, "sandbox_payload", None)
        ):
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.VALIDATION,
                safe_error_code="SANDBOX_POLICY_UNSUPPORTED",
                safe_message="该 Tool 未声明受支持的固定 sandbox workload。",
                phase=ToolExecutionPhase.VALIDATION,
            )
        # The policy is taken from the validated immutable spec, never from the
        # invocation or provider metadata.
        policy = spec.sandbox_policy
        _validate_demo_policy(policy)
        if not isinstance(policy, SandboxExecutionPolicy):
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.VALIDATION,
                safe_error_code="SANDBOX_POLICY_INVALID",
                safe_message="Sandbox execution policy 无效。",
                phase=ToolExecutionPhase.VALIDATION,
            )
        paths: _ContainerPaths | None = None
        container_name = _container_name(invocation)
        process: asyncio.subprocess.Process | None = None
        try:
            payload = adapter.sandbox_payload(invocation)
            paths = self._prepare_paths(policy, container_name)
            command = self._build_command(policy, container_name, paths, payload)
            process = await self._start(command)
            await self._send_payload(process, payload)
            stdout_task = asyncio.create_task(
                _read_bounded(process.stdout, policy.output_bytes)
            )
            stderr_task = asyncio.create_task(
                _read_bounded(process.stderr, policy.output_bytes)
            )
            stdout, stderr = await self._wait_process(
                process,
                stdout_task,
                stderr_task,
                execution_context,
            )
            termination_verified = await self._verify_container_absent(container_name)
            _record_worker_termination(execution_context, termination_verified)
            if not termination_verified:
                raise ToolAdapterInvocationError(
                    category=ToolErrorCategory.INTERNAL,
                    safe_error_code="SANDBOX_CLEANUP_FAILED",
                    safe_message="Sandbox worker termination 无法验证。",
                    phase=ToolExecutionPhase.INVOCATION,
                    worker_terminated=False,
                )
            if process.returncode != 0:
                raise ToolAdapterInvocationError(
                    category=ToolErrorCategory.INTERNAL,
                    safe_error_code="SANDBOX_START_FAILED",
                    safe_message="Sandbox worker 启动或执行失败。",
                    phase=ToolExecutionPhase.INVOCATION,
                    side_effect_state=ToolSideEffectState.NOT_STARTED,
                    side_effect_state_authoritative=True,
                )
            return _decode_worker_response(stdout, stderr)
        except RunCancelledError:
            if process is not None and process.returncode is None:
                verified = await self._terminate_process(process, container_name)
                _record_worker_termination(execution_context, verified)
            raise
        except _SandboxCaptureLimit:
            verified = await self._terminate_if_running(process, container_name)
            _record_worker_termination(execution_context, verified)
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.OUTPUT_TOO_LARGE,
                safe_error_code="SANDBOX_OUTPUT_LIMIT",
                safe_message="Sandbox worker 输出超过限制。",
                phase=ToolExecutionPhase.OUTPUT,
                worker_terminated=verified,
            ) from None
        except asyncio.TimeoutError:
            verified = await self._terminate_if_running(process, container_name)
            _record_worker_termination(execution_context, verified)
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.TIMEOUT,
                safe_error_code="SANDBOX_TIMEOUT",
                safe_message="Sandbox worker 超时。",
                phase=ToolExecutionPhase.INVOCATION,
                side_effect_state=ToolSideEffectState.UNKNOWN,
                side_effect_state_authoritative=True,
                worker_terminated=verified,
            ) from None
        except FileNotFoundError:
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.INTERNAL,
                safe_error_code="SANDBOX_BACKEND_UNAVAILABLE",
                safe_message="Isolated sandbox backend 不可用。",
                phase=ToolExecutionPhase.INVOCATION,
            ) from None
        except ToolAdapterInvocationError:
            raise
        except Exception:
            verified = await self._terminate_if_running(process, container_name)
            _record_worker_termination(execution_context, verified)
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.INTERNAL,
                safe_error_code="SANDBOX_PROTOCOL_INVALID",
                safe_message="Sandbox worker 协议无效。",
                phase=ToolExecutionPhase.OUTPUT,
                worker_terminated=verified,
            ) from None
        finally:
            if process is not None and process.returncode is None:
                verified = await self._terminate_process(process, container_name)
                _record_worker_termination(execution_context, verified)
            if paths is not None:
                self._cleanup_paths(paths)

    def _prepare_paths(self, policy: SandboxExecutionPolicy, container_name: str) -> _ContainerPaths:
        base = Path(tempfile.mkdtemp(prefix=f"{container_name}-", dir=self._temp_root))
        try:
            input_dir = base / "input"
            output_dir = base / "output"
            input_dir.mkdir()
            output_dir.mkdir()
            if self._input_root is not None:
                _copy_input_tree(self._input_root, input_dir)
            worker_directory = input_dir / self.WORKER_STAGING_DIRECTORY
            if worker_directory.exists():
                raise ToolAdapterInvocationError(
                    category=ToolErrorCategory.PERMISSION_DENIED,
                    safe_error_code="SANDBOX_POLICY_INVALID",
                    safe_message="Sandbox input tree 使用了保留路径。",
                    phase=ToolExecutionPhase.VALIDATION,
                )
            worker_directory.mkdir()
            shutil.copyfile(self._worker_source, worker_directory / "worker.py")
            return _ContainerPaths(input_dir, output_dir)
        except BaseException:
            shutil.rmtree(base, ignore_errors=True)
            raise

    def _build_command(
        self,
        policy: SandboxExecutionPolicy,
        container_name: str,
        paths: _ContainerPaths,
        payload: dict[str, object],
    ) -> list[str]:
        command = [
            self._docker_executable,
            "run",
            "-i",
            "--rm",
            "--name",
            container_name,
            "--init",
            "--read-only",
            "--user",
            "65532:65532",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            str(policy.process_limit),
            "--memory",
            str(policy.memory_bytes),
            "--cpus",
            str(policy.cpu_units),
            "--network",
            "none" if policy.network_mode is SandboxNetworkMode.NO_NETWORK else "bridge",
            "-v",
            f"{paths.input_root}:{self.INPUT_CONTAINER_PATH}:ro",
            "-v",
            f"{paths.output_root}:{self.OUTPUT_CONTAINER_PATH}:rw",
        ]
        for key, value in self._FIXED_RUNTIME_ENV.items():
            command.extend(("-e", f"{key}={value}"))
        for key in policy.environment_allowlist:
            if key in policy.environment_values:
                command.extend(("-e", f"{key}={policy.environment_values[key]}"))
        command.extend((self.IMAGE, "python", self.WORKER_CONTAINER_PATH))
        return command

    async def _start(self, command: list[str]) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    @staticmethod
    async def _send_payload(
        process: asyncio.subprocess.Process, payload: dict[str, object]
    ) -> None:
        assert process.stdin is not None
        process.stdin.write(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        await process.stdin.drain()
        process.stdin.close()

    async def _wait_process(
        self,
        process: asyncio.subprocess.Process,
        stdout_task: asyncio.Task[bytes],
        stderr_task: asyncio.Task[bytes],
        execution_context: object,
    ) -> tuple[bytes, bytes]:
        context = execution_context
        remaining = context.remaining_seconds()
        if remaining <= 0:
            raise asyncio.TimeoutError
        process_task = asyncio.create_task(process.wait())
        capture_task = asyncio.ensure_future(asyncio.gather(stdout_task, stderr_task))
        cancellation_task = asyncio.create_task(
            context.attempt_cancellation_token.wait_cancelled()
        )
        run_cancellation_task = asyncio.create_task(
            context.run_context.cancellation_token.wait_cancelled()
        )
        try:
            deadline = asyncio.get_running_loop().time() + remaining
            while True:
                timeout = max(0.0, deadline - asyncio.get_running_loop().time())
                if timeout <= 0:
                    raise asyncio.TimeoutError
                done, _ = await asyncio.wait(
                    {
                        process_task,
                        capture_task,
                        cancellation_task,
                        run_cancellation_task,
                    },
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancellation_task in done or run_cancellation_task in done:
                    context.raise_if_cancelled()
                    context.run_context.raise_if_inactive()
                if capture_task in done:
                    stdout, stderr = capture_task.result()
                    if process_task in done:
                        return stdout, stderr
                    # The streams can close before the process exits; keep
                    # waiting for the fixed worker process.
                if process_task in done:
                    stdout, stderr = await capture_task
                    return stdout, stderr
        finally:
            cancellation_task.cancel()
            await asyncio.gather(cancellation_task, return_exceptions=True)
            run_cancellation_task.cancel()
            await asyncio.gather(run_cancellation_task, return_exceptions=True)
            if not process_task.done():
                process_task.cancel()
                await asyncio.gather(process_task, return_exceptions=True)
            if not capture_task.done():
                capture_task.cancel()
                await asyncio.gather(capture_task, return_exceptions=True)

    async def _terminate_process(
        self,
        process: asyncio.subprocess.Process,
        container_name: str,
    ) -> bool:
        try:
            rm = await asyncio.create_subprocess_exec(
                self._docker_executable,
                "rm",
                "-f",
                container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(rm.wait(), timeout=2.0)
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await asyncio.wait_for(process.wait(), timeout=1.0)
            return await self._verify_container_absent(container_name)
        except Exception:
            self._last_cleanup_status = "FAILED"
            return False

    async def _terminate_if_running(
        self,
        process: asyncio.subprocess.Process | None,
        container_name: str,
    ) -> bool:
        if process is None:
            return True
        if process.returncode is None:
            return await self._terminate_process(process, container_name)
        return await self._verify_container_absent(container_name)

    async def _verify_container_absent(self, container_name: str) -> bool:
        try:
            inspect_process = await asyncio.create_subprocess_exec(
                self._docker_executable,
                "inspect",
                container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            inspect_code = await asyncio.wait_for(inspect_process.wait(), timeout=2.0)
            verified = inspect_code != 0
            self._last_cleanup_status = "VERIFIED" if verified else "FAILED"
            return verified
        except Exception:
            self._last_cleanup_status = "FAILED"
            return False

    def _cleanup_paths(self, paths: _ContainerPaths) -> None:
        try:
            shutil.rmtree(paths.input_root.parent, ignore_errors=False)
        except OSError:
            self._last_cleanup_status = "FAILED"


class _SandboxCaptureLimit(RuntimeError):
    pass


def _record_worker_termination(execution_context: object, verified: bool) -> None:
    recorder = getattr(execution_context, "record_worker_termination", None)
    if callable(recorder):
        recorder(verified)


async def _read_bounded(stream: asyncio.StreamReader | None, limit: int) -> bytes:
    if stream is None:
        return b""
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await stream.read(min(4096, limit + 1))
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            raise _SandboxCaptureLimit
        chunks.append(chunk)


def _decode_worker_response(stdout: bytes, stderr: bytes) -> ToolAdapterResponse:
    del stderr
    try:
        envelope = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ToolAdapterInvocationError(
            category=ToolErrorCategory.OUTPUT_INVALID,
            safe_error_code="SANDBOX_PROTOCOL_INVALID",
            safe_message="Sandbox worker 协议无效。",
            phase=ToolExecutionPhase.OUTPUT,
        ) from None
    if not isinstance(envelope, dict) or envelope.get("status") != "ok":
        code = envelope.get("error_code") if isinstance(envelope, dict) else None
        safe_code = code if isinstance(code, str) and code.startswith("SANDBOX_") else "SANDBOX_PROTOCOL_INVALID"
        category = ToolErrorCategory.VALIDATION if safe_code == "SANDBOX_PATH_DENIED" else ToolErrorCategory.INTERNAL
        raise ToolAdapterInvocationError(
            category=category,
            safe_error_code=safe_code,
            safe_message="Sandbox worker 未完成固定操作。",
            phase=ToolExecutionPhase.INVOCATION,
        )
    result = envelope.get("result")
    if isinstance(result, str):
        content = result
        content_type = "text/plain"
    else:
        content = json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        content_type = "application/json"
    return ToolAdapterResponse(
        content=content,
        content_type=content_type,
        safe_summary="Sandbox demo Tool 已完成。",
        status=ToolExecutionStatus.SUCCEEDED,
        side_effect_state=ToolSideEffectState.NOT_STARTED,
        side_effect_state_authoritative=True,
    )


def _container_name(invocation: ToolInvocation) -> str:
    digest = hashlib.sha256(invocation.invocation_id.encode("utf-8")).hexdigest()[:20]
    return f"localagent-sbx-{digest}-{uuid4().hex[:8]}"


def _validate_demo_policy(policy: SandboxExecutionPolicy) -> None:
    capability_ids = {item.capability_id for item in policy.filesystem_capabilities}
    if capability_ids != {"demo-input", "demo-output"}:
        raise ToolAdapterInvocationError(
            category=ToolErrorCategory.VALIDATION,
            safe_error_code="SANDBOX_POLICY_INVALID",
            safe_message="Sandbox filesystem capability 不受支持。",
            phase=ToolExecutionPhase.VALIDATION,
        )
    paths = {item.capability_id: item.container_path for item in policy.filesystem_capabilities}
    if paths != {
        "demo-input": DockerIsolatedExecutionBackend.INPUT_CONTAINER_PATH,
        "demo-output": DockerIsolatedExecutionBackend.OUTPUT_CONTAINER_PATH,
    }:
        raise ToolAdapterInvocationError(
            category=ToolErrorCategory.VALIDATION,
            safe_error_code="SANDBOX_POLICY_INVALID",
            safe_message="Sandbox filesystem boundary 不受支持。",
            phase=ToolExecutionPhase.VALIDATION,
        )


def _copy_input_tree(source: Path, destination: Path) -> None:
    source = source.resolve()
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != source):
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.PERMISSION_DENIED,
                safe_error_code="SANDBOX_POLICY_INVALID",
                safe_message="Sandbox input tree 包含不受支持的链接。",
                phase=ToolExecutionPhase.VALIDATION,
            )
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not path.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


__all__ = [
    "DockerIsolatedExecutionBackend",
    "ToolExecutionBackend",
    "ToolExecutionBackendResolver",
    "TrustedInProcessExecutionBackend",
]
