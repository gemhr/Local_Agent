from pathlib import Path

import pandas as pd

from core.knowledge_base.document_loader import (
    iter_supported_files,
    load_document_file,
    split_documents,
)


def test_metadata_directory_is_excluded(
    tmp_path: Path,
) -> None:
    metadata_dir = tmp_path / "00_metadata"
    metadata_dir.mkdir()

    (metadata_dir / "documents_manifest.csv").write_text(
        "a,b\n1,2\n",
        encoding="utf-8",
    )

    normal_dir = tmp_path / "05_company_simulation"
    normal_dir.mkdir()

    normal_file = normal_dir / "faq.md"
    normal_file.write_text(
        "# FAQ\n\n正常正文",
        encoding="utf-8",
    )

    files = list(iter_supported_files(str(tmp_path)))

    assert normal_file in files
    assert (
        metadata_dir / "documents_manifest.csv"
    ) not in files


def test_markdown_indentation_is_preserved(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "example.md"
    file_path.write_text(
        """# 示例

## Python 代码

```python
async def main():
    if True:
        await run()
    """,
        encoding="utf-8",
    )

    documents = load_document_file(
        file_path,
        tmp_path,
    )

    assert len(documents) == 1
    assert "    if True:" in documents[0].content
    assert "        await run()" in documents[0].content

    chunks = split_documents(
        documents,
        chunk_size=500,
        chunk_overlap=50,
    )

    assert chunks
    assert any(
        chunk["metadata"].get("section_h2")
        == "Python 代码"
        for chunk in chunks
    )

def test_csv_is_converted_to_summary(
            tmp_path: Path,
    ) -> None:
    file_path = tmp_path / "sample.csv"

    pd.DataFrame(
        {
            "name": ["a", "b", None],
            "result": ["PASS", "FAIL", "PASS"],
        }
    ).to_csv(
        file_path,
        index=False,
        encoding="utf-8-sig",
    )

    documents = load_document_file(
        file_path,
        tmp_path,
    )

    assert len(documents) == 1

    document = documents[0]

    assert document.metadata["block_type"] == "table_summary"
    assert document.metadata["row_count"] == 3
    assert document.metadata["column_count"] == 2
    assert "字段说明" in document.content
    assert "result" in document.content