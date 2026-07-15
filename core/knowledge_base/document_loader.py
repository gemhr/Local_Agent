"""本地知识库文档读取与结构化切片工具。"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


@dataclass(frozen=True)
class LoadedDocument:
    """标准化后的文档解析单元。

    一个源文件可以产生多个 LoadedDocument，例如：
    - PDF：每页一个 LoadedDocument
    - Excel：每个工作表一个 LoadedDocument
    """

    source: str
    content: str
    file_type: str
    metadata: dict[str, Any] = field(default_factory=dict)


SUPPORTED_EXTENSIONS = {
    ".md",
    ".txt",
    ".rst",
    ".html",
    ".htm",
    ".pdf",
    ".docx",
    ".xlsx",
    ".xls",
    ".csv",
}

EXCLUDED_DIRECTORY_NAMES = {
    "00_metadata",
    ".git",
    "__pycache__",
    "chroma_db",
    "vector_store",
    ".venv",
    "venv",
}

SOURCE_GROUP_MAPPING = {
    "01_tech_docs": "tech_docs",
    "02_standards": "standards",
    "03_papers": "papers",
    "04_tables": "tables",
    "05_company_simulation": "company_simulation",
}

SCHEMA_VERSION = "kb_chunk_schema_v2"
CHUNKER_NAME = "structure_aware_splitter"
CHUNKER_VERSION = "v2"


def _normalize_text(text: str) -> str:
    """规范文本，但保留代码缩进、Markdown 结构和段落边界。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # 只删除行尾空白，不删除行首缩进。
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)

    # 连续三个以上空行压缩为两个换行。
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _read_text_file(path: Path) -> str:
    """读取常见文本编码。"""
    encodings = ("utf-8-sig", "utf-8", "gb18030")

    for encoding in encodings:
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue

    return path.read_text(encoding="utf-8", errors="ignore")


def _read_html_file(path: Path) -> str:
    """提取 HTML 正文，过滤导航、脚本和样式。"""
    raw = _read_text_file(path)

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(raw, "html.parser")

        for tag in soup(
            [
                "script",
                "style",
                "nav",
                "footer",
                "header",
                "aside",
                "noscript",
            ]
        ):
            tag.decompose()

        return soup.get_text("\n")
    except Exception:
        # BeautifulSoup 不可用时的基础兜底。
        raw = re.sub(
            r"(?is)<(script|style|nav|footer|header|aside).*?>.*?</\1>",
            "",
            raw,
        )
        raw = re.sub(r"(?s)<[^>]+>", "\n", raw)
        return html.unescape(raw)


def _read_docx_file(path: Path) -> str:
    """读取 DOCX，并将 Heading 样式转换成 Markdown 标题。"""
    from docx import Document

    document = Document(str(path))
    parts: list[str] = []

    for paragraph in document.paragraphs:
        text = paragraph.text.rstrip()
        if not text.strip():
            continue

        style_name = getattr(paragraph.style, "name", "") or ""
        heading_match = re.match(r"Heading\s+(\d+)", style_name, re.IGNORECASE)

        if heading_match:
            level = min(6, max(1, int(heading_match.group(1))))
            parts.append(f"{'#' * level} {text.strip()}")
        else:
            parts.append(text)

    return "\n\n".join(parts)


def _calculate_file_hash(path: Path) -> str:
    """计算文件 SHA-256，用于后续增量入库。"""
    digest = hashlib.sha256()

    with path.open("rb") as file:
        while block := file.read(1024 * 1024):
            digest.update(block)

    return digest.hexdigest()


def _detect_language(text: str) -> str:
    """使用简单字符比例判断中文、英文或混合文本。"""
    sample = text[:5000]

    chinese_count = len(re.findall(r"[\u4e00-\u9fff]", sample))
    english_count = len(re.findall(r"[A-Za-z]", sample))

    if chinese_count and english_count:
        if chinese_count > english_count * 2:
            return "zh"
        if english_count > chinese_count * 4:
            return "en"
        return "mixed"

    if chinese_count:
        return "zh"
    if english_count:
        return "en"
    return "unknown"


