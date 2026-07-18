from pathlib import Path

import pytest

from scripts import bootstrap_local_kb, query_local_kb


def test_bootstrap_help_is_available() -> None:
    with pytest.raises(SystemExit) as exc_info:
        bootstrap_local_kb.main(["--help"])

    assert exc_info.value.code == 0


def test_bootstrap_dry_run_does_not_load_vector_db(tmp_path: Path, capsys) -> None:
    (tmp_path / "faq.md").write_text("# FAQ\n\n本地知识库", encoding="utf-8")

    result = bootstrap_local_kb.main(["--source-dir", str(tmp_path), "--dry-run"])

    assert result == 0
    output = capsys.readouterr().out
    assert "Dry Run: True" in output
    assert "实际写入 Chunk: 0" in output


def test_bootstrap_rejects_invalid_directory(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        bootstrap_local_kb.main(
            ["--source-dir", str(tmp_path / "missing"), "--dry-run"]
        )

    assert exc_info.value.code == 2


def test_bootstrap_rejects_invalid_chunk_overlap(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        bootstrap_local_kb.main(
            [
                "--source-dir",
                str(tmp_path),
                "--dry-run",
                "--chunk-size",
                "100",
                "--chunk-overlap",
                "100",
            ]
        )

    assert exc_info.value.code == 2


def test_query_help_and_top_k_validation() -> None:
    with pytest.raises(SystemExit) as help_exit:
        query_local_kb.main(["--help"])
    assert help_exit.value.code == 0

    with pytest.raises(SystemExit) as invalid_exit:
        query_local_kb.main(["question", "--top-k", "0"])
    assert invalid_exit.value.code == 2
