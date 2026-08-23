"""Cross-Encoder reranker deterministic fake tests（不依赖真实模型/网络/benchmark）。"""

from __future__ import annotations

import asyncio
import hashlib
import time

import pytest

from core.knowledge_base.cross_encoder_reranker import (
    CE_CANDIDATE_LIMIT,
    CE_ERROR_OOM,
    CE_PROVENANCE_SCHEMA_VERSION,
    CeCandidate,
    CeEnvironmentMeta,
    CeRerankResult,
    ChunkReadResult,
    ChromaCollectionReader,
    ChromaTextResolver,
    CrossEncoderAssetConfig,
    CrossEncoderAssetError,
    CrossEncoderBackpressureError,
    CrossEncoderCancellationError,
    CrossEncoderCandidateError,
    CrossEncoderInferenceError,
    CrossEncoderLoadError,
    CrossEncoderModelState,
    CrossEncoderOomError,
    CrossEncoderReranker,
    CrossEncoderScoreValidationError,
    CrossEncoderTextResolutionError,
    CrossEncoderTimeoutError,
    ResolvedChunkText,
    compute_asset_tree_sha256,
    deterministic_reorder,
    provenance_to_safe_row,
    validate_asset,
    validate_candidates,
    validate_scores,
)

TEXT_A = "alpha chunk content for ce test"
TEXT_B = "beta chunk content for ce test"
TEXT_C = "gamma chunk content for ce test"
HASH_A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
HASH_B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
HASH_C = "cccccccccccccccccccccccccccccccccccccccc"
DOC_A = "doc-a"
DOC_B = "doc-b"
DOC_C = "doc-c"
CHUNK_A = "chunk-a"
CHUNK_B = "chunk-b"
CHUNK_C = "chunk-c"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FakeScorer:
    def __init__(
        self,
        scores=None,
        *,
        exception=None,
        delay=0.0,
        oom=False,
    ) -> None:
        self.scores = scores
        self.exception = exception
        self.delay = delay
        self.oom = oom
        self.calls = 0

    def score(self, query: str, texts):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.oom:
            raise CrossEncoderOomError(CE_ERROR_OOM)
        if self.exception is not None:
            raise self.exception
        if self.scores is None:
            return [1.0 / (index + 1) for index in range(len(texts))]
        return self.scores


class FakeLoader:
    def __init__(self, scorer=None, *, load_exception=None, load_oom=False) -> None:
        self.scorer = scorer or FakeScorer()
        self.calls = 0
        self.load_exception = load_exception
        self.load_oom = load_oom

    def load(self, config):
        self.calls += 1
        if self.load_oom:
            raise CrossEncoderOomError(CE_ERROR_OOM)
        if self.load_exception is not None:
            raise self.load_exception
        return self.scorer


class FakeCollection:
    """deterministic Chroma-like collection stub；只暴露 get()。"""

    def __init__(self, rows) -> None:
        self.rows = dict(rows)
        self.calls: list[dict] = []

    def get(self, ids=None, include=None, **kwargs):
        self.calls.append({"method": "get", "ids": list(ids or []), "include": list(include or [])})
        result_ids: list[str] = []
        documents: list[str | None] = []
        metadatas: list[dict | None] = []
        for chunk_id in ids or []:
            if chunk_id in self.rows:
                text, metadata = self.rows[chunk_id]
                result_ids.append(chunk_id)
                documents.append(text)
                metadatas.append(metadata)
        return {"ids": result_ids, "documents": documents, "metadatas": metadatas}


class ReorderedFakeCollection(FakeCollection):
    def get(self, ids=None, include=None, **kwargs):
        # 故意以与请求相反的顺序返回，验证 resolver 恢复输入顺序。
        ordered = list(reversed(ids or []))
        return super().get(ids=ordered, include=include, **kwargs)


def _row(chunk_id, document_id, content_hash, text):
    return {
        "chunk_id": chunk_id,
        "doc_id": document_id,
        "content_hash": content_hash,
        "source": f"{document_id}.md",
    }, text


def _chunks_map(*entries):
    rows = {}
    manifest = {}
    for chunk_id, document_id, content_hash, text in entries:
        metadata, _ = _row(chunk_id, document_id, content_hash, text)
        rows[chunk_id] = (text, metadata)
        manifest[chunk_id] = content_hash
    return rows, manifest