def _resolve_source_group(path: Path) -> str:
    """根据知识库目录层级识别来源分组。"""
    for part in path.parts:
        if part in SOURCE_GROUP_MAPPING:
            return SOURCE_GROUP_MAPPING[part]
    return "unknown"


def _resolve_document_title(content: str, fallback: str) -> str:
    """优先使用第一个 Markdown 一级标题作为文档标题。"""
    match = re.search(r"(?m)^#\s+(.+?)\s*$", content)
    if match:
        return match.group(1).strip()
    return fallback


def _relative_source(path: Path, root: Path) -> str:
    """将路径统一转换为 POSIX 风格相对路径。"""
    return path.relative_to(root).as_posix()


def _is_excluded_path(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)

    # 文件本身不参与目录排除判断。
    directory_parts = set(relative.parts[:-1])
    return bool(directory_parts & EXCLUDED_DIRECTORY_NAMES)


def iter_supported_files(
    base_dir: str,
    *,
    max_files: int | None = None,
) -> Iterator[Path]:
    """遍历支持入库的文件。"""
    root = Path(base_dir).resolve()

    if not root.exists():
        return

    emitted = 0

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue

        if _is_excluded_path(path, root):
            continue

        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue

        yield path
        emitted += 1

        if max_files is not None and emitted >= max_files:
            break


def _build_common_metadata(path: Path, root: Path) -> dict[str, Any]:
    stat = path.stat()

    return {
        "doc_id": hashlib.sha1(
            _relative_source(path, root).encode("utf-8")
        ).hexdigest(),
        "file_name": path.name,
        "absolute_path": str(path.resolve()),
        "source_group": _resolve_source_group(path),
        "file_hash": _calculate_file_hash(path),
        "modified_time": datetime.fromtimestamp(
            stat.st_mtime,
            timezone.utc,
        ).isoformat(),
    }


def _build_table_summary(
    frame: Any,
    *,
    file_name: str,
    sheet_name: str,
) -> tuple[str, dict[str, Any]]:
    """为 CSV 或 Excel 工作表生成结构摘要。"""
    row_count = int(len(frame))
    column_count = int(len(frame.columns))

    lines = [
        f"文件：{file_name}",
        f"工作表：{sheet_name}",
        f"总行数：{row_count}",
        f"总列数：{column_count}",
        "",
        "字段说明：",
    ]

    column_names: list[str] = []

    for original_column in list(frame.columns)[:100]:
        column_name = str(original_column)
        column_names.append(column_name)

        series = frame[original_column]
        missing_count = int(series.isna().sum())
        dtype = str(series.dtype)

        sample_values: list[str] = []
        try:
            values = (
                series.dropna()
                .astype(str)
                .head(200)
                .drop_duplicates()
                .head(5)
                .tolist()
            )
            sample_values = [
                value.replace("\r", " ").replace("\n", " ")[:80]
                for value in values
            ]
        except Exception:
            sample_values = []

        line = (
            f"- {column_name}：类型={dtype}，"
            f"缺失值={missing_count}"
        )

        if sample_values:
            line += f"，示例值={', '.join(sample_values)}"

        lines.append(line)

    if column_count > 100:
        lines.append(f"- 其余 {column_count - 100} 个字段未在摘要中展开。")

    metadata = {
        "sheet_name": sheet_name,
        "row_count": row_count,
        "column_count": column_count,
        "columns_csv": ",".join(column_names),
        "block_type": "table_summary",
        "parser_name": "pandas",
        "parser_version": "v1",
    }

    return "\n".join(lines), metadata


