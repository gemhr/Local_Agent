"""Stage5 Phase3 WP3 evaluation-only Cross-Encoder reranking 基础设施。

P1：LocalAgent evaluation-only post-RRF reranker。只按 frozen Contract 在显式 CE
evaluation runtime 中装配；production composition（``server.py::lifespan()``）不得引用。

职责分离（避免单个超大 service class）：

- Candidate 纯校验与 budget 保持
- 确定性重排（同 N 候选的严格 permutation）
- READY Chroma/Dense 只读 text resolver
- 本地 asset validator 与 tree digest
- scorer/loader 最小注入 seam（fake deterministic tests，非 plugin framework）
- process-scoped lazy model lifecycle（UNLOADED -> LOADING -> READY | FAILED）
- evaluation-owned 1-worker/0-pending executor 协调（timeout/cancel/detach truth）
- score 校验（count/shape/finite/bool）
- safe provenance DTO 与 sidecar row 构造

真实模型当前不存在（MODEL_NOT_PRESENT）：本模块不下载、不访问网络、不选择模型。
真实 loader 保持 offline/local_files_only；资产缺失时 typed fail closed。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from core.runtime.blocking_executor import (
    BlockingExecutorAdmissionTimeout,
    BlockingExecutorClosedError,
    BlockingTaskKind,
    BlockingTaskWaitState,
    BoundedBlockingExecutor,
)

CE_PROVENANCE_SCHEMA_VERSION = "localagent-cross-encoder-provenance.v1"
CE_ALGORITHM_REF = "cross-encoder-rerank.v1"
TEXT_RESOLUTION_CONTRACT = "cross-encoder-text-resolution.v1"
CE_DEVICE = "cpu"
CE_CANDIDATE_LIMIT = 8

# ---------------------------------------------------------------------------
# 1. Safe error catalog（evaluation-only，低基数；不写正文/路径/raw exception）
# ---------------------------------------------------------------------------

CE_ERROR_ASSET_MISSING = "CROSS_ENCODER_ASSET_MISSING"
CE_ERROR_ASSET_PATH_INVALID = "CROSS_ENCODER_ASSET_PATH_INVALID"
CE_ERROR_ASSET_NOT_DIRECTORY = "CROSS_ENCODER_ASSET_NOT_DIRECTORY"
CE_ERROR_ASSET_DIGEST_MISMATCH = "CROSS_ENCODER_ASSET_DIGEST_MISMATCH"
CE_ERROR_ASSET_FILES_MISSING = "CROSS_ENCODER_ASSET_FILES_MISSING"
CE_ERROR_MODEL_REF_INVALID = "CROSS_ENCODER_MODEL_REF_INVALID"
CE_ERROR_CONFIG_INVALID = "CROSS_ENCODER_CONFIG_INVALID"
CE_ERROR_LOAD_FAILED = "CROSS_ENCODER_LOAD_FAILED"
CE_ERROR_OOM = "CROSS_ENCODER_OOM"
CE_ERROR_INFERENCE_EXCEPTION = "CROSS_ENCODER_INFERENCE_EXCEPTION"
CE_ERROR_TIMEOUT = "CROSS_ENCODER_TIMEOUT"
CE_ERROR_CANCELLED = "CROSS_ENCODER_CANCELLED"
CE_ERROR_CANDIDATE_INVALID = "CROSS_ENCODER_CANDIDATE_INVALID"
CE_ERROR_CANDIDATE_DUPLICATE = "CROSS_ENCODER_CANDIDATE_DUPLICATE"
CE_ERROR_CANDIDATE_CONTENT_HASH_MISSING = "CROSS_ENCODER_CANDIDATE_CONTENT_HASH_MISSING"
CE_ERROR_TEXT_RESOLUTION = "CROSS_ENCODER_TEXT_RESOLUTION"
CE_ERROR_TEXT_MISSING = "CROSS_ENCODER_TEXT_MISSING"
CE_ERROR_TEXT_DUPLICATE = "CROSS_ENCODER_TEXT_DUPLICATE"
CE_ERROR_TEXT_INVALID = "CROSS_ENCODER_TEXT_INVALID"
CE_ERROR_TEXT_CHUNK_ID_MISMATCH = "CROSS_ENCODER_TEXT_CHUNK_ID_MISMATCH"
CE_ERROR_TEXT_DOCUMENT_ID_MISMATCH = "CROSS_ENCODER_TEXT_DOCUMENT_ID_MISMATCH"
CE_ERROR_TEXT_CONTENT_HASH_MISMATCH = "CROSS_ENCODER_TEXT_CONTENT_HASH_MISMATCH"
CE_ERROR_SCORE_SHAPE = "CROSS_ENCODER_SCORE_SHAPE"
CE_ERROR_SCORE_COUNT = "CROSS_ENCODER_SCORE_COUNT"
CE_ERROR_SCORE_NON_FINITE = "CROSS_ENCODER_SCORE_NON_FINITE"
CE_ERROR_SCORE_BOOL = "CROSS_ENCODER_SCORE_BOOL"
CE_ERROR_EXECUTOR_BUSY = "CROSS_ENCODER_EXECUTOR_BUSY"
CE_ERROR_EXECUTOR_CLOSED = "CROSS_ENCODER_EXECUTOR_CLOSED"
CE_ERROR_MODEL_STATE = "CROSS_ENCODER_MODEL_STATE"

_CE_TERMINAL_STATUSES = frozenset(
    {"FAILED", "TIMED_OUT", "CANCELLED", "EMPTY", "SUCCEEDED"}
)


class CrossEncoderError(RuntimeError):
    """统一 typed failure；只携带 safe 低基数事实。"""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        detach: BlockingTaskWaitState | None = None,
        ce_latency_ms: float | None = None,
    ) -> None:
        self.code = code
        self.detach = detach
        self.ce_latency_ms = ce_latency_ms
        super().__init__(code if not message else f"{code}: {message}")


class CrossEncoderConfigError(CrossEncoderError):
    pass


class CrossEncoderAssetError(CrossEncoderError):
    pass


class CrossEncoderLoadError(CrossEncoderError):
    pass


class CrossEncoderOomError(CrossEncoderError):
    pass


class CrossEncoderInferenceError(CrossEncoderError):
    pass


class CrossEncoderScoreValidationError(CrossEncoderError):
    pass


class CrossEncoderCandidateError(CrossEncoderError):
    pass


class CrossEncoderTextResolutionError(CrossEncoderError):
    pass


class CrossEncoderBackpressureError(CrossEncoderError):
    pass


class CrossEncoderTimeoutError(CrossEncoderError):
    pass


class CrossEncoderCancellationError(CrossEncoderError):
    pass


def _is_oom(exc: BaseException) -> bool:
    return isinstance(exc, MemoryError) or type(exc).__name__ == "OutOfMemoryError"


def _validate_positive_finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CrossEncoderConfigError(CE_ERROR_CONFIG_INVALID, f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise CrossEncoderConfigError(CE_ERROR_CONFIG_INVALID, f"{name} must be positive finite")
    return number


# ---------------------------------------------------------------------------
# 2. Candidate DTO / validation（纯逻辑）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CeCandidate:
    document_id: str
    chunk_id: str
    content_hash: str
    pre_ce_rrf_rank: int
    rrf_score: float
    source_channels: tuple[str, ...] = ()
    payload: Any = None

    @property
    def stable_identity(self) -> tuple[str, str]:
        return self.document_id, self.chunk_id


@dataclass(frozen=True, slots=True)
class CeRankedCandidate:
    document_id: str
    chunk_id: str
    content_hash: str
    resolved_text_sha256: str
    pre_ce_rrf_rank: int
    post_ce_rank: int
    cross_encoder_score: float
    rrf_score: float
    source_channels: tuple[str, ...] = ()
    payload: Any = None

    @property
    def stable_identity(self) -> tuple[str, str]:
        return self.document_id, self.chunk_id


def validate_candidates(
    candidates: Sequence[CeCandidate], *, candidate_limit: int = CE_CANDIDATE_LIMIT
) -> tuple[CeCandidate, ...]:
    """校验 CE 输入：0<=N<=8、identity 唯一、rank 从 1 连续、rrf_score 有限、content_hash 存在。"""
    items = tuple(candidates)
    if len(items) > candidate_limit:
        raise CrossEncoderCandidateError(
            CE_ERROR_CANDIDATE_INVALID, f"candidate budget exceeded ({len(items)}>{candidate_limit})"
        )
    if len(items) > 0:
        seen: set[tuple[str, str]] = set()
        for item in items:
            if not item.document_id or not item.chunk_id:
                raise CrossEncoderCandidateError(CE_ERROR_CANDIDATE_INVALID, "empty identity")
            if not item.content_hash:
                raise CrossEncoderCandidateError(
                    CE_ERROR_CANDIDATE_CONTENT_HASH_MISSING, "candidate content_hash missing"
                )
            if isinstance(item.pre_ce_rrf_rank, bool) or not isinstance(item.pre_ce_rrf_rank, int) or item.pre_ce_rrf_rank <= 0:
                raise CrossEncoderCandidateError(CE_ERROR_CANDIDATE_INVALID, "invalid pre-CE rank")
            if not math.isfinite(float(item.rrf_score)):
                raise CrossEncoderCandidateError(CE_ERROR_CANDIDATE_INVALID, "non-finite rrf_score")
            identity = item.stable_identity
            if identity in seen:
                raise CrossEncoderCandidateError(CE_ERROR_CANDIDATE_DUPLICATE, "duplicate identity")
            seen.add(identity)
        ranks = [item.pre_ce_rrf_rank for item in items]
        if ranks != list(range(1, len(items) + 1)):
            raise CrossEncoderCandidateError(
                CE_ERROR_CANDIDATE_INVALID, "pre-CE ranks must be contiguous and start at 1"
            )
    return items


# ---------------------------------------------------------------------------
# 3. 确定性重排（同 N identity 集合的严格 permutation）
# ---------------------------------------------------------------------------


def deterministic_reorder(
    candidates: Sequence[CeCandidate],
    resolved: Sequence["ResolvedChunkText"],
    scores: Sequence[float],
) -> tuple[CeRankedCandidate, ...]:
    """sort key：-cross_encoder_score, pre_ce_rrf_rank, document_id, chunk_id。

    只对完全相等的 float score 视为 tie；不 round、不 epsilon bucket、不 threshold。
    """
    if len(candidates) != len(resolved) or len(candidates) != len(scores):
        raise CrossEncoderScoreValidationError(
            CE_ERROR_SCORE_COUNT, "candidate/resolved/scores length mismatch"
        )
    indexed: list[tuple[float, int, str, str, CeCandidate, ResolvedChunkText, float]] = []
    for candidate, text, score in zip(candidates, resolved, scores, strict=True):
        indexed.append(
            (
                -float(score),
                candidate.pre_ce_rrf_rank,
                candidate.document_id,
                candidate.chunk_id,
                candidate,
                text,
                float(score),
            )
        )
    indexed.sort(key=lambda value: (value[0], value[1], value[2], value[3]))
    return tuple(
        CeRankedCandidate(
            document_id=candidate.document_id,
            chunk_id=candidate.chunk_id,
            content_hash=candidate.content_hash,
            resolved_text_sha256=text.resolved_text_sha256,
            pre_ce_rrf_rank=candidate.pre_ce_rrf_rank,
            post_ce_rank=position,
            cross_encoder_score=score,
            rrf_score=candidate.rrf_score,
            source_channels=candidate.source_channels,
            payload=candidate.payload,
        )
        for position, (_neg, _rank, _doc, _chunk, candidate, text, score) in enumerate(
            indexed, 1
        )
    )


# ---------------------------------------------------------------------------
# 4. Score 校验（纯逻辑）
# ---------------------------------------------------------------------------


def _to_float(item: Any) -> float:
    if isinstance(item, bool):
        raise CrossEncoderScoreValidationError(CE_ERROR_SCORE_BOOL, "bool score rejected")
    try:
        return float(item)
    except (TypeError, ValueError) as exc:
        raise CrossEncoderScoreValidationError(CE_ERROR_SCORE_SHAPE, "non-numeric score") from exc


def _flatten_scores(value: Any) -> list[float]:
    if isinstance(value, bool):
        raise CrossEncoderScoreValidationError(CE_ERROR_SCORE_BOOL, "bool score rejected")
    if isinstance(value, (int, float)):
        return [float(value)]
    if hasattr(value, "shape"):
        shape = tuple(value.shape)
        if len(shape) == 0:
            return [_to_float(value)]
        if len(shape) == 1:
            return [_to_float(item) for item in value.tolist()]
        if len(shape) == 2 and shape[1] == 1:
            return [_to_float(item) for item in value[:, 0].tolist()]
        raise CrossEncoderScoreValidationError(CE_ERROR_SCORE_SHAPE, f"unexpected shape {shape}")
    try:
        values = list(value)
    except TypeError as exc:
        raise CrossEncoderScoreValidationError(CE_ERROR_SCORE_SHAPE, "not a sequence") from exc
    if not values:
        return []
    if isinstance(values[0], (list, tuple)):
        for row in values:
            if not isinstance(row, (list, tuple)) or len(row) != 1:
                raise CrossEncoderScoreValidationError(CE_ERROR_SCORE_SHAPE, "invalid 2D shape")
        return [_to_float(row[0]) for row in values]
    return [_to_float(item) for item in values]


def validate_scores(scores: Any, expected_count: int) -> list[float]:
    """规范化后必须是 (N,)：恰好 N 个 finite real scalar；bool 拒绝。"""
    flattened = _flatten_scores(scores)
    if len(flattened) != expected_count:
        raise CrossEncoderScoreValidationError(
            CE_ERROR_SCORE_COUNT, f"score count {len(flattened)} != {expected_count}"
        )
    for index, value in enumerate(flattened):
        if not math.isfinite(value):
            raise CrossEncoderScoreValidationError(
                CE_ERROR_SCORE_NON_FINITE, f"non-finite score at {index}"
            )
    return flattened


# ---------------------------------------------------------------------------
# 5. READY Chroma/Dense 只读 text resolver
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedChunkText:
    document_id: str
    chunk_id: str
    content_hash: str
    resolved_text: str
    resolved_text_sha256: str
    pre_ce_rrf_rank: int


@dataclass(frozen=True, slots=True)
class ChunkReadResult:
    ids: list[str]
    documents: list[str | None]
    metadatas: list[Mapping[str, Any] | None]


class ChunkCollectionReader(Protocol):
    """Chroma collection.get 的最薄只读视图；tests 注入 deterministic stub。"""

    def get(self, *, ids: list[str], include: list[str]) -> ChunkReadResult: ...


class ChromaCollectionReader:
    """包装真实 chromadb-like collection（``collection.get(ids=..., include=[...])``）。"""

    def __init__(self, collection: Any) -> None:
        self._collection = collection

    def get(self, *, ids: list[str], include: list[str]) -> ChunkReadResult:
        try:
            raw = self._collection.get(ids=ids, include=include)
        except Exception as exc:
            raise CrossEncoderTextResolutionError(
                CE_ERROR_TEXT_RESOLUTION, "collection lookup failed"
            ) from exc
        if not isinstance(raw, Mapping) or "ids" not in raw or not isinstance(raw["ids"], list):
            raise CrossEncoderTextResolutionError(CE_ERROR_TEXT_RESOLUTION, "invalid collection response")
        raw_ids = list(raw["ids"])
        raw_documents = raw.get("documents")
        raw_metadatas = raw.get("metadatas")
        if not isinstance(raw_documents, list) or not isinstance(raw_metadatas, list):
            raise CrossEncoderTextResolutionError(
                CE_ERROR_TEXT_RESOLUTION, "collection response columns missing"
            )
        if not (len(raw_ids) == len(raw_documents) == len(raw_metadatas)):
            raise CrossEncoderTextResolutionError(
                CE_ERROR_TEXT_RESOLUTION, "collection response parallel columns have inconsistent lengths"
            )
        return ChunkReadResult(
            ids=[str(item) for item in raw_ids],
            documents=[item for item in raw_documents],
            metadatas=[item for item in raw_metadatas],
        )


class ChromaTextResolver:
    """batch 只读解析：按 chunk_id 读取 exact page_content + metadata，逐项 fail-closed 校验。"""

    contract = TEXT_RESOLUTION_CONTRACT

    def __init__(
        self,
        reader: ChunkCollectionReader,
        manifest_content_hash: Mapping[str, str],
    ) -> None:
        self._reader = reader
        self._manifest_content_hash = dict(manifest_content_hash)

    def resolve(self, candidates: Sequence[CeCandidate]) -> tuple[ResolvedChunkText, ...]:
        if not candidates:
            return ()
        requested = [candidate.chunk_id for candidate in candidates]
        if len(set(requested)) != len(requested):
            raise CrossEncoderTextResolutionError(CE_ERROR_TEXT_DUPLICATE, "duplicate requested chunk_id")
        result = self._reader.get(ids=requested, include=["documents", "metadatas"])
        if not (len(result.ids) == len(result.documents) == len(result.metadatas)):
            raise CrossEncoderTextResolutionError(
                CE_ERROR_TEXT_RESOLUTION, "collection response parallel columns have inconsistent lengths"
            )
        if len(result.ids) != len(requested):
            raise CrossEncoderTextResolutionError(
                CE_ERROR_TEXT_MISSING,
                f"returned {len(result.ids)} rows for {len(requested)} requested chunk_ids",
            )
        if len(set(result.ids)) != len(result.ids):
            raise CrossEncoderTextResolutionError(CE_ERROR_TEXT_DUPLICATE, "duplicate returned row")
        returned_set = set(result.ids)
        if returned_set != set(requested):
            raise CrossEncoderTextResolutionError(
                CE_ERROR_TEXT_MISSING, "returned row set does not match requested chunk_ids"
            )
        by_chunk: dict[str, tuple[str | None, Mapping[str, Any] | None]] = {}
        for chunk_id, document, metadata in zip(result.ids, result.documents, result.metadatas, strict=True):
            by_chunk[chunk_id] = (document, metadata)

        resolved: list[ResolvedChunkText] = []
        for candidate in candidates:
            entry = by_chunk.get(candidate.chunk_id)
            if entry is None:
                raise CrossEncoderTextResolutionError(CE_ERROR_TEXT_MISSING, "chunk_id missing")
            text, metadata = entry
            metadata = metadata or {}
            if not isinstance(text, str) or not text.strip():
                raise CrossEncoderTextResolutionError(CE_ERROR_TEXT_INVALID, "page_content empty")
            if str(metadata.get("chunk_id")) != candidate.chunk_id:
                raise CrossEncoderTextResolutionError(
                    CE_ERROR_TEXT_CHUNK_ID_MISMATCH, "metadata chunk_id mismatch"
                )
            if str(metadata.get("doc_id")) != candidate.document_id:
                raise CrossEncoderTextResolutionError(
                    CE_ERROR_TEXT_DOCUMENT_ID_MISMATCH, "metadata doc_id mismatch"
                )
            manifest_hash = self._manifest_content_hash.get(candidate.chunk_id)
            if manifest_hash is None:
                raise CrossEncoderTextResolutionError(
                    CE_ERROR_TEXT_CONTENT_HASH_MISMATCH, "manifest content_hash missing"
                )
            chroma_hash = str(metadata.get("content_hash") or "")
            if not chroma_hash or manifest_hash != candidate.content_hash or chroma_hash != candidate.content_hash:
                raise CrossEncoderTextResolutionError(
                    CE_ERROR_TEXT_CONTENT_HASH_MISMATCH,
                    "candidate/manifest/Chroma content_hash mismatch",
                )
            resolved.append(
                ResolvedChunkText(
                    document_id=candidate.document_id,
                    chunk_id=candidate.chunk_id,
                    content_hash=candidate.content_hash,
                    resolved_text=text,
                    resolved_text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    pre_ce_rrf_rank=candidate.pre_ce_rrf_rank,
                )
            )
        return tuple(resolved)


# ---------------------------------------------------------------------------
# 6. Asset validator 与 tree digest（不加载模型）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CrossEncoderAssetConfig:
    model_ref: str
    local_model_path: Path
    asset_tree_sha256: str
    max_length: int
    truncation_policy: str

    def __post_init__(self) -> None:
        if not isinstance(self.model_ref, str) or not self.model_ref.strip():
            raise CrossEncoderConfigError(CE_ERROR_MODEL_REF_INVALID, "model_ref empty")
        if isinstance(self.max_length, bool) or not isinstance(self.max_length, int) or self.max_length <= 0:
            raise CrossEncoderConfigError(CE_ERROR_CONFIG_INVALID, "max_length must be a positive integer")
        if not isinstance(self.truncation_policy, str) or not self.truncation_policy.strip():
            raise CrossEncoderConfigError(CE_ERROR_CONFIG_INVALID, "truncation_policy empty")
        if not isinstance(self.asset_tree_sha256, str) or len(self.asset_tree_sha256) != 64:
            raise CrossEncoderConfigError(CE_ERROR_ASSET_DIGEST_MISMATCH, "invalid asset_tree_sha256")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def compute_asset_tree_sha256(root: Path) -> str:
    """目录内全部 regular files：[(relative_path, size, sha256)] 排序后 canonical JSON 的 SHA-256。"""
    entries: list[list[str | int]] = []
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            entries.append([rel, path.stat().st_size, _sha256_file(path)])
    entries.sort()
    canonical = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_REQUIRED_ASSET_FILES = frozenset({"config.json", "tokenizer_config.json"})
_REQUIRED_TOKENIZER_FILES = frozenset({"tokenizer.json", "tokenizer_config.json"})
_REQUIRED_WEIGHT_FILES = frozenset({"model.safetensors", "pytorch_model.bin"})


def validate_asset(config: CrossEncoderAssetConfig) -> Path:
    """filesystem 级校验；不 import/load CrossEncoder；远程查询禁止。"""
    raw = Path(config.local_model_path)
    if not raw.exists():
        raise CrossEncoderAssetError(CE_ERROR_ASSET_MISSING, "asset path missing")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise CrossEncoderAssetError(CE_ERROR_ASSET_PATH_INVALID, "cannot resolve asset path") from exc
    if not resolved.is_dir():
        raise CrossEncoderAssetError(CE_ERROR_ASSET_NOT_DIRECTORY, "asset is not a directory")
    if config.model_ref.strip() == resolved.name:
        raise CrossEncoderAssetError(CE_ERROR_MODEL_REF_INVALID, "model_ref must not equal path basename")
    computed = compute_asset_tree_sha256(resolved)
    if computed != config.asset_tree_sha256:
        raise CrossEncoderAssetError(
            CE_ERROR_ASSET_DIGEST_MISMATCH, "asset tree digest does not match approved value"
        )
    names = {path.name for path in resolved.iterdir() if path.is_file()}
    missing = sorted(_REQUIRED_ASSET_FILES - names)
    if missing:
        raise CrossEncoderAssetError(CE_ERROR_ASSET_FILES_MISSING, f"missing required files: {','.join(missing)}")
    if not (names & _REQUIRED_TOKENIZER_FILES):
        raise CrossEncoderAssetError(CE_ERROR_ASSET_FILES_MISSING, "tokenizer files missing")
    if not (names & _REQUIRED_WEIGHT_FILES):
        raise CrossEncoderAssetError(CE_ERROR_ASSET_FILES_MISSING, "model weight files missing")
    return resolved


# ---------------------------------------------------------------------------
# 7. scorer/loader 注入 seam + 真实 offline loader
# ---------------------------------------------------------------------------


class CrossEncoderScorer(Protocol):
    def score(self, query: str, texts: Sequence[str]) -> Sequence[float]: ...


class CrossEncoderLoader(Protocol):
    def load(self, config: CrossEncoderAssetConfig) -> CrossEncoderScorer: ...


class _SentenceTransformerScorer:
    """真实 CrossEncoder.predict 适配；shape 规范化 (N,)/(N,1) -> N scalars。"""

    def __init__(self, model: Any, *, max_batch_size: int, truncation_policy: str) -> None:
        self._model = model
        self._max_batch_size = max(1, int(max_batch_size))
        self._truncation_policy = truncation_policy

    def score(self, query: str, texts: Sequence[str]) -> Sequence[float]:
        if not texts:
            return []
        pairs = [(query, text) for text in texts]
        try:
            result = self._model.predict(
                pairs,
                batch_size=min(self._max_batch_size, len(texts)),
                show_progress_bar=False,
                convert_to_numpy=True,
            )
        except Exception as exc:
            if _is_oom(exc):
                raise CrossEncoderOomError(CE_ERROR_OOM, "inference OOM") from exc
            raise CrossEncoderInferenceError(CE_ERROR_INFERENCE_EXCEPTION) from exc
        try:
            return _flatten_scores(result)
        except CrossEncoderScoreValidationError as exc:
            raise CrossEncoderInferenceError(CE_ERROR_SCORE_SHAPE, str(exc)) from exc


class SentenceTransformerCrossEncoderLoader:
    """offline/local_files_only 的真实 loader；不在 module 顶层 import torch/ST（重依赖）。"""

    def __init__(self, *, max_batch_size: int = 8) -> None:
        self._max_batch_size = max(1, int(max_batch_size))

    def load(self, config: CrossEncoderAssetConfig) -> CrossEncoderScorer:
        resolved = validate_asset(config)
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        from sentence_transformers import CrossEncoder

        try:
            model = CrossEncoder(
                str(resolved),
                device=CE_DEVICE,
                local_files_only=True,
                max_length=config.max_length,
                trust_remote_code=False,
            )
        except Exception as exc:
            if _is_oom(exc):
                raise CrossEncoderOomError(CE_ERROR_OOM, "load OOM") from exc
            raise CrossEncoderLoadError(CE_ERROR_LOAD_FAILED) from exc
        return _SentenceTransformerScorer(
            model, max_batch_size=self._max_batch_size, truncation_policy=config.truncation_policy
        )


# ---------------------------------------------------------------------------
# 8. process-scoped lazy model lifecycle
# ---------------------------------------------------------------------------


class CrossEncoderModelState(str, Enum):
    UNLOADED = "UNLOADED"
    LOADING = "LOADING"
    READY = "READY"
    FAILED = "FAILED"


class CrossEncoderModelOwner:
    """UNLOADED -> LOADING -> READY | FAILED；load failure sticky，同进程不自动 retry。"""

    def __init__(self, config: CrossEncoderAssetConfig, loader: CrossEncoderLoader) -> None:
        self._config = config
        self._loader = loader
        self._state = CrossEncoderModelState.UNLOADED
        self._scorer: CrossEncoderScorer | None = None
        self._failure_code: str | None = None
        self._load_latency_ms: float | None = None
        self._lock = threading.RLock()

    @property
    def state(self) -> CrossEncoderModelState:
        with self._lock:
            return self._state

    @property
    def load_latency_ms(self) -> float | None:
        with self._lock:
            return self._load_latency_ms

    def mark_failed(self, code: str) -> None:
        with self._lock:
            self._state = CrossEncoderModelState.FAILED
            self._failure_code = code

    def ensure_loaded(self) -> tuple[CrossEncoderScorer, float | None]:
        """返回 (scorer, load_latency_ms_or_None)；并发进程内只允许一次真实 load。"""
        with self._lock:
            if self._state is CrossEncoderModelState.FAILED:
                raise CrossEncoderLoadError(
                    self._failure_code or CE_ERROR_LOAD_FAILED,
                    "model state is sticky FAILED; restart process after fixing the asset",
                )
            if self._state is CrossEncoderModelState.READY:
                return self._scorer, None  # type: ignore[return-value]
            if self._state is CrossEncoderModelState.LOADING:
                raise CrossEncoderLoadError(CE_ERROR_LOAD_FAILED, "concurrent load is not permitted")
            self._state = CrossEncoderModelState.LOADING
        started = time.perf_counter_ns()
        try:
            scorer = self._loader.load(self._config)
        except CrossEncoderOomError:
            self.mark_failed(CE_ERROR_OOM)
            raise
        except CrossEncoderError as exc:
            self.mark_failed(exc.code)
            raise
        except Exception as exc:
            self.mark_failed(CE_ERROR_LOAD_FAILED)
            raise CrossEncoderLoadError(CE_ERROR_LOAD_FAILED) from exc
        with self._lock:
            self._scorer = scorer
            self._state = CrossEncoderModelState.READY
            self._load_latency_ms = (time.perf_counter_ns() - started) / 1_000_000
        return scorer, self._load_latency_ms


# ---------------------------------------------------------------------------
# 9. safe provenance DTO / row 构造（不写 query 原文、text、本地路径、raw exception）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CeItemProvenance:
    document_id: str
    chunk_id: str
    content_hash: str
    resolved_text_sha256: str
    pre_ce_rrf_rank: int
    post_ce_rank: int
    cross_encoder_score: float


@dataclass(frozen=True, slots=True)
class CeLatency:
    model_load_latency_ms: float | None = None
    inference_latency_ms: float | None = None
    ce_total_latency_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class CeEnvironmentMeta:
    cache_identity: str = ""
    cache_digests: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CeProvenanceRecord:
    schema_version: str = CE_PROVENANCE_SCHEMA_VERSION
    query_sha256: str = ""
    algorithm_ref: str = CE_ALGORITHM_REF
    model_ref: str = ""
    asset_tree_sha256: str = ""
    device: str = CE_DEVICE
    cache_identity: str = ""
    cache_digests: Mapping[str, str] = field(default_factory=dict)
    candidate_count: int = 0
    status: str = "SUCCEEDED"
    safe_code: str | None = None
    items: tuple[CeItemProvenance, ...] = ()
    latency: CeLatency = field(default_factory=CeLatency)
    detach: BlockingTaskWaitState | None = None


def build_success_provenance(
    *,
    query_sha256: str,
    config: CrossEncoderAssetConfig,
    env: CeEnvironmentMeta,
    ranked: Sequence[CeRankedCandidate],
    latency: CeLatency,
) -> CeProvenanceRecord:
    return CeProvenanceRecord(
        query_sha256=query_sha256,
        model_ref=config.model_ref,
        asset_tree_sha256=config.asset_tree_sha256,
        cache_identity=env.cache_identity,
        cache_digests=dict(env.cache_digests),
        candidate_count=len(ranked),
        status="SUCCEEDED",
        items=tuple(
            CeItemProvenance(
                document_id=item.document_id,
                chunk_id=item.chunk_id,
                content_hash=item.content_hash,
                resolved_text_sha256=item.resolved_text_sha256,
                pre_ce_rrf_rank=item.pre_ce_rrf_rank,
                post_ce_rank=item.post_ce_rank,
                cross_encoder_score=item.cross_encoder_score,
            )
            for item in ranked
        ),
        latency=latency,
    )


def build_empty_provenance(
    *,
    query_sha256: str,
    config: CrossEncoderAssetConfig,
    env: CeEnvironmentMeta,
) -> CeProvenanceRecord:
    return CeProvenanceRecord(
        query_sha256=query_sha256,
        model_ref=config.model_ref,
        asset_tree_sha256=config.asset_tree_sha256,
        cache_identity=env.cache_identity,
        cache_digests=dict(env.cache_digests),
        candidate_count=0,
        status="EMPTY",
        latency=CeLatency(ce_total_latency_ms=0.0),
    )


def build_failure_provenance(
    *,
    query_sha256: str,
    config: CrossEncoderAssetConfig,
    env: CeEnvironmentMeta,
    candidate_count: int,
    status: str,
    error: CrossEncoderError,
    ce_total_latency_ms: float,
) -> CeProvenanceRecord:
    return CeProvenanceRecord(
        query_sha256=query_sha256,
        model_ref=config.model_ref,
        asset_tree_sha256=config.asset_tree_sha256,
        cache_identity=env.cache_identity,
        cache_digests=dict(env.cache_digests),
        candidate_count=candidate_count,
        status=status,
        safe_code=error.code,
        latency=CeLatency(ce_total_latency_ms=ce_total_latency_ms),
        detach=error.detach,
    )


def _safe_detach(detach: BlockingTaskWaitState | None) -> dict[str, bool] | None:
    if detach is None:
        return None
    return {
        "worker_terminated": detach.worker_terminated,
        "execution_detached": detach.execution_detached,
        "background_work_pending": detach.background_work_pending,
    }


def provenance_to_safe_row(record: CeProvenanceRecord) -> dict[str, Any]:
    """序列化 safe sidecar row；绝不包含 query 原文 / chunk text / 本地路径 / raw exception。"""
    row: dict[str, Any] = {
        "schema_version": record.schema_version,
        "query_sha256": record.query_sha256,
        "algorithm_ref": record.algorithm_ref,
        "model_ref": record.model_ref,
        "asset_tree_sha256": record.asset_tree_sha256,
        "device": record.device,
        "cache_identity": record.cache_identity,
        "cache_digests": dict(record.cache_digests),
        "candidate_count": record.candidate_count,
        "status": record.status,
        "safe_code": record.safe_code,
        "latency_ms": {
            "model_load_latency_ms": record.latency.model_load_latency_ms,
            "inference_latency_ms": record.latency.inference_latency_ms,
            "ce_total_latency_ms": record.latency.ce_total_latency_ms,
        },
        "detach": _safe_detach(record.detach),
        "items": [
            {
                "document_id": item.document_id,
                "chunk_id": item.chunk_id,
                "content_hash": item.content_hash,
                "resolved_text_sha256": item.resolved_text_sha256,
                "pre_ce_rrf_rank": item.pre_ce_rrf_rank,
                "post_ce_rank": item.post_ce_rank,
                "cross_encoder_score": item.cross_encoder_score,
            }
            for item in record.items
        ],
    }
    return row


# ---------------------------------------------------------------------------
# 10. orchestration：evaluation-owned 1-worker/0-pending executor + timeout/cancel
# ---------------------------------------------------------------------------


class CrossEncoderReranker:
    """post-RRF 显式 CE 重排；只装配进 evaluation runtime，不进入 production composition。"""

    def __init__(
        self,
        *,
        config: CrossEncoderAssetConfig,
        loader: CrossEncoderLoader,
        resolver: ChromaTextResolver,
        env: CeEnvironmentMeta,
        executor: BoundedBlockingExecutor | None = None,
        ce_timeout_seconds: float = 60.0,
    ) -> None:
        validate_asset(config)  # fail closed at startup；不 load 模型
        self._config = config
        self._loader = loader
        self._resolver = resolver
        self._env = env
        self._ce_timeout_seconds = _validate_positive_finite(ce_timeout_seconds, "ce_timeout_seconds")
        self._executor = executor or BoundedBlockingExecutor(
            max_workers=1, max_pending_tasks=0, thread_name_prefix="ce-eval"
        )
        self._owner = CrossEncoderModelOwner(config, loader)

    @property
    def model_state(self) -> CrossEncoderModelState:
        return self._owner.state

    @property
    def env(self) -> CeEnvironmentMeta:
        return self._env

    @property
    def config(self) -> CrossEncoderAssetConfig:
        return self._config

    def close(self) -> None:
        """先关 admission，再有界等待 worker；detached work 由进程退出作为最终边界。"""
        self._executor.close_admission()
        self._executor.shutdown(wait=True, timeout=5.0)

    @staticmethod
    def _cancellation_check() -> None:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError

    async def rerank(
        self,
        *,
        query: str,
        candidates: Sequence[CeCandidate],
        remaining_seconds: float,
        run_id: str,
    ) -> CeRerankResult:
        """返回 CE 重排后的同 N 候选；N=0 直接 EMPTY（不 load / 不 infer）。"""
        query_sha256 = hashlib.sha256(query.encode("utf-8")).hexdigest()
        validated = validate_candidates(candidates)
        if not validated:
            return CeRerankResult(
                status="EMPTY",
                ranked=(),
                provenance=build_empty_provenance(
                    query_sha256=query_sha256, config=self._config, env=self._env
                ),
            )
        if isinstance(remaining_seconds, bool) or not isinstance(remaining_seconds, (int, float)):
            raise CrossEncoderTimeoutError(CE_ERROR_TIMEOUT, "invalid remaining_seconds")
        if float(remaining_seconds) <= 0:
            raise CrossEncoderTimeoutError(CE_ERROR_TIMEOUT, "outer deadline already expired")
        total_started = time.perf_counter_ns()
        effective = min(self._ce_timeout_seconds, float(remaining_seconds))
        try:
            handle = self._executor.submit_nowait(
                self._run_worker(query, query_sha256, validated),
                kind=BlockingTaskKind.RERANK,
                run_id=run_id,
                operation_id=f"ce-rerank:{run_id}",
                cancellation_check=self._cancellation_check,
            )
        except BlockingExecutorAdmissionTimeout as exc:
            raise CrossEncoderBackpressureError(
                CE_ERROR_EXECUTOR_BUSY, ce_latency_ms=self._elapsed_ms(total_started)
            ) from exc
        except BlockingExecutorClosedError as exc:
            raise CrossEncoderBackpressureError(
                CE_ERROR_EXECUTOR_CLOSED, ce_latency_ms=self._elapsed_ms(total_started)
            ) from exc
        try:
            async with asyncio.timeout(effective):
                result = await handle.result_async()
        except TimeoutError:
            wait = handle.cancel_or_detach()
            raise CrossEncoderTimeoutError(
                CE_ERROR_TIMEOUT, detach=wait, ce_latency_ms=self._elapsed_ms(total_started)
            ) from None
        except asyncio.CancelledError:
            wait = handle.cancel_or_detach()
            raise CrossEncoderCancellationError(
                CE_ERROR_CANCELLED, detach=wait, ce_latency_ms=self._elapsed_ms(total_started)
            ) from None
        return result

    def _run_worker(
        self, query: str, query_sha256: str, validated: tuple[CeCandidate, ...]
    ) -> Callable[[], "CeRerankResult"]:
        def run() -> CeRerankResult:
            total_started = time.perf_counter_ns()
            scorer, load_ms = self._owner.ensure_loaded()
            resolved = self._resolver.resolve(validated)
            texts = [item.resolved_text for item in resolved]
            inference_started = time.perf_counter_ns()
            try:
                raw_scores = scorer.score(query, texts)
            except CrossEncoderOomError:
                self._owner.mark_failed(CE_ERROR_OOM)
                raise
            except CrossEncoderError:
                raise
            except Exception as exc:
                raise CrossEncoderInferenceError(CE_ERROR_INFERENCE_EXCEPTION) from exc
            inference_ms = (time.perf_counter_ns() - inference_started) / 1_000_000
            scores = validate_scores(raw_scores, len(texts))
            ranked = deterministic_reorder(validated, resolved, scores)
            latency = CeLatency(
                model_load_latency_ms=load_ms,
                inference_latency_ms=inference_ms,
                ce_total_latency_ms=(time.perf_counter_ns() - total_started) / 1_000_000,
            )
            provenance = build_success_provenance(
                query_sha256=query_sha256,
                config=self._config,
                env=self._env,
                ranked=ranked,
                latency=latency,
            )
            return CeRerankResult(status="SUCCEEDED", ranked=ranked, provenance=provenance)

        return run

    @staticmethod
    def _elapsed_ms(started_ns: int) -> float:
        return (time.perf_counter_ns() - started_ns) / 1_000_000


@dataclass(frozen=True, slots=True)
class CeRerankResult:
    status: str
    ranked: tuple[CeRankedCandidate, ...]
    provenance: CeProvenanceRecord


__all__ = [
    "CE_ALGORITHM_REF",
    "CE_CANDIDATE_LIMIT",
    "CE_DEVICE",
    "CE_ERROR_ASSET_DIGEST_MISMATCH",
    "CE_ERROR_ASSET_FILES_MISSING",
    "CE_ERROR_ASSET_MISSING",
    "CE_ERROR_ASSET_NOT_DIRECTORY",
    "CE_ERROR_ASSET_PATH_INVALID",
    "CE_ERROR_CANCELLED",
    "CE_ERROR_CANDIDATE_CONTENT_HASH_MISSING",
    "CE_ERROR_CANDIDATE_DUPLICATE",
    "CE_ERROR_CANDIDATE_INVALID",
    "CE_ERROR_CONFIG_INVALID",
    "CE_ERROR_EXECUTOR_BUSY",
    "CE_ERROR_EXECUTOR_CLOSED",
    "CE_ERROR_INFERENCE_EXCEPTION",
    "CE_ERROR_LOAD_FAILED",
    "CE_ERROR_MODEL_REF_INVALID",
    "CE_ERROR_MODEL_STATE",
    "CE_ERROR_OOM",
    "CE_ERROR_SCORE_BOOL",
    "CE_ERROR_SCORE_COUNT",
    "CE_ERROR_SCORE_NON_FINITE",
    "CE_ERROR_SCORE_SHAPE",
    "CE_ERROR_TEXT_CHUNK_ID_MISMATCH",
    "CE_ERROR_TEXT_CONTENT_HASH_MISMATCH",
    "CE_ERROR_TEXT_DOCUMENT_ID_MISMATCH",
    "CE_ERROR_TEXT_DUPLICATE",
    "CE_ERROR_TEXT_INVALID",
    "CE_ERROR_TEXT_MISSING",
    "CE_ERROR_TEXT_RESOLUTION",
    "CE_ERROR_TIMEOUT",
    "CE_PROVENANCE_SCHEMA_VERSION",
    "TEXT_RESOLUTION_CONTRACT",
    "CeCandidate",
    "CeEnvironmentMeta",
    "CeItemProvenance",
    "CeLatency",
    "CeProvenanceRecord",
    "CeRankedCandidate",
    "CeRerankResult",
    "ChromaCollectionReader",
    "ChromaTextResolver",
    "ChunkCollectionReader",
    "ChunkReadResult",
    "CrossEncoderAssetConfig",
    "CrossEncoderAssetError",
    "CrossEncoderBackpressureError",
    "CrossEncoderCancellationError",
    "CrossEncoderCandidateError",
    "CrossEncoderConfigError",
    "CrossEncoderError",
    "CrossEncoderInferenceError",
    "CrossEncoderLoader",
    "CrossEncoderLoadError",
    "CrossEncoderModelOwner",
    "CrossEncoderModelState",
    "CrossEncoderOomError",
    "CrossEncoderReranker",
    "CrossEncoderScorer",
    "CrossEncoderScoreValidationError",
    "CrossEncoderTextResolutionError",
    "CrossEncoderTimeoutError",
    "ResolvedChunkText",
    "SentenceTransformerCrossEncoderLoader",
    "build_empty_provenance",
    "build_failure_provenance",
    "build_success_provenance",
    "compute_asset_tree_sha256",
    "deterministic_reorder",
    "provenance_to_safe_row",
    "validate_asset",
    "validate_candidates",
    "validate_scores",
]