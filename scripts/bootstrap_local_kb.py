#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""将本地知识文档解析、切片并写入向量库。"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.knowledge_base.document_loader import (
    ensure_test_documents,
    iter_supported_files,
    load_document_file,
    split_documents,
)
from core.settings import Settings


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须大于或等于 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="将本地知识文档写入 Chroma 向量库。")
    parser.add_argument("--source-dir", help="知识文档目录；默认读取 Settings。")
    parser.add_argument("--collection", help="Chroma Collection 名称。")
    parser.add_argument(
        "--rebuild", action="store_true", help="入库前清空当前 Collection。"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只扫描、解析和切片，不加载模型或写入。"
    )
    parser.add_argument(
        "--seed-test-docs", action="store_true", help="先生成仓库自带的测试文档。"
    )
    parser.add_argument("--max-files", type=_positive_int, help="最多处理的文件数。")
    parser.add_argument(
        "--chunk-size", type=_positive_int, default=1400, help="Chunk 目标字符数。"
    )
    parser.add_argument(
        "--chunk-overlap", type=int, default=180, help="相邻 Chunk 重叠字符数。"
    )
    parser.add_argument(
        "--ingest-batch-size", type=_positive_int, default=32, help="单次向量化写入数。"
    )
    parser.add_argument(
        "--flush-chunks",
        type=_positive_int,
        default=128,
        help="累计多少 Chunk 后写入。",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.chunk_overlap < 0:
        parser.error("--chunk-overlap 不能小于 0")
    if args.chunk_overlap >= args.chunk_size:
        parser.error("--chunk-overlap 必须小于 --chunk-size")

    settings = Settings.load()
    source_dir = Path(args.source_dir or settings.local_knowledge_base_dir).resolve()
    collection_name = args.collection or settings.knowledge_collection_name
    if not source_dir.is_dir():
        parser.error(f"知识库目录不存在或不是目录：{source_dir}")

    if args.seed_test_docs:
        for path in ensure_test_documents(str(source_dir)):
            print(f"[KB] 已生成测试文档: {path}")

    files = list(iter_supported_files(str(source_dir), max_files=args.max_files))
    extension_counts = Counter(path.suffix.lower() for path in files)
    print(f"[KB] 文档目录: {source_dir}")
    print(f"[KB] Collection: {collection_name}")
    print(f"[KB] Dry Run: {args.dry_run}")
    print(f"[KB] Chunk 参数: size={args.chunk_size}, overlap={args.chunk_overlap}")
    print(f"[KB] 支持文件数: {len(files)}")
    for suffix, count in sorted(extension_counts.items()):
        print(f"[KB] 文件类型 {suffix}: {count}")
    if not files:
        print("[KB] 没有发现可处理文件。")
        return 0

    manager = None
    if not args.dry_run:
        from core.knowledge_base.vector_db_manager import VectorDBManager

        manager = VectorDBManager(
            db_persist_dir=settings.chroma_dir,
            local_model_path=settings.embedding_model_path,
            collection_name=collection_name,
            ingest_batch_size=args.ingest_batch_size,
            embedding_batch_size=settings.embedding_batch_size,
            query_prompt_name=settings.embedding_query_prompt_name or None,
        )
        if args.rebuild:
            print(f"[KB] 已清空 Collection，删除 Chunk: {manager.clear_collection()}")

    batch_id = datetime.now(timezone.utc).isoformat()
    pending_chunks: list[dict] = []
    pending_sources: list[str] = []
    success_files = skipped_files = failed_files = 0
    parsed_units = total_chunks = written_chunks = 0
    failures: list[tuple[str, str]] = []

    def flush() -> int:
        nonlocal pending_chunks, pending_sources
        if manager is None or not pending_chunks:
            return 0
        for source in dict.fromkeys(pending_sources):
            manager.delete_source(source)
        written = manager.ingest_chunks(pending_chunks)
        print(f"[KB] 批量写入完成: files={len(set(pending_sources))}, chunks={written}")
        pending_chunks = []
        pending_sources = []
        return written

    for index, path in enumerate(files, start=1):
        source = path.relative_to(source_dir).as_posix()
        try:
            documents = load_document_file(path, source_dir)
            if not documents:
                skipped_files += 1
                print(f"[KB] [{index}/{len(files)}] 跳过空文档: {source}")
                continue
            chunks = split_documents(
                documents,
                chunk_size=args.chunk_size,
                chunk_overlap=args.chunk_overlap,
                ingest_batch_id=batch_id,
            )
            if not chunks:
                skipped_files += 1
                print(f"[KB] [{index}/{len(files)}] 没有有效 Chunk: {source}")
                continue
            success_files += 1
            parsed_units += len(documents)
            total_chunks += len(chunks)
            if manager is not None:
                pending_sources.append(source)
                pending_chunks.extend(chunks)
                if len(pending_chunks) >= args.flush_chunks:
                    written_chunks += flush()
            print(
                f"[KB] [{index}/{len(files)}] 成功: {source} | "
                f"解析单元={len(documents)} | chunks={len(chunks)}"
            )
        except Exception as exc:
            failed_files += 1
            failures.append((source, str(exc)))
            print(f"[KB] [{index}/{len(files)}] 失败: {source} | {exc}")

    written_chunks += flush()
    print("[KB] 入库任务完成")
    print(f"[KB] 成功文件: {success_files}")
    print(f"[KB] 跳过文件: {skipped_files}")
    print(f"[KB] 失败文件: {failed_files}")
    print(f"[KB] 解析单元: {parsed_units}")
    print(f"[KB] 生成 Chunk: {total_chunks}")
    print(f"[KB] 实际写入 Chunk: {written_chunks}")
    if manager is not None:
        print(f"[KB] Collection 当前总量: {manager.count()}")
        print(f"[KB] Chroma 路径: {settings.chroma_dir}")
    for source, error in failures:
        print(f"[KB] 失败明细: {source}: {error}")
    return 1 if failed_files else 0


if __name__ == "__main__":
    raise SystemExit(main())