def _read_table_documents(
    path: Path,
    root: Path,
    common_metadata: dict[str, Any],
) -> list[LoadedDocument]:
    """读取 CSV、XLS 和 XLSX，并为每个工作表生成摘要。"""
    import pandas as pd

    suffix = path.suffix.lower()

    if suffix == ".csv":
        frame = None
        last_error: Exception | None = None

        for encoding in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                frame = pd.read_csv(
                    path,
                    encoding=encoding,
                    low_memory=False,
                    on_bad_lines="skip",
                )
                break
            except UnicodeDecodeError as exc:
                last_error = exc

        if frame is None:
            if last_error is not None:
                raise last_error
            raise ValueError(f"无法读取 CSV 文件：{path}")

        sheets = {"csv": frame}
    else:
        sheets = pd.read_excel(path, sheet_name=None)

    source = _relative_source(path, root)
    documents: list[LoadedDocument] = []

    for sheet_name, frame in sheets.items():
        summary, table_metadata = _build_table_summary(
            frame,
            file_name=path.name,
            sheet_name=str(sheet_name),
        )

        metadata = {
            **common_metadata,
            **table_metadata,
            "document_title": path.stem,
            "lang": _detect_language(summary),
        }

        documents.append(
            LoadedDocument(
                source=source,
                content=_normalize_text(summary),
                file_type=suffix.lstrip("."),
                metadata=metadata,
            )
        )

    return documents


def load_document_file(
    path: Path,
    base_dir: str | Path,
) -> list[LoadedDocument]:
    """读取单个源文件。

    一个文件可能产生多个 LoadedDocument。
    """
    root = Path(base_dir).resolve()
    path = Path(path).resolve()

    suffix = path.suffix.lower()

    if suffix not in SUPPORTED_EXTENSIONS:
        return []

    source = _relative_source(path, root)
    common_metadata = _build_common_metadata(path, root)

    if suffix in {".csv", ".xls", ".xlsx"}:
        return _read_table_documents(
            path,
            root,
            common_metadata,
        )

    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        documents: list[LoadedDocument] = []

        for page_index, page in enumerate(reader.pages, start=1):
            content = _normalize_text(page.extract_text() or "")
            if not content:
                continue

            metadata = {
                **common_metadata,
                "document_title": path.stem,
                "page_start": page_index,
                "page_end": page_index,
                "block_type": "page",
                "lang": _detect_language(content),
                "parser_name": "pypdf",
                "parser_version": "v1",
            }

            documents.append(
                LoadedDocument(
                    source=source,
                    content=content,
                    file_type="pdf",
                    metadata=metadata,
                )
            )

        return documents

    if suffix == ".docx":
        content = _normalize_text(_read_docx_file(path))
        parser_name = "python-docx"
        content_format = "markdown"
    elif suffix in {".html", ".htm"}:
        content = _normalize_text(_read_html_file(path))
        parser_name = "beautifulsoup"
        content_format = "plain"
    else:
        content = _normalize_text(_read_text_file(path))
        parser_name = "native"
        content_format = "markdown" if suffix == ".md" else "plain"

    if not content:
        return []

    metadata = {
        **common_metadata,
        "document_title": _resolve_document_title(
            content,
            path.stem,
        ),
        "block_type": "document",
        "lang": _detect_language(content),
        "parser_name": parser_name,
        "parser_version": "v1",
        "content_format": content_format,
    }

    rfc_match = re.fullmatch(r"rfc(\d+)", path.stem, re.IGNORECASE)
    if rfc_match:
        metadata["rfc_number"] = int(rfc_match.group(1))

    return [
        LoadedDocument(
            source=source,
            content=content,
            file_type=suffix.lstrip("."),
            metadata=metadata,
        )
    ]


def load_documents(
    base_dir: str,
    *,
    max_files: int | None = None,
) -> list[LoadedDocument]:
    """兼容旧调用：从目录加载全部文档。"""
    documents: list[LoadedDocument] = []

    for path in iter_supported_files(base_dir, max_files=max_files):
        try:
            documents.extend(load_document_file(path, base_dir))
        except Exception:
            # 批量加载时隔离单文件异常。
            continue

    return documents