def _candidates(*triples) -> tuple[CeCandidate, ...]:
    return tuple(
        CeCandidate(
            document_id=document_id,
            chunk_id=chunk_id,
            content_hash=content_hash,
            pre_ce_rrf_rank=rank,
            rrf_score=1.0 / (60 + rank),
            source_channels=("current-dense-led-ranked.v1",),
            payload={"content_hash": content_hash, "source": f"{document_id}.md"},
        )
        for rank, (chunk_id, document_id, content_hash) in enumerate(triples, 1)
    )


def _standard_candidates() -> tuple[CeCandidate, ...]:
    return _candidates((CHUNK_A, DOC_A, HASH_A), (CHUNK_B, DOC_B, HASH_B), (CHUNK_C, DOC_C, HASH_C))


def _resolver_for(*entries, collection_cls=FakeCollection) -> ChromaTextResolver:
    rows, manifest = _chunks_map(*entries)
    return ChromaTextResolver(
        ChromaCollectionReader(collection_cls(rows)), manifest_content_hash=manifest
    )


def _make_asset_dir(tmp_path, *, model_ref="approved/cross-encoder@v1"):
    root = tmp_path / "asset"
    root.mkdir()
    (root / "config.json").write_text("{}", encoding="utf-8")
    (root / "tokenizer_config.json").write_text("{}", encoding="utf-8")
    (root / "tokenizer.json").write_text("{}", encoding="utf-8")
    (root / "model.safetensors").write_bytes(b"fake weights")
    digest = compute_asset_tree_sha256(root)
    return root, CrossEncoderAssetConfig(
        model_ref=model_ref,
        local_model_path=root,
        asset_tree_sha256=digest,
        max_length=512,
        truncation_policy="longest_first",
    )


def _make_reranker(
    tmp_path,
    *,
    loader=None,
    resolver=None,
    timeout=60.0,
    scores=None,
    delay=0.0,
    scorer_exception=None,
    scorer_oom=False,
):
    asset_root, config = _make_asset_dir(tmp_path)
    if loader is None:
        scorer = FakeScorer(scores=scores, exception=scorer_exception, delay=delay, oom=scorer_oom)
        loader = FakeLoader(scorer)
    if resolver is None:
        resolver = _resolver_for(
            (CHUNK_A, DOC_A, HASH_A, TEXT_A),
            (CHUNK_B, DOC_B, HASH_B, TEXT_B),
            (CHUNK_C, DOC_C, HASH_C, TEXT_C),
        )
    return CrossEncoderReranker(
        config=config,
        loader=loader,
        resolver=resolver,
        env=CeEnvironmentMeta(cache_identity="cache-key", cache_digests={"manifest_sha256": "m"}),
        ce_timeout_seconds=timeout,
    ), loader


# ---------------------------------------------------------------------------
# Asset validator
# ---------------------------------------------------------------------------


def test_asset_validator_missing_path_rejected(tmp_path) -> None:
    config = CrossEncoderAssetConfig(
        model_ref="approved/ce@v1",
        local_model_path=tmp_path / "missing",
        asset_tree_sha256="0" * 64,
        max_length=512,
        truncation_policy="longest_first",
    )
    with pytest.raises(CrossEncoderAssetError, match="MISSING"):
        validate_asset(config)


def test_asset_validator_not_directory(tmp_path) -> None:
    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    config = CrossEncoderAssetConfig(
        model_ref="approved/ce@v1",
        local_model_path=file_path,
        asset_tree_sha256="0" * 64,
        max_length=512,
        truncation_policy="longest_first",
    )
    with pytest.raises(CrossEncoderAssetError, match="NOT_DIRECTORY"):
        validate_asset(config)


def test_asset_validator_digest_mismatch(tmp_path) -> None:
    root, config = _make_asset_dir(tmp_path)
    bad = CrossEncoderAssetConfig(
        model_ref=config.model_ref,
        local_model_path=root,
        asset_tree_sha256="0" * 64,
        max_length=512,
        truncation_policy="longest_first",
    )
    with pytest.raises(CrossEncoderAssetError, match="DIGEST_MISMATCH"):
        validate_asset(bad)


def test_asset_validator_model_ref_must_not_be_basename(tmp_path) -> None:
    root, config = _make_asset_dir(tmp_path, model_ref="asset")
    with pytest.raises(CrossEncoderAssetError, match="MODEL_REF_INVALID"):
        validate_asset(config)


def test_asset_validator_missing_weight_file(tmp_path) -> None:
    root, config = _make_asset_dir(tmp_path)
    (root / "model.safetensors").unlink()
    bad = CrossEncoderAssetConfig(
        model_ref=config.model_ref,
        local_model_path=root,
        asset_tree_sha256=compute_asset_tree_sha256(root),
        max_length=512,
        truncation_policy="longest_first",
    )
    with pytest.raises(CrossEncoderAssetError, match="FILES_MISSING"):
        validate_asset(bad)


