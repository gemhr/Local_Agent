#!/usr/bin/env python
"""构建或启动 Stage5 Phase3 的隔离 RAG Evaluation Runtime。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.agent_router import AgentRouter  # noqa: E402
from core.knowledge_base.evaluation_environment import (  # noqa: E402
    COLLECTION_NAME,
    build_evaluation_kb,
    manifest_json,
)
from core.runtime import (  # noqa: E402
    BudgetLedger,
    ChatRuntimeMode,
    RetrievalExecutionService,
    RetrievalExecutionSpec,
    RetrievalInvocation,
    RunBudget,
    RunCoordinatorResult,
    RunStatus,
    StopReason,
    create_run_context,
)
from core.runtime.retrieval_adapters import RuntimeKnowledgeRetrievalAdapter  # noqa: E402
from core.settings import Settings  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "serve"):
        item = sub.add_parser(name)
        item.add_argument("--persist-dir", type=Path, required=True)
        item.add_argument("--manifest-out", type=Path)
        if name == "serve":
            item.add_argument("--host", default="127.0.0.1")
            item.add_argument("--port", type=int, required=True)
    return parser


def _build(persist_dir: Path):
    settings = Settings.load()
    return build_evaluation_kb(
        persist_dir=persist_dir,
        embedding_model_path=Path(settings.embedding_model_path),
        embedding_batch_size=settings.embedding_batch_size,
        query_prompt_name=settings.embedding_query_prompt_name or None,
    )


def _write_manifest(value: str, path: Path | None, *, echo: bool) -> None:
    if path is not None:
        path.write_text(value + "\n", encoding="utf-8")
    if echo:
        print(value, flush=True)


class RetrievalBaselineService:
    """固定 identity rewrite，仅测当前 Retrieval 算法。"""

    admission_gate = SimpleNamespace(accepts_new_runs=True)

    def __init__(self, manager) -> None:
        adapter = RuntimeKnowledgeRetrievalAdapter(
            manager,
            query_rewriter=lambda query, *_args, **_kwargs: query,
            query_term_extractor=lambda rewritten, original: AgentRouter._extract_query_terms(
                None, rewritten, original
            ),
            candidate_scorer=lambda content, score, terms, metadata: AgentRouter._score_rag_candidate(
                None, content, score, terms, metadata
            ),
        )
        self._retrieval = RetrievalExecutionService(
            adapter,
            spec=RetrievalExecutionSpec(
                max_candidates=8,
                max_context_chunks=3,
                max_context_chars=2400,
                max_single_chunk_chars=1000,
                max_document_reads=8,
            ),
            minimum_score=0.55,
        )

    def selected_runtime_mode(self):
        return ChatRuntimeMode.COORDINATED

    async def run_coordinated_agent(self, *, query, run_id, **_kwargs):
        context, _source = create_run_context(
            entry_agent_id="knowledge_expert", run_id=run_id
        )
        context.attach_budget_ledger(
            BudgetLedger(RunBudget(), deadline_remaining=context.remaining_seconds)
        )
        result = self._retrieval.execute(
            RetrievalInvocation.create(
                query,
                collection_names=(COLLECTION_NAME,),
                top_k=8,
                rerank_top_k=3,
                requested_timeout_seconds=30.0,
            ),
            run_context=context,
            step_id="rag-evaluation-retrieval",
        )
        succeeded = result.status.value in {"SUCCEEDED", "EMPTY", "DEGRADED"}
        coordinator = RunCoordinatorResult(
            run_id=run_id,
            plan_id="rag-evaluation-baseline.v1",
            status=RunStatus.SUCCEEDED if succeeded else RunStatus.FAILED,
            stop_reason=StopReason.COMPLETED if succeeded else StopReason.UNHANDLED_ERROR,
            succeeded_step_ids=("rag-evaluation-retrieval",) if succeeded else (),
            failed_step_ids=() if succeeded else ("rag-evaluation-retrieval",),
            cancelled_step_ids=(),
            blocked_step_ids=(),
            budget_snapshot=context.budget_ledger.snapshot(),
            cleanup_error_codes=(),
            error_code=None if succeeded else "RAG_EVALUATION_RETRIEVAL_FAILED",
            safe_message="" if succeeded else "RAG evaluation retrieval failed",
        )
        return "retrieval baseline completed", coordinator


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    args = _parser().parse_args(argv)
    manager, manifest = _build(args.persist_dir)
    payload = manifest_json(manifest)
    _write_manifest(payload, args.manifest_out, echo=args.command == "build")
    if args.command == "build":
        return 0
    import server
    import uvicorn

    server.chat_service = RetrievalBaselineService(manager)
    print(json.dumps({"status": "READY", "collection": COLLECTION_NAME}), flush=True)
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
