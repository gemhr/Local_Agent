"""本地知识库文档读取与切片工具。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

@dataclass(frozen=True)
class LoadedDocument:
    """标准化后的文档对象。"""

    source: str
    content: str
    file_type: str


SUPPORTED_EXTENSIONS = {".md", ".txt", ".pdf", ".docx", ".xlsx", ".xls", ".csv"}


def _read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _read_pdf_file(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except Exception:
        return ""

    reader = PdfReader(str(path))
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    return "\n\n".join(part for part in pages if part)


def _read_docx_file(path: Path) -> str:
    try:
        from docx import Document
    except Exception:
        return ""

    document = Document(str(path))
    parts = [paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()]
    return "\n\n".join(parts)


def _read_excel_file(path: Path) -> str:
    try:
        import pandas as pd
    except Exception:
        return ""

    suffix = path.suffix.lower()
    if suffix == ".csv":
        sheets = {"csv": pd.read_csv(path)}
    else:
        sheets = pd.read_excel(path, sheet_name=None)

    outputs: list[str] = []
    for sheet_name, frame in sheets.items():
        sample = frame.fillna("").astype(str).head(100)
        lines = [f"[{sheet_name}] 列: {', '.join(sample.columns)}"]
        for _, row in sample.iterrows():
            lines.append(" | ".join(f"{col}={row[col]}" for col in sample.columns))
        outputs.append("\n".join(lines))
    return "\n\n".join(outputs)


def load_documents(base_dir: str) -> list[LoadedDocument]:
    """从目录中加载可支持格式的文档。"""
    root = Path(base_dir)
    if not root.exists():
        return []

    documents: list[LoadedDocument] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue

        if path.suffix.lower() in {".md", ".txt"}:
            text = _read_text_file(path)
        elif path.suffix.lower() == ".pdf":
            text = _read_pdf_file(path)
        elif path.suffix.lower() == ".docx":
            text = _read_docx_file(path)
        else:
            text = _read_excel_file(path)

        normalized = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        if not normalized:
            continue
        documents.append(
            LoadedDocument(
                source=str(path.relative_to(root)),
                content=normalized,
                file_type=path.suffix.lower().lstrip("."),
            )
        )
    return documents


def split_documents(
    documents: Iterable[LoadedDocument],
    *,
    chunk_size: int = 600,
    chunk_overlap: int = 100,
) -> list[dict]:
    """将文档切分为可向量化 chunk。"""
    chunks: list[dict] = []
    step = max(1, chunk_size - chunk_overlap)

    for document in documents:
        content = document.content
        for start in range(0, len(content), step):
            snippet = content[start : start + chunk_size].strip()
            if not snippet:
                continue
            chunk_index = len(chunks)
            chunks.append(
                {
                    "page_content": snippet,
                    "metadata": {
                        "source": document.source,
                        "file_type": document.file_type,
                        "chunk_id": f"{document.source}:{chunk_index}:{uuid.uuid4().hex[:8]}",
                        "offset": start,
                    },
                }
            )
            if start + chunk_size >= len(content):
                break
    return chunks


def ensure_test_documents(base_dir: str) -> list[str]:
    """写入一组可直接用于测试的知识库文档。"""
    root = Path(base_dir)
    root.mkdir(parents=True, exist_ok=True)
    created: list[str] = []

    md_path = root / "product_faq.md"
    md_path.write_text(
        """# 本地知识库产品 FAQ

## 计费策略
- 标准版按账号计费，月付 99 元。
- 企业版支持私有部署，按年签约。

## 数据安全
- 默认只使用本地向量数据库，不上传原文。
- 可配置审计日志保存 180 天。
""",
        encoding="utf-8",
    )
    created.append(str(md_path))

    txt_path = root / "ops_runbook.txt"
    txt_path.write_text(
        """故障排查步骤：
1. 检查 embedding 模型目录是否存在。
2. 检查 chroma_db 是否可写。
3. 如命中率低，增大 top_k 并重建索引。""",
        encoding="utf-8",
    )
    created.append(str(txt_path))

    try:
        import pandas as pd

        xlsx_path = root / "sla_matrix.xlsx"
        pd.DataFrame(
            [
                {"服务等级": "P1", "响应时效": "15分钟", "恢复目标": "2小时"},
                {"服务等级": "P2", "响应时效": "1小时", "恢复目标": "8小时"},
                {"服务等级": "P3", "响应时效": "4小时", "恢复目标": "24小时"},
            ]
        ).to_excel(xlsx_path, index=False)
        created.append(str(xlsx_path))
    except Exception:
        csv_path = root / "sla_matrix.csv"
        csv_path.write_text(
            "服务等级,响应时效,恢复目标\nP1,15分钟,2小时\nP2,1小时,8小时\nP3,4小时,24小时\n",
            encoding="utf-8",
        )
        created.append(str(csv_path))

    return created