def test_asset_tree_digest_is_deterministic(tmp_path) -> None:
    root, config = _make_asset_dir(tmp_path)
    assert compute_asset_tree_sha256(root) == config.asset_tree_sha256
    assert compute_asset_tree_sha256(root) == compute_asset_tree_sha256(root)
    (root / "extra.bin").write_bytes(b"extra")
    assert compute_asset_tree_sha256(root) != config.asset_tree_sha256


# ---------------------------------------------------------------------------
# Candidate validation
# ---------------------------------------------------------------------------


def test_candidate_validation_rejects_duplicate_identity() -> None:
    candidates = _candidates((CHUNK_A, DOC_A, HASH_A), (CHUNK_A, DOC_A, HASH_A), (CHUNK_C, DOC_C, HASH_C))
    with pytest.raises(CrossEncoderCandidateError, match="DUPLICATE"):
        validate_candidates(candidates)


def test_candidate_validation_rejects_non_contiguous_rank() -> None:
    candidates = _candidates((CHUNK_A, DOC_A, HASH_A), (CHUNK_B, DOC_B, HASH_B), (CHUNK_C, DOC_C, HASH_C))
    bad = (
        candidates[0],
        CeCandidate(
            document_id=DOC_B,
            chunk_id=CHUNK_B,
            content_hash=HASH_B,
            pre_ce_rrf_rank=3,
            rrf_score=0.1,
        ),
    )
    with pytest.raises(CrossEncoderCandidateError, match="contiguous"):
        validate_candidates(bad)


def test_candidate_validation_rejects_missing_content_hash() -> None:
    candidate = CeCandidate(
        document_id=DOC_A, chunk_id=CHUNK_A, content_hash="", pre_ce_rrf_rank=1, rrf_score=0.1
    )
    with pytest.raises(CrossEncoderCandidateError, match="CONTENT_HASH_MISSING"):
        validate_candidates([candidate])


def test_candidate_validation_rejects_over_budget() -> None:
    candidates = _candidates(*[(f"chunk-{i}", f"doc-{i}", f"{i:040d}") for i in range(1, CE_CANDIDATE_LIMIT + 2)])
    with pytest.raises(CrossEncoderCandidateError, match="budget"):
        validate_candidates(candidates)


def test_candidate_validation_allows_empty() -> None:
    assert validate_candidates([]) == ()


# ---------------------------------------------------------------------------
# Score validation
# ---------------------------------------------------------------------------


def test_score_validation_rejects_bool() -> None:
    with pytest.raises(CrossEncoderScoreValidationError, match="BOOL"):
        validate_scores([True, 0.5], 2)


def test_score_validation_rejects_wrong_count() -> None:
    with pytest.raises(CrossEncoderScoreValidationError, match="COUNT"):
        validate_scores([0.1, 0.2], 3)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_score_validation_rejects_non_finite(bad) -> None:
    with pytest.raises(CrossEncoderScoreValidationError, match="NON_FINITE"):
        validate_scores([bad, 0.5], 2)


def test_score_validation_accepts_shape_n1() -> None:
    assert validate_scores([[0.1], [0.2]], 2) == [0.1, 0.2]


def test_score_validation_rejects_shape_n2() -> None:
    with pytest.raises(CrossEncoderScoreValidationError, match="SHAPE"):
        validate_scores([[0.1, 0.2]], 1)


# ---------------------------------------------------------------------------
# Deterministic reorder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_rerank_and_candidate_set_preserved(tmp_path) -> None:
    reranker, _loader = _make_reranker(
        tmp_path, scores=[0.5, 0.9, 0.7]
    )
    result = await reranker.rerank(
        query="query", candidates=_standard_candidates(), remaining_seconds=60.0, run_id="run-1"
    )
    assert result.status == "SUCCEEDED"
    assert [item.chunk_id for item in result.ranked] == [CHUNK_B, CHUNK_C, CHUNK_A]
    assert [item.post_ce_rank for item in result.ranked] == [1, 2, 3]
    assert [item.pre_ce_rrf_rank for item in result.ranked] == [2, 3, 1]
    assert {item.stable_identity for item in result.ranked} == {
        (DOC_A, CHUNK_A),
        (DOC_B, CHUNK_B),
        (DOC_C, CHUNK_C),
    }


