import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.knowledge_base.document_loader import (
    iter_supported_files,
    load_document_file,
    split_documents,
)


def test_markdown_indentation_and_heading_metadata_are_preserved(
    tmp_path: Path,
) -> None:
    source = tmp_path / "example.md"
    source.write_text(
        "# 示例\n\n## Python 代码\n\n```python\nasync def main():\n"
        "    if True:\n        await run()\n```\n",
        encoding="utf-8",
    )

    documents = load_document_file(source, tmp_path)
    chunks = split_documents(documents, chunk_size=500, chunk_overlap=50)

    assert "    if True:" in documents[0].content
    assert "        await run()" in documents[0].content
    assert any(chunk["metadata"].get("section_h2") == "Python 代码" for chunk in chunks)


def test_csv_is_summarized_instead_of_dumped(tmp_path: Path) -> None:
    pandas = pytest.importorskip("pandas")
    source = tmp_path / "sample.csv"
    pandas.DataFrame(
        {"name": ["a", "b", None], "result": ["PASS", "FAIL", "PASS"]}
    ).to_csv(source, index=False, encoding="utf-8-sig")

    document = load_document_file(source, tmp_path)[0]

    assert document.metadata["block_type"] == "table_summary"
    assert document.metadata["row_count"] == 3
    assert "字段说明" in document.content
    assert "PASS\nFAIL\nPASS" not in document.content


def test_pdf_page_metadata_is_retained(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "manual.pdf"
    source.write_bytes(b"not-a-real-pdf")
    pages = [
        SimpleNamespace(extract_text=lambda: "第一页"),
        SimpleNamespace(extract_text=lambda: "第二页"),
    ]
    fake_module = SimpleNamespace(PdfReader=lambda _path: SimpleNamespace(pages=pages))
    monkeypatch.setitem(sys.modules, "pypdf", fake_module)

    documents = load_document_file(source, tmp_path)
    chunks = split_documents(documents)

    assert [document.metadata["page_start"] for document in documents] == [1, 2]
    assert {chunk["metadata"]["page_start"] for chunk in chunks} == {1, 2}


def test_long_text_chunk_size_and_overlap_are_bounded(tmp_path: Path) -> None:
    source = tmp_path / "long.txt"
    source.write_text("A" * 2500, encoding="utf-8")
    chunks = split_documents(
        load_document_file(source, tmp_path),
        chunk_size=1000,
        chunk_overlap=100,
    )

    assert len(chunks) == 3
    assert all(len(chunk["page_content"]) <= 1000 for chunk in chunks)
    overlap = chunks[0]["metadata"]["char_end"] - chunks[1]["metadata"]["char_start"]
    assert overlap == 100


def test_invalid_chunk_parameters_are_rejected() -> None:
    with pytest.raises(ValueError, match="小于 chunk_size"):
        split_documents([], chunk_size=100, chunk_overlap=100)


def test_local_artifact_directories_are_excluded(tmp_path: Path) -> None:
    metadata = tmp_path / "00_metadata"
    metadata.mkdir()
    (metadata / "manifest.csv").write_text("a,b\n1,2", encoding="utf-8")
    valid = tmp_path / "faq.md"
    valid.write_text("# FAQ", encoding="utf-8")

    assert list(iter_supported_files(str(tmp_path))) == [valid]
