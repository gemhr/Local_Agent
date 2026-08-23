#!/usr/bin/env python
"""构建或启动 Stage5 Phase3 WP0B 的 BEIR SciFact Evaluation Runtime。

build：外部 corpus.jsonl -> 物化 Markdown -> 既有 loader/splitter -> Qwen3 -> fresh Chroma。
serve：复用已构建的 persistence（不重建 embedding index），服务 frozen dense pipeline。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.knowledge_base.beir_scifact_environment import (  # noqa: E402
    BEIR_SCIFACT_COLLECTION_NAME,
    build_or_reuse_beir_scifact_cache,
    load_beir_scifact_cache,
)
from core.settings import Settings  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--cache-root", type=Path)
    build.add_argument("--manifest-out", type=Path)
    build.add_argument("--beir-corpus", type=Path, required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--cache-dir", type=Path, required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, required=True)
    return parser


def _build(args) -> tuple[object, object]:
    settings = Settings.load()
    cache = build_or_reuse_beir_scifact_cache(
        corpus_jsonl=args.beir_corpus,
        cache_root=args.cache_root,
        embedding_model_path=Path(settings.embedding_model_path),
        embedding_batch_size=settings.embedding_batch_size,
        query_prompt_name=settings.embedding_query_prompt_name or None,
    )
    return cache


def _load_built_manager(cache_dir: Path):
    """加载 READY cache；不执行 ingest 或 embedding rebuild。"""
    settings = Settings.load()
    return load_beir_scifact_cache(
        cache_dir=cache_dir,
        embedding_model_path=Path(settings.embedding_model_path),
        embedding_batch_size=settings.embedding_batch_size,
        query_prompt_name=settings.embedding_query_prompt_name or None,
    )


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    args = _parser().parse_args(argv)
    if args.command == "build":
        cache = _build(args)
        if args.manifest_out is not None:
            args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
            args.manifest_out.write_bytes(cache.manifest_path.read_bytes())
        print(
            json.dumps(
                {
                    "status": cache.status,
                    "collection": BEIR_SCIFACT_COLLECTION_NAME,
                    "cache_dir": str(cache.cache_dir),
                    "cache_key": cache.identity.cache_key,
                    "manifest_sha256": cache.identity.manifest_sha256,
                    "elapsed_seconds": cache.elapsed_seconds,
                }
            ),
            flush=True,
        )
        return 0

    manager, cache = _load_built_manager(args.cache_dir)
    import server
    import uvicorn

    from scripts.rag_evaluation_runtime import RetrievalBaselineService

    server.chat_service = RetrievalBaselineService(
        manager, collection_name=BEIR_SCIFACT_COLLECTION_NAME
    )
    print(
        json.dumps(
            {
                "status": "CACHE_HIT",
                "collection": BEIR_SCIFACT_COLLECTION_NAME,
                "cache_dir": str(cache.cache_dir),
                "cache_key": cache.identity.cache_key,
                "manifest_sha256": cache.identity.manifest_sha256,
                "embedding_rebuild": "NO",
            }
        ),
        flush=True,
    )
    uvicorn.run(
        server.app,
        host=args.host,
        port=args.port,
        lifespan="off",
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