@pytest.mark.asyncio
async def test_inverse_ordering(tmp_path) -> None:
    reranker, _loader = _make_reranker(tmp_path, scores=[0.1, 0.2, 0.3])
    result = await reranker.rerank(
        query="query", candidates=_standard_candidates(), remaining_seconds=60.0, run_id="run-1"
    )
    assert [item.chunk_id for item in result.ranked] == [CHUNK_C, CHUNK_B, CHUNK_A]


@pytest.mark.asyncio
async def test_exact_score_tie_uses_pre_ce_rank(tmp_path) -> None:
    reranker, _loader = _make_reranker(tmp_path, scores=[0.5, 0.5, 0.3])
    result = await reranker.rerank(
        query="query", candidates=_standard_candidates(), remaining_seconds=60.0, run_id="run-1"
    )
    # A 与 B 同分（完全相等才 tie），tie-break 用 pre-CE rank（A=1 在 B=2 前）。
    assert [item.chunk_id for item in result.ranked] == [CHUNK_A, CHUNK_B, CHUNK_C]


@pytest.mark.asyncio
async def test_near_equal_score_is_not_tie(tmp_path) -> None:
    reranker, _loader = _make_reranker(tmp_path, scores=[0.5, 0.5 + 1e-9, 0.3])
    result = await reranker.rerank(
        query="query", candidates=_standard_candidates(), remaining_seconds=60.0, run_id="run-1"
    )
    # B 略高 -> B 在前；A 与 B 不构成完全相等 tie。
    assert [item.chunk_id for item in result.ranked] == [CHUNK_B, CHUNK_A, CHUNK_C]


@pytest.mark.asyncio
async def test_one_candidate(tmp_path) -> None:
    reranker, loader = _make_reranker(tmp_path, scores=[0.9])
    candidate = _candidates((CHUNK_A, DOC_A, HASH_A))
    result = await reranker.rerank(query="q", candidates=candidate, remaining_seconds=60.0, run_id="r")
    assert result.status == "SUCCEEDED"
    assert len(result.ranked) == 1
    assert result.ranked[0].post_ce_rank == 1
    assert result.ranked[0].pre_ce_rrf_rank == 1
    assert loader.scorer.calls == 1


@pytest.mark.asyncio
async def test_empty_candidates_no_load_no_inference(tmp_path) -> None:
    reranker, loader = _make_reranker(tmp_path)
    result = await reranker.rerank(query="q", candidates=[], remaining_seconds=60.0, run_id="r")
    assert result.status == "EMPTY"
    assert result.ranked == ()
    assert loader.calls == 0
    assert loader.scorer.calls == 0
    assert reranker.model_state is CrossEncoderModelState.UNLOADED


# ---------------------------------------------------------------------------
# Failure: score / inference / candidate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_score_count_fails_closed(tmp_path) -> None:
    reranker, _loader = _make_reranker(tmp_path, scores=[0.1, 0.2])
    with pytest.raises(CrossEncoderScoreValidationError, match="COUNT"):
        await reranker.rerank(
            query="q", candidates=_standard_candidates(), remaining_seconds=60.0, run_id="r"
        )


@pytest.mark.asyncio
async def test_wrong_score_shape_fails_closed(tmp_path) -> None:
    reranker, _loader = _make_reranker(tmp_path, scores=[[0.1, 0.2]])
    with pytest.raises(CrossEncoderScoreValidationError, match="SHAPE"):
        await reranker.rerank(
            query="q", candidates=_standard_candidates(), remaining_seconds=60.0, run_id="r"
        )


@pytest.mark.asyncio
async def test_scorer_exception_fails_closed(tmp_path) -> None:
    reranker, _loader = _make_reranker(tmp_path, scorer_exception=RuntimeError("boom"))
    with pytest.raises(CrossEncoderInferenceError, match="INFERENCE_EXCEPTION"):
        await reranker.rerank(
            query="q", candidates=_standard_candidates(), remaining_seconds=60.0, run_id="r"
        )
    assert reranker.model_state is CrossEncoderModelState.READY


@pytest.mark.asyncio
async def test_model_load_failure_is_sticky(tmp_path) -> None:
    loader = FakeLoader(load_exception=CrossEncoderLoadError("CROSS_ENCODER_LOAD_FAILED"))
    reranker, _ = _make_reranker(tmp_path, loader=loader)
    with pytest.raises(CrossEncoderLoadError, match="LOAD_FAILED"):
        await reranker.rerank(query="q", candidates=_standard_candidates(), remaining_seconds=60.0, run_id="r")
    assert reranker.model_state is CrossEncoderModelState.FAILED
    with pytest.raises(CrossEncoderLoadError, match="sticky"):
        await reranker.rerank(query="q", candidates=_standard_candidates(), remaining_seconds=60.0, run_id="r")
    assert loader.calls == 1


