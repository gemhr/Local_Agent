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
    parser = argparse.ArgumentParser(description="构建本地知识库检索 generation。")
    parser.add_argument("--source-dir", help="知识文档目录；默认读取 Settings。")
    parser.add_argument("--collection", help="Chroma Collection 名称。")
    parser.add_argument(
        "--build-purpose",
        choices=("production", "development"),
        default="production",
        help="production 使用 Settings chunk 参数并允许发布 active.json；"
        "development 允许覆盖 chunk 参数但不能发布 active.json。",
    )
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
        "--chunk-size",
        type=_positive_int,
        default=None,
        help="Chunk 目标字符数；production 模式禁止提供（使用 Settings）。",
    )
    parser.add_argument(
        "--chunk-overlap",
        type=int,
        default=None,
        help="相邻 Chunk 重叠字符数；production 模式禁止提供（使用 Settings）。",
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
    if args.chunk_overlap is not None and args.chunk_overlap < 0:
        parser.error("--chunk-overlap 不能小于 0")
    if (
        args.chunk_size is not None
        and args.chunk_overlap is not None
        and args.chunk_overlap >= args.chunk_size
    ):
        parser.error("--chunk-overlap 必须小于 --chunk-size")

    settings = Settings.load()
    if args.build_purpose == "production" and (
        args.chunk_size is not None or args.chunk_overlap is not None
    ):
        parser.error(
            "production 构建禁止提供 --chunk-size/--chunk-overlap；"
            "chunk policy 唯一 authority 是 Settings（LOCAL_AGENT_KB_CHUNK_SIZE/OVERLAP）。"
        )
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
    print(f"[KB] Build Purpose: {args.build_purpose}")
    print(f"[KB] Dry Run: {args.dry_run}")
    chunk_size = (
        settings.knowledge_chunk_size
        if args.chunk_size is None
        else args.chunk_size
    )
    chunk_overlap = (
        settings.knowledge_chunk_overlap
        if args.chunk_overlap is None
        else args.chunk_overlap
    )
    print(f"[KB] Chunk 参数: size={chunk_size}, overlap={chunk_overlap}")
    print(f"[KB] 支持文件数: {len(files)}")
    for suffix, count in sorted(extension_counts.items()):
        print(f"[KB] 文件类型 {suffix}: {count}")
    if not files:
        print("[KB] 没有发现可处理文件。")
        return 0

    if args.build_purpose == "production" and not args.dry_run:
        from core.knowledge_base.production_build import (
            BUILD_PURPOSE_PRODUCTION,
            build_production_generation,
        )

        try:
            result = build_production_generation(
                source_dir=source_dir,
                logical_collection_name=collection_name,
                chroma_dir=settings.chroma_dir,
                embedding_model_path=settings.embedding_model_path,
                chunk_size=settings.knowledge_chunk_size,
                chunk_overlap=settings.knowledge_chunk_overlap,
                embedding_batch_size=settings.embedding_batch_size,
                query_prompt_name=settings.embedding_query_prompt_name or None,
                purpose=BUILD_PURPOSE_PRODUCTION,
                publish_active=True,
            )
            print(f"[KB] 生产 generation 构建完成: {result.generation_id}")
            print(f"[KB] Dense collection: {result.dense_collection_name}")
            print(f"[KB] active.json 已发布: {result.active_published}")
            return 0
        except Exception as exc:  # noqa: BLE001 - CLI 顶层错误报告
            print(f"[KB] 生产 build 失败（旧 active 保持不变）: {exc}")
            return 1

    if args.build_purpose == "development" and not args.dry_run:
        from core.knowledge_base.production_build import (
            BUILD_PURPOSE_DEVELOPMENT,
            build_production_generation,
        )

        try:
            result = build_production_generation(
                source_dir=source_dir,
                logical_collection_name=collection_name,
                chroma_dir=settings.chroma_dir,
                embedding_model_path=settings.embedding_model_path,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                embedding_batch_size=settings.embedding_batch_size,
                query_prompt_name=settings.embedding_query_prompt_name or None,
                purpose=BUILD_PURPOSE_DEVELOPMENT,
                publish_active=False,
            )
            print(f"[KB] 开发 generation 构建完成: {result.generation_id}")
            print(f"[KB] Dense collection: {result.dense_collection_name}")
            print(f"[KB] active.json 未发布（development 禁止）: {result.active_published}")
            return 0
        except Exception as exc:  # noqa: BLE001 - CLI 顶层错误报告
            print(f"[KB] 开发 build 失败: {exc}")
            return 1

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
            # WP1-D：destructive rebuild 必须先使旧 marker 失效/移除，再清空，
            # 成功后最后发布新 marker。任何失败都不得保留“看似有效”的旧 marker。
            manager.remove_collection_marker()
            print(f"[KB] 已移除 LocalAgent Collection marker，删除 Chunk: {manager.clear_collection()}")

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
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
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
    marker_publish_failed = False
    if manager is not None:
        print(f"[KB] Collection 当前总量: {manager.count()}")
        print(f"[KB] Chroma 路径: {settings.chroma_dir}")
        if failed_files == 0 and written_chunks > 0:
            # WP1-D：完整 source ingest 成功后才发布匹配 marker（最后一步）。
            try:
                manager.publish_collection_marker()
                print("[KB] 已发布 LocalAgent Collection marker")
            except Exception as exc:
                marker_publish_failed = True
                print(f"[KB] LocalAgent Collection marker 发布失败（collection 保持 unmarked）: {exc}")
        elif failed_files:
            print("[KB] 存在失败文件，不发布 Collection marker（保持 unmarked/mismatched）")
        else:
            print("[KB] 没有写入任何 Chunk，不发布 Collection marker")
    for source, error in failures:
        print(f"[KB] 失败明细: {source}: {error}")
    return 1 if (failed_files or marker_publish_failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
