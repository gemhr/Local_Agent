#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成测试知识文档并写入向量库。"""

from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.knowledge_base.document_loader import ensure_test_documents, load_documents, split_documents
from core.settings import Settings


def main() -> None:
    settings = Settings.load()

    created = ensure_test_documents(settings.local_knowledge_base_dir)
    print("[KB] 已生成测试文档:")
    for path in created:
        print(f"  - {path}")

    documents = load_documents(settings.local_knowledge_base_dir)
    chunks = split_documents(documents)
    print(f"[KB] 加载文档数: {len(documents)}")
    print(f"[KB] 切片数量: {len(chunks)}")

    if not chunks:
        print("[KB] 没有可入库的切片，已跳过。")
        return

    try:
        from core.knowledge_base.vector_db_manager import VectorDBManager
    except Exception as exc:
        print(f"[KB] 跳过向量化：缺少依赖({exc})。请先安装项目依赖后重试。")
        return

    manager = VectorDBManager(
        db_persist_dir=settings.chroma_dir,
        local_model_path=settings.embedding_model_path,
    )
    manager.ingest_chunks(chunks)
    print(f"[KB] 向量化完成，已写入: {settings.chroma_dir}")


if __name__ == "__main__":
    main()