@pytest.mark.asyncio
async def test_oom_is_sticky_failed(tmp_path) -> None:
    reranker, _loader = _make_reranker(tmp_path, scorer_oom=True)
    with pytest.raises(CrossEncoderOomError, match="OOM"):
        await reranker.rerank(query="q", candidates=_standard_candidates(), remaining_seconds=60.0, run_id="r")
    assert reranker.model_state is CrossEncoderModelState.FAILED
    with pytest.raises(CrossEncoderLoadError, match="OOM"):
        await reranker.rerank(query="q", candidates=_standard_candidates(), remaining_seconds=60.0, run_id="r")


@pytest.mark.asyncio
async def test_load_oom_is_sticky_failed(tmp_path) -> None:
    loader = FakeLoader(load_oom=True)
    reranker, _ = _make_reranker(tmp_path, loader=loader)
    with pytest.raises(CrossEncoderOomError, match="OOM"):
        await reranker.rerank(query="q", candidates=_standard_candidates(), remaining_seconds=60.0, run_id="r")
    assert reranker.model_state is CrossEncoderModelState.FAILED


# ---------------------------------------------------------------------------
# Executor: single load / backpressure / timeout / cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_first_load_only_once(tmp_path) -> None:
    loader = FakeLoader(FakeScorer(scores=[0.5, 0.6, 0.7], delay=0.4))
    reranker, _ = _make_reranker(tmp_path, loader=loader)
    candidates = _standard_candidates()

    async def one():
        return await reranker.rerank(query="q", candidates=candidates, remaining_seconds=60.0, run_id="a")

    results = await asyncio_gather(one(), one())
    outcomes = []
    for value in results:
        if isinstance(value, CrossEncoderBackpressureError):
            outcomes.append("backpressure")
        elif isinstance(value, CeRerankResult) and value.status == "SUCCEEDED":
            outcomes.append("succeeded")
        else:
            outcomes.append(type(value).__name__)
    assert loader.calls == 1
    assert "succeeded" in outcomes
    assert "backpressure" in outcomes


@pytest.mark.asyncio
async def test_executor_busy_immediate_backpressure(tmp_path) -> None:
    loader = FakeLoader(FakeScorer(scores=[0.5, 0.6, 0.7], delay=0.6))
    reranker, _ = _make_reranker(tmp_path, loader=loader)
    candidates = _standard_candidates()
    first = asyncio_create_task(
        reranker.rerank(query="q", candidates=candidates, remaining_seconds=60.0, run_id="a")
    )
    await asyncio_sleep(0.05)
    with pytest.raises(CrossEncoderBackpressureError, match="EXECUTOR_BUSY"):
        await reranker.rerank(query="q", candidates=candidates, remaining_seconds=60.0, run_id="b")
    await first
    # 慢任务结束后重新可提交。
    result = await reranker.rerank(query="q", candidates=candidates, remaining_seconds=60.0, run_id="c")
    assert result.status == "SUCCEEDED"


@pytest.mark.asyncio
async def test_timeout_produces_detach_truth_and_recovers(tmp_path) -> None:
    loader = FakeLoader(FakeScorer(scores=[0.5, 0.6, 0.7], delay=0.8))
    reranker, _ = _make_reranker(tmp_path, loader=loader, timeout=0.1)
    candidates = _standard_candidates()
    with pytest.raises(CrossEncoderTimeoutError, match="TIMEOUT") as exc_info:
        await reranker.rerank(query="q", candidates=candidates, remaining_seconds=60.0, run_id="a")
    detach = exc_info.value.detach
    assert detach is not None
    assert detach.worker_terminated is False
    assert detach.execution_detached is True
    assert detach.background_work_pending is True
    # detached worker 结束后，permit 释放，可再次提交（改用快速 scorer）。
    await asyncio_sleep(1.2)
    loader.scorer.delay = 0
    result = await reranker.rerank(query="q", candidates=candidates, remaining_seconds=60.0, run_id="b")
    assert result.status == "SUCCEEDED"


