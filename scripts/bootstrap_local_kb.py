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

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.knowledge_base.document_loader import (
    ensure_test_documents,
    iter_supported_files,
    load_document_file,
    split_documents,
)
from core.settings import Settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将本地知识文档写入 Chroma 向量库。",
    )

    parser.add_argument(
        "--source-dir",
        default=None,
        help="原始知识文档目录。默认读取 Settings.local_knowledge_base_dir。",
    )
    parser.add_argument(
        "--collection",
        default=None,
        help="Chroma Collection 名称。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只扫描、解析和切片，不加载模型、不写入向量库。",
    )
    parser.add_argument(
        "--seed-test-docs",
        action="store_true",
        help="先生成原有测试文档。默认不生成。",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=None,
        help="最多处理多少个支持的文件。",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="入库前清空当前 Collection。",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=1400,
        help="单个 Chunk 的目标字符数。",
    )
    parser.add_argument(
        "--chunk-overlap",
        type=int,
        default=180,
        help="相邻 Chunk 的重叠字符数。",
    )
    parser.add_argument(
        "--ingest-batch-size",
        type=int,
        default=32,
        help="单次向量化和写入的 Chunk 数量。",
    )

    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = Settings.load()

    source_dir = Path(
        args.source_dir
        or settings.local_knowledge_base_dir
    ).resolve()

    collection_name = (
        args.collection
        or settings.knowledge_collection_name
    )

    if not source_dir.exists():
        raise FileNotFoundError(
            f"知识库目录不存在：{source_dir}"
        )

    if args.chunk_overlap >= args.chunk_size:
        raise ValueError(
            "chunk-overlap 必须小于 chunk-size。"
        )

    print("=" * 80)
    print(f"[KB] 文档目录: {source_dir}")
    print(f"[KB] Collection: {collection_name}")
    print(f"[KB] Dry Run: {args.dry_run}")
    print(
        f"[KB] Chunk 参数: "
        f"size={args.chunk_size}, "
        f"overlap={args.chunk_overlap}"
    )
    print("=" * 80)

    if args.seed_test_docs:
        created = ensure_test_documents(str(source_dir))
        print("[KB] 已生成测试文档：")
        for path in created:
            print(f"  - {path}")

    files = list(
        iter_supported_files(
            str(source_dir),
            max_files=args.max_files,
        )
    )

    extension_counter = Counter(
        path.suffix.lower() for path in files
    )

    print(f"[KB] 支持文件数: {len(files)}")
    print("[KB] 文件类型统计:")

    for suffix, count in sorted(extension_counter.items()):
        print(f"  - {suffix}: {count}")

    if not files:
        print("[KB] 没有发现可处理文件。")
        return

    manager = None

    if not args.dry_run:
        try:
            from core.knowledge_base.vector_db_manager import (
                VectorDBManager,
            )
        except Exception as exc:
            print(
                f"[KB] 缺少向量库依赖：{exc}"
            )
            return

        manager = VectorDBManager(
            db_persist_dir=settings.chroma_dir,
            local_model_path=settings.embedding_model_path,
            collection_name=collection_name,
            ingest_batch_size=args.ingest_batch_size,
            embedding_batch_size=settings.embedding_batch_size,
            query_prompt_name=(settings.embedding_query_prompt_name),
        )

        if args.rebuild:
            deleted = manager.clear_collection()
            print(
                f"[KB] 已清空 Collection，"
                f"删除 Chunk 数: {deleted}"
            )

    ingest_batch_id = datetime.now(
        timezone.utc
    ).isoformat()

    success_files = 0
    skipped_files = 0
    failed_files = 0
    parsed_documents = 0
    total_chunks = 0
    written_chunks = 0

    failures: list[tuple[str, str]] = []

    for index, path in enumerate(files, start=1):
        source = path.relative_to(source_dir).as_posix()

        try:
            documents = load_document_file(
                path,
                source_dir,
            )

            if not documents:
                skipped_files += 1
                print(
                    f"[KB] [{index}/{len(files)}] "
                    f"跳过空文档: {source}"
                )
                continue

            chunks = split_documents(
                documents,
                chunk_size=args.chunk_size,
                chunk_overlap=args.chunk_overlap,
                ingest_batch_id=ingest_batch_id,
            )

            parsed_documents += len(documents)
            total_chunks += len(chunks)

            if not chunks:
                skipped_files += 1
                print(
                    f"[KB] [{index}/{len(files)}] "
                    f"没有有效 Chunk: {source}"
                )
                continue

            if manager is not None:
                written = manager.replace_source_chunks(
                    source,
                    chunks,
                )
                written_chunks += written

            success_files += 1

            print(
                f"[KB] [{index}/{len(files)}] "
                f"成功: {source} | "
                f"解析单元={len(documents)} | "
                f"chunks={len(chunks)}"
            )

        except Exception as exc:
            failed_files += 1
            failures.append((source, str(exc)))

            print(
                f"[KB] [{index}/{len(files)}] "
                f"失败: {source} | {exc}"
            )

    print()
    print("=" * 80)
    print("[KB] 入库任务完成")
    print(f"[KB] 成功文件: {success_files}")
    print(f"[KB] 跳过文件: {skipped_files}")
    print(f"[KB] 失败文件: {failed_files}")
    print(f"[KB] 解析单元: {parsed_documents}")
    print(f"[KB] 生成 Chunk: {total_chunks}")
    print(f"[KB] 实际写入 Chunk: {written_chunks}")

    if manager is not None:
        print(
            f"[KB] Collection 当前总量: "
            f"{manager.count()}"
        )
        print(
            f"[KB] Chroma 路径: "
            f"{settings.chroma_dir}"
        )

    if failures:
        print("[KB] 失败明细:")
        for source, error in failures:
            print(f"  - {source}: {error}")

    print("=" * 80)


if __name__ == "__main__":
    main()