def _build_markdown_sections(content: str) -> list[dict[str, Any]]:
    """按 Markdown H1～H6 构建章节。"""
    pattern = re.compile(r"(?m)^(#{1,6})[ \t]+(.+?)[ \t]*$")
    matches = list(pattern.finditer(content))

    if not matches:
        return [
            {
                "start": 0,
                "end": len(content),
                "content": content,
                "section_h1": None,
                "section_h2": None,
                "section_h3": None,
                "section_path": None,
            }
        ]

    sections: list[dict[str, Any]] = []
    hierarchy: list[str | None] = [None] * 6

    first_start = matches[0].start()
    preamble = content[:first_start].strip()

    if preamble:
        sections.append(
            {
                "start": 0,
                "end": first_start,
                "content": preamble,
                "section_h1": None,
                "section_h2": None,
                "section_h3": None,
                "section_path": None,
            }
        )

    for index, match in enumerate(matches):
        level = len(match.group(1))
        title = match.group(2).strip()

        hierarchy[level - 1] = title
        for reset_index in range(level, len(hierarchy)):
            hierarchy[reset_index] = None

        start = match.start()
        end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(content)
        )

        section_content = content[start:end].strip()
        section_path = " > ".join(
            item for item in hierarchy if item
        )

        sections.append(
            {
                "start": start,
                "end": end,
                "content": section_content,
                "section_h1": hierarchy[0],
                "section_h2": hierarchy[1],
                "section_h3": hierarchy[2],
                "section_path": section_path or None,
            }
        )

    return sections


