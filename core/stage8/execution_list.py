"""现有测试平台 execution-list.xls 的最小确定性构造器。"""

from __future__ import annotations

import hashlib
from html import escape
from pathlib import Path

from core.stage8.domain import GeneratedCaseArtifact


class ExecutionListBuilder:
    """只由 AgentCore 生成输出路径，不接受 caller path。"""

    def __init__(self, root: str | Path = "data/stage8/execution_lists"):
        self.root = Path(root)

    def build(
        self,
        mission_id: str,
        artifact: GeneratedCaseArtifact,
        execution_request_digest: str,
    ) -> str:
        safe_mission = _safe_component(mission_id)[:48]
        safe_artifact = _safe_component(artifact.artifact_id)[:48]
        identity = hashlib.sha256(
            f"{mission_id}\0{artifact.artifact_id}\0{execution_request_digest}".encode()
        ).hexdigest()[:16]
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{safe_mission}_{safe_artifact}_{identity}.xls"
        rows = (("Case Path", "Selected"), (artifact.case_path, "TRUE"))
        body = "".join(
            "<Row>" + "".join(f"<Cell><Data ss:Type=\"String\">{escape(value)}</Data></Cell>" for value in row) + "</Row>"
            for row in rows
        )
        content = (
            '<?xml version="1.0"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
            'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><Worksheet ss:Name="ExecutionList">'
            f"<Table>{body}</Table></Worksheet></Workbook>"
        )
        path.write_text(content, encoding="utf-8")
        return str(path.resolve())


def _safe_component(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in value)
    return safe or "unknown"