@pytest.mark.asyncio
async def test_cancellation_raises_typed_cancellation_with_detach_truth(tmp_path) -> None:
    loader = FakeLoader(FakeScorer(scores=[0.5, 0.6, 0.7], delay=2.0))
    reranker, _ = _make_reranker(tmp_path, loader=loader, timeout=60.0)
    candidates = _standard_candidates()
    task = asyncio_create_task(
        reranker.rerank(query="q", candidates=candidates, remaining_seconds=60.0, run_id="a")
    )
    await asyncio_sleep(0.1)
    task.cancel()
    with pytest.raises(CrossEncoderCancellationError, match="CANCELLED") as exc_info:
        await task
    detach = exc_info.value.detach
    assert detach is not None
    assert detach.execution_detached is True or detach.worker_terminated is True


@pytest.mark.asyncio
async def test_remaining_zero_is_timeout(tmp_path) -> None:
    reranker, _loader = _make_reranker(tmp_path, scores=[0.5, 0.6, 0.7])
    with pytest.raises(CrossEncoderTimeoutError, match="TIMEOUT"):
        await reranker.rerank(
            query="q", candidates=_standard_candidates(), remaining_seconds=0.0, run_id="r"
        )


# ---------------------------------------------------------------------------
# Text resolver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_missing_chunk_fails_closed(tmp_path) -> None:
    resolver = _resolver_for((CHUNK_A, DOC_A, HASH_A, TEXT_A), (CHUNK_B, DOC_B, HASH_B, TEXT_B))
    reranker, _ = _make_reranker(tmp_path, resolver=resolver, scores=[0.5, 0.6, 0.7])
    candidates = _candidates((CHUNK_A, DOC_A, HASH_A), (CHUNK_B, DOC_B, HASH_B), (CHUNK_C, DOC_C, HASH_C))
    with pytest.raises(CrossEncoderTextResolutionError, match="MISSING"):
        await reranker.rerank(query="q", candidates=candidates, remaining_seconds=60.0, run_id="r")


@pytest.mark.asyncio
async def test_resolver_duplicate_requested_chunk_fails_closed(tmp_path) -> None:
    candidates = _candidates((CHUNK_A, DOC_A, HASH_A), (CHUNK_A, DOC_A, HASH_A))
    reranker, _ = _make_reranker(
        tmp_path,
        resolver=_resolver_for((CHUNK_A, DOC_A, HASH_A, TEXT_A)),
        scores=[0.5, 0.6],
    )
    # duplicate identity 首先被 candidate validator 拦截。
    with pytest.raises(CrossEncoderCandidateError, match="DUPLICATE"):
        await reranker.rerank(query="q", candidates=candidates, remaining_seconds=60.0, run_id="r")


@pytest.mark.asyncio
async def test_resolver_document_identity_mismatch_fails_closed(tmp_path) -> None:
    rows, manifest = _chunks_map((CHUNK_A, DOC_A, HASH_A, TEXT_A))
    metadata, _ = _row(CHUNK_A, "wrong-doc", HASH_A, TEXT_A)
    rows[CHUNK_A] = (TEXT_A, metadata)
    resolver = ChromaTextResolver(
        ChromaCollectionReader(FakeCollection(rows)), manifest_content_hash=manifest
    )
    reranker, _ = _make_reranker(tmp_path, resolver=resolver, scores=[0.5])
    candidates = _candidates((CHUNK_A, DOC_A, HASH_A))
    with pytest.raises(CrossEncoderTextResolutionError, match="DOCUMENT_ID_MISMATCH"):
        await reranker.rerank(query="q", candidates=candidates, remaining_seconds=60.0, run_id="r")


@pytest.mark.asyncio
async def test_resolver_chunk_identity_mismatch_fails_closed(tmp_path) -> None:
    rows, manifest = _chunks_map((CHUNK_A, DOC_A, HASH_A, TEXT_A))
    metadata, _ = _row(CHUNK_A, DOC_A, HASH_A, TEXT_A)
    metadata["chunk_id"] = "other-chunk"
    rows[CHUNK_A] = (TEXT_A, metadata)
    resolver = ChromaTextResolver(
        ChromaCollectionReader(FakeCollection(rows)), manifest_content_hash=manifest
    )
    reranker, _ = _make_reranker(tmp_path, resolver=resolver, scores=[0.5])
    candidates = _candidates((CHUNK_A, DOC_A, HASH_A))
    with pytest.raises(CrossEncoderTextResolutionError, match="CHUNK_ID_MISMATCH"):
        await reranker.rerank(query="q", candidates=candidates, remaining_seconds=60.0, run_id="r")


