#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""固定用途 Docker sandbox worker；不是通用 shell 或 Python runner。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time


INPUT_ROOT = Path("/sandbox/input")
OUTPUT_ROOT = Path("/sandbox/output")
MAX_INPUT_PATH_BYTES = 256
MAX_OUTPUT_TEXT_BYTES = 4096
MAX_STRESS_BYTES = 65_536


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(16_384)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError
        result = _dispatch(payload)
        _emit({"status": "ok", "result": result})
        return 0
    except _PathDenied:
        _emit({"status": "error", "error_code": "SANDBOX_PATH_DENIED"})
        return 0
    except Exception:
        _emit({"status": "error", "error_code": "SANDBOX_WORKER_ERROR"})
        return 0


def _dispatch(payload: dict[str, object]) -> object:
    operation = payload.get("operation")
    if operation == "READ_INPUT":
        path = _contained_path(INPUT_ROOT, payload.get("input_path", "input.txt"))
        return path.read_text(encoding="utf-8")
    if operation == "WRITE_OUTPUT":
        text = payload.get("output_text", "")
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_OUTPUT_TEXT_BYTES:
            raise ValueError
        path = _contained_path(OUTPUT_ROOT, payload.get("output_path", "result.txt"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return {"written": True, "path": str(path.relative_to(OUTPUT_ROOT)).replace("\\", "/")}
    if operation == "SLEEP":
        seconds = payload.get("sleep_seconds", 0)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 <= seconds <= 5:
            raise ValueError
        time.sleep(seconds)
        return {"slept_seconds": seconds}
    if operation == "NETWORK_PROBE":
        # Fixed target; user/provider data cannot select an egress destination.
        import socket

        try:
            with socket.create_connection(("example.com", 80), timeout=1.0):
                return {"network": "reachable"}
        except OSError:
            return {"network": "blocked"}
    if operation == "ENV_PROBE":
        return {
            "synthetic_secret_present": any(
                key.startswith("WP12_SYNTHETIC_SECRET_")
                or value.startswith("WP12_SYNTHETIC_SECRET_")
                for key, value in os.environ.items()
            )
        }
    if operation == "CHILD_PROCESS":
        child = subprocess.Popen(
            [sys.executable, __file__, "--fixed-child"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        child.wait()
        return {"child": "completed"}
    if operation == "OUTPUT_STRESS":
        size = payload.get("output_size", MAX_STRESS_BYTES)
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= MAX_STRESS_BYTES:
            raise ValueError
        return "x" * size
    raise ValueError


class _PathDenied(ValueError):
    pass


def _contained_path(root: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise _PathDenied
    if len(relative.encode("utf-8")) > MAX_INPUT_PATH_BYTES:
        raise _PathDenied
    normalized = relative.replace("\\", "/")
    if normalized.startswith("/") or normalized in {".", ".."}:
        raise _PathDenied
    candidate = (root / normalized).resolve()
    root_resolved = root.resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        raise _PathDenied from None
    if candidate.is_symlink():
        raise _PathDenied
    return candidate


def _emit(envelope: dict[str, object]) -> None:
    encoded = json.dumps(envelope, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    sys.stdout.write(encoded)
    sys.stdout.flush()


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--fixed-child":
        time.sleep(0.05)
        raise SystemExit(0)
    raise SystemExit(main())
