#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""直接查询本地向量知识库，用于验证入库结果。"""

from __future__ import annotations

import argparse
import os
import sys

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.knowledge_base.vector_db_manager import VectorDBManager
from core.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", help="查询内容")
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
    )
    parser.add_argument(
        "--collection",
        default=None,
    )

    args = parser.parse_args()
    settings = Settings.load()

    collection_name = (
        args.collection
        or settings.knowledge_collection_name
    )

    manager = VectorDBManager(
        db_persist_dir=settings.chroma_dir,
        local_model_path=settings.embedding_model_path,
        collection_name=collection_name,
        embedding_batch_size=(settings.embedding_batch_size),
        query_prompt_name=(settings.embedding_query_prompt_name),
    )

    results = manager.search_with_scores(
        query=args.query,
        k=args.top_k,
    )

    print(
        f"Collection: {collection_name}, "
        f"总 Chunk: {manager.count()}"
    )
    print("=" * 100)

    for index, (document, score) in enumerate(
        results,
        start=1,
    ):
        metadata = document.metadata

        print(f"[{index}] score={score:.4f}")
        print(f"source={metadata.get('source')}")

        if metadata.get("section_path"):
            print(
                f"section={metadata.get('section_path')}"
            )

        if metadata.get("page_start") is not None:
            print(
                f"page={metadata.get('page_start')}"
            )

        if metadata.get("sheet_name"):
            print(
                f"sheet={metadata.get('sheet_name')}"
            )

        print(document.page_content[:800])
        print("-" * 100)


if __name__ == "__main__":
    main()