@pytest.mark.asyncio
async def test_resolver_content_hash_mismatch_fails_closed(tmp_path) -> None:
    rows, manifest = _chunks_map((CHUNK_A, DOC_A, HASH_A, TEXT_A))
    metadata, _ = _row(CHUNK_A, DOC_A, HASH_A, TEXT_A)
    metadata["content_hash"] = "0" * 40
    rows[CHUNK_A] = (TEXT_A, metadata)
    resolver = ChromaTextResolver(
        ChromaCollectionReader(FakeCollection(rows)), manifest_content_hash=manifest
    )
    reranker, _ = _make_reranker(tmp_path, resolver=resolver, scores=[0.5])
    candidates = _candidates((CHUNK_A, DOC_A, HASH_A))
    with pytest.raises(CrossEncoderTextResolutionError, match="CONTENT_HASH_MISMATCH"):
        await reranker.rerank(query="q", candidates=candidates, remaining_seconds=60.0, run_id="r")


@pytest.mark.asyncio
async def test_resolver_manifest_missing_hash_fails_closed(tmp_path) -> None:
    rows, manifest = _chunks_map((CHUNK_A, DOC_A, HASH_A, TEXT_A))
    manifest.pop(CHUNK_A)
    resolver = ChromaTextResolver(
        ChromaCollectionReader(FakeCollection(rows)), manifest_content_hash=manifest
    )
    reranker, _ = _make_reranker(tmp_path, resolver=resolver, scores=[0.5])
    candidates = _candidates((CHUNK_A, DOC_A, HASH_A))
    with pytest.raises(CrossEncoderTextResolutionError, match="CONTENT_HASH_MISMATCH"):
        await reranker.rerank(query="q", candidates=candidates, remaining_seconds=60.0, run_id="r")


@pytest.mark.asyncio
async def test_resolver_restores_input_order_regardless_of_chroma_order(tmp_path) -> None:
    resolver = _resolver_for(
        (CHUNK_A, DOC_A, HASH_A, TEXT_A),
        (CHUNK_B, DOC_B, HASH_B, TEXT_B),
        (CHUNK_C, DOC_C, HASH_C, TEXT_C),
        collection_cls=ReorderedFakeCollection,
    )
    reranker, _ = _make_reranker(tmp_path, resolver=resolver, scores=[0.9, 0.7, 0.5])
    result = await reranker.rerank(
        query="q", candidates=_standard_candidates(), remaining_seconds=60.0, run_id="r"
    )
    # Chroma 返回逆序；resolver 恢复输入顺序（A,B,C），score 顺序保持 A,B,C。
    assert [item.chunk_id for item in result.ranked] == [CHUNK_A, CHUNK_B, CHUNK_C]


@pytest.mark.asyncio
async def test_resolved_text_sha256_correct_and_no_cache_write(tmp_path) -> None:
    rows, manifest = _chunks_map(
        (CHUNK_A, DOC_A, HASH_A, TEXT_A),
        (CHUNK_B, DOC_B, HASH_B, TEXT_B),
        (CHUNK_C, DOC_C, HASH_C, TEXT_C),
    )
    collection = FakeCollection(rows)
    resolver = ChromaTextResolver(ChromaCollectionReader(collection), manifest_content_hash=manifest)
    reranker, _ = _make_reranker(tmp_path, resolver=resolver, scores=[0.9, 0.7, 0.5])
    result = await reranker.rerank(
        query="q", candidates=_standard_candidates(), remaining_seconds=60.0, run_id="r"
    )
    by_chunk = {item.chunk_id: item for item in result.ranked}
    assert by_chunk[CHUNK_A].resolved_text_sha256 == _sha256_text(TEXT_A)
    assert by_chunk[CHUNK_B].resolved_text_sha256 == _sha256_text(TEXT_B)
    assert by_chunk[CHUNK_C].resolved_text_sha256 == _sha256_text(TEXT_C)
    assert collection.calls
    assert all(call["method"] == "get" for call in collection.calls)
    assert all(call["include"] == ["documents", "metadatas"] for call in collection.calls)


# ---------------------------------------------------------------------------
# P1-06 malformed Chroma parallel columns -> typed CrossEncoderTextResolutionError
# ---------------------------------------------------------------------------


class _MalformedColumnsReader:
    def __init__(self, documents, metadatas) -> None:
        self.documents = documents
        self.metadatas = metadatas

    def get(self, *, ids, include):
        return ChunkReadResult(
            ids=list(ids),
            documents=list(self.documents),
            metadatas=list(self.metadatas),
        )


def _malformed_resolver(documents, metadatas) -> ChromaTextResolver:
    return ChromaTextResolver(
        _MalformedColumnsReader(documents, metadatas), manifest_content_hash={}
    )


