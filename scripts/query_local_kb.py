#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""直接查询本地向量知识库，用于排查入库和前端 RAG 问题。"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Sequence

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.settings import Settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="直接执行 Embedding → Chroma 检索。")
    parser.add_argument("query", help="查询文本。")
    parser.add_argument("--collection", help="Chroma Collection 名称。")
    parser.add_argument("--top-k", type=int, default=5, help="返回结果数。")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.top_k < 1:
        parser.error("--top-k 必须大于或等于 1")

    from core.knowledge_base.vector_db_manager import VectorDBManager

    settings = Settings.load()
    collection_name = args.collection or settings.knowledge_collection_name
    manager = VectorDBManager(
        db_persist_dir=settings.chroma_dir,
        local_model_path=settings.embedding_model_path,
        collection_name=collection_name,
        embedding_batch_size=settings.embedding_batch_size,
        query_prompt_name=settings.embedding_query_prompt_name or None,
    )
    results = manager.search_with_scores(args.query, k=args.top_k)
    print(f"Collection: {collection_name}, 总 Chunk: {manager.count()}")
    print("=" * 100)
    for index, (document, score) in enumerate(results, start=1):
        metadata = document.metadata
        print(f"[{index}] score={score:.4f}")
        print(f"source={metadata.get('source', '未知来源')}")
        for key in (
            "section_path",
            "page_start",
            "sheet_name",
            "chunk_index",
            "content_hash",
        ):
            if metadata.get(key) is not None:
                print(f"{key}={metadata[key]}")
        print(document.page_content[:800])
        print("-" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