def _split_text_windows(
    text: str,
    *,
    chunk_size: int,
    chunk_overlap: int,
) -> Iterator[tuple[int, int, str]]:
    """优先在段落或换行边界切片，最后才硬切字符。"""
    if not text:
        return

    start = 0
    text_length = len(text)

    while start < text_length:
        hard_end = min(text_length, start + chunk_size)
        end = hard_end

        if hard_end < text_length:
            minimum_boundary = start + max(1, chunk_size // 2)

            candidates = [
                text.rfind("\n\n", minimum_boundary, hard_end),
                text.rfind("\n", minimum_boundary, hard_end),
                text.rfind("。", minimum_boundary, hard_end),
                text.rfind(". ", minimum_boundary, hard_end),
            ]

            best_boundary = max(candidates)
            if best_boundary >= minimum_boundary:
                end = best_boundary + 1

        if end <= start:
            end = hard_end

        snippet = text[start:end].strip()

        if snippet:
            yield start, end, snippet

        if end >= text_length:
            break

        next_start = max(start + 1, end - chunk_overlap)
        start = next_start


def _build_embedding_content(
    document: LoadedDocument,
    snippet: str,
    section_path: str | None,
) -> str:
    """将标题、章节、页码等上下文加入向量化文本。"""
    prefix: list[str] = []

    title = document.metadata.get("document_title")
    if title:
        prefix.append(f"文档：{title}")

    if section_path:
        prefix.append(f"章节：{section_path}")

    page_start = document.metadata.get("page_start")
    if page_start is not None:
        prefix.append(f"页码：{page_start}")

    sheet_name = document.metadata.get("sheet_name")
    if sheet_name:
        prefix.append(f"工作表：{sheet_name}")

    if not prefix:
        return snippet

    return "\n".join(prefix) + "\n\n" + snippet


def _resolve_source_type(document: LoadedDocument) -> str:
    if (
        document.file_type == "md"
        and document.source.endswith(".pdf.md")
    ):
        return "pdf_md"
    return document.file_type


def split_documents(
    documents: Iterable[LoadedDocument],
    *,
    chunk_size: int = 1400,
    chunk_overlap: int = 180,
    ingest_batch_id: str | None = None,
) -> list[dict[str, Any]]:
    """将文档解析单元切分为可向量化 Chunk（文本块）。"""
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0。")

    if chunk_overlap < 0:
        raise ValueError("chunk_overlap 不能小于 0。")

    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap 必须小于 chunk_size。")

    chunks: list[dict[str, Any]] = []
    batch_id = ingest_batch_id or datetime.now(
        timezone.utc
    ).isoformat()

    source_chunk_counters: dict[str, int] = {}

    for document in documents:
        source_type = _resolve_source_type(document)

        is_markdown_format = (
            document.file_type == "md"
            or document.metadata.get("content_format") == "markdown"
        )

        if is_markdown_format:
            sections = _build_markdown_sections(document.content)
        else:
            sections = [
                {
                    "start": 0,
                    "end": len(document.content),
                    "content": document.content,
                    "section_h1": None,
                    "section_h2": None,
                    "section_h3": None,
                    "section_path": None,
                }
            ]

        source_chunk_counters.setdefault(document.source, 0)

        for section in sections:
            section_content = section["content"]
            section_base_offset = int(section["start"])

            for local_start, local_end, snippet in _split_text_windows(
                section_content,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            ):
                char_start = section_base_offset + local_start
                char_end = section_base_offset + local_end

                section_path = section.get("section_path")

                page_content = _build_embedding_content(
                    document,
                    snippet,
                    section_path,
                )

                content_hash = hashlib.sha1(
                    snippet.encode("utf-8")
                ).hexdigest()

                page_start = document.metadata.get("page_start")
                page_end = document.metadata.get("page_end")
                sheet_name = document.metadata.get("sheet_name")

                stable_chunk_key = "|".join(
                    [
                        document.source,
                        str(page_start or ""),
                        str(page_end or ""),
                        str(sheet_name or ""),
                        str(char_start),
                        str(char_end),
                        str(section_path or ""),
                        content_hash,
                    ]
                )

                chunk_id = hashlib.sha1(
                    stable_chunk_key.encode("utf-8")
                ).hexdigest()

                chunk_index = source_chunk_counters[document.source]
                source_chunk_counters[document.source] += 1

                metadata = {
                    **document.metadata,
                    "schema_version": SCHEMA_VERSION,
                    "source": document.source,
                    "source_type": source_type,
                    "file_type": document.file_type,
                    "chunk_id": chunk_id,
                    "chunk_index": chunk_index,
                    "content_hash": content_hash,
                    "char_start": char_start,
                    "char_end": char_end,
                    "offset": char_start,
                    "section_h1": section.get("section_h1"),
                    "section_h2": section.get("section_h2"),
                    "section_h3": section.get("section_h3"),
                    "section_path": section_path,
                    "block_type": document.metadata.get(
                        "block_type",
                        "paragraph",
                    ),
                    "lang": document.metadata.get(
                        "lang",
                        _detect_language(snippet),
                    ),
                    "chunker_name": CHUNKER_NAME,
                    "chunker_version": CHUNKER_VERSION,
                    "ingest_batch_id": batch_id,
                }

                chunks.append(
                    {
                        "page_content": page_content,
                        "metadata": metadata,
                    }
                )

    return chunks


def ensure_test_documents(base_dir: str) -> list[str]:
    """显式请求时才写入测试知识库文档。"""
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
3. 如命中率低，增大 top_k 并重建索引。
""",
        encoding="utf-8",
    )
    created.append(str(txt_path))

    try:
        import pandas as pd

        xlsx_path = root / "sla_matrix.xlsx"
        pd.DataFrame(
            [
                {
                    "服务等级": "P1",
                    "响应时效": "15分钟",
                    "恢复目标": "2小时",
                },
                {
                    "服务等级": "P2",
                    "响应时效": "1小时",
                    "恢复目标": "8小时",
                },
                {
                    "服务等级": "P3",
                    "响应时效": "4小时",
                    "恢复目标": "24小时",
                },
            ]
        ).to_excel(xlsx_path, index=False)

        created.append(str(xlsx_path))
    except Exception:
        csv_path = root / "sla_matrix.csv"
        csv_path.write_text(
            (
                "服务等级,响应时效,恢复目标\n"
                "P1,15分钟,2小时\n"
                "P2,1小时,8小时\n"
                "P3,4小时,24小时\n"
            ),
            encoding="utf-8",
        )
        created.append(str(csv_path))

    return created