def test_resolver_malformed_parallel_columns_documents_short() -> None:
    candidates = _candidates((CHUNK_A, DOC_A, HASH_A), (CHUNK_B, DOC_B, HASH_B))
    resolver = _malformed_resolver([TEXT_A], [{}, {}])
    with pytest.raises(CrossEncoderTextResolutionError, match="inconsistent lengths"):
        resolver.resolve(candidates)


def test_resolver_malformed_parallel_columns_metadatas_short() -> None:
    candidates = _candidates((CHUNK_A, DOC_A, HASH_A), (CHUNK_B, DOC_B, HASH_B))
    resolver = _malformed_resolver([TEXT_A, TEXT_B], [{}])
    with pytest.raises(CrossEncoderTextResolutionError, match="inconsistent lengths"):
        resolver.resolve(candidates)


def test_reader_malformed_raw_parallel_columns_is_typed() -> None:
    class RawCollection:
        def get(self, ids=None, include=None, **kwargs):
            return {"ids": ["a", "b"], "documents": ["doc"], "metadatas": [{}, {}]}

    reader = ChromaCollectionReader(RawCollection())
    with pytest.raises(CrossEncoderTextResolutionError, match="inconsistent lengths"):
        reader.get(ids=["a", "b"], include=["documents", "metadatas"])


# ---------------------------------------------------------------------------
# Provenance safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provenance_is_safe_and_complete(tmp_path) -> None:
    reranker, _loader = _make_reranker(tmp_path, scores=[0.5, 0.9, 0.7])
    result = await reranker.rerank(
        query="sensitive query text", candidates=_standard_candidates(), remaining_seconds=60.0, run_id="run-1"
    )
    row = provenance_to_safe_row(result.provenance)
    assert row["schema_version"] == CE_PROVENANCE_SCHEMA_VERSION
    assert row["candidate_count"] == 3
    assert row["status"] == "SUCCEEDED"
    assert row["model_ref"] == "approved/cross-encoder@v1"
    assert row["device"] == "cpu"
    assert len(row["items"]) == 3
    item = row["items"][0]
    assert set(item) == {
        "document_id",
        "chunk_id",
        "content_hash",
        "resolved_text_sha256",
        "pre_ce_rrf_rank",
        "post_ce_rank",
        "cross_encoder_score",
    }
    encoded = str(row)
    assert "sensitive query text" not in encoded
    assert TEXT_A not in encoded
    assert "asset" not in encoded or "local_model_path" not in encoded
    assert "local_model_path" not in encoded
    assert row["latency_ms"]["model_load_latency_ms"] is not None


@pytest.mark.asyncio
async def test_empty_provenance_is_safe(tmp_path) -> None:
    reranker, _loader = _make_reranker(tmp_path)
    result = await reranker.rerank(query="EMPTY_SECRET_QUERY_TEXT", candidates=[], remaining_seconds=60.0, run_id="r")
    row = provenance_to_safe_row(result.provenance)
    assert row["status"] == "EMPTY"
    assert row["candidate_count"] == 0
    assert row["items"] == []
    assert "EMPTY_SECRET_QUERY_TEXT" not in str(row)


def test_deterministic_reorder_pure_function() -> None:
    candidates = _standard_candidates()
    resolved = (
        ResolvedChunkText(
            document_id=DOC_A, chunk_id=CHUNK_A, content_hash=HASH_A,
            resolved_text=TEXT_A, resolved_text_sha256=_sha256_text(TEXT_A), pre_ce_rrf_rank=1,
        ),
        ResolvedChunkText(
            document_id=DOC_B, chunk_id=CHUNK_B, content_hash=HASH_B,
            resolved_text=TEXT_B, resolved_text_sha256=_sha256_text(TEXT_B), pre_ce_rrf_rank=2,
        ),
        ResolvedChunkText(
            document_id=DOC_C, chunk_id=CHUNK_C, content_hash=HASH_C,
            resolved_text=TEXT_C, resolved_text_sha256=_sha256_text(TEXT_C), pre_ce_rrf_rank=3,
        ),
    )
    ranked = deterministic_reorder(candidates, resolved, [0.5, 0.9, 0.7])
    assert [item.chunk_id for item in ranked] == [CHUNK_B, CHUNK_C, CHUNK_A]


async def asyncio_gather(*tasks):
    return await asyncio.gather(*tasks, return_exceptions=True)


def asyncio_create_task(coro):
    return asyncio.get_running_loop().create_task(coro)


async def asyncio_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)