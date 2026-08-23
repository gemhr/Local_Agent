"""独立、确定性的 BM25 稀疏索引。"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

BM25_ALGORITHM_REF = "bm25-lucene-idf.v1"
BM25_TOKENIZER_REF = "bm25-unicode-lexical-tokenizer.v1"
BM25_K1 = 1.2
BM25_B = 0.75
BM25_INDEX_SCHEMA_VERSION = "bm25-sparse-index.v1"

# ASCII 技术标识符整体保留；CJK 统一按单字；其他 Unicode 字母按连续词切分。
_TOKEN = re.compile(
    r"[a-z0-9_]+(?:[.@/+:#-][a-z0-9_]+)*|"
    r"[\u3400-\u4dbf\u4e00-\u9fff]|"
    r"[^\W\d_]+|\d+",
    re.UNICODE,
)


def bm25_tokenize(value: str) -> tuple[str, ...]:
    """执行冻结的 NFKC + casefold + lexical tokenization。"""
    if not isinstance(value, str):
        raise TypeError("BM25 tokenizer input must be a string")
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return tuple(match.group(0) for match in _TOKEN.finditer(normalized))


@dataclass(frozen=True, slots=True)
class Bm25Document:
    document_id: str
    chunk_id: str
    text: str
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.document_id or not self.chunk_id:
            raise ValueError("BM25 document and chunk identity must be non-empty")
        if not isinstance(self.text, str):
            raise TypeError("BM25 document text must be a string")
        try:
            json.dumps(self.metadata, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("BM25 document metadata must be JSON-safe") from error

    @property
    def stable_identity(self) -> str:
        return f"{self.document_id}:{self.chunk_id}"


@dataclass(frozen=True, slots=True)
class Bm25SearchResult:
    document: Bm25Document
    score: float
    rank: int


class Bm25SparseIndex:
    """只负责构建、加载和 BM25 top-k chunk retrieval。"""

    def __init__(
        self,
        *,
        documents: tuple[Bm25Document, ...],
        postings: Mapping[str, tuple[tuple[int, int], ...]],
        document_lengths: tuple[int, ...],
        average_document_length: float,
        k1: float = BM25_K1,
        b: float = BM25_B,
    ) -> None:
        if not math.isfinite(k1) or k1 <= 0:
            raise ValueError("BM25 k1 must be finite and positive")
        if not math.isfinite(b) or not 0 <= b <= 1:
            raise ValueError("BM25 b must be finite and in [0, 1]")
        if len(documents) != len(document_lengths):
            raise ValueError("BM25 document length count mismatch")
        if len({item.stable_identity for item in documents}) != len(documents):
            raise ValueError("BM25 chunk identity must be unique")
        if documents and (not math.isfinite(average_document_length) or average_document_length <= 0):
            raise ValueError("BM25 average document length must be finite and positive")
        if not documents and average_document_length != 0.0:
            raise ValueError("empty BM25 index must have zero average document length")
        self._documents = documents
        self._postings = dict(postings)
        self._document_lengths = document_lengths
        self._average_document_length = average_document_length
        self._k1 = float(k1)
        self._b = float(b)

    @classmethod
    def build(
        cls,
        documents: Iterable[Bm25Document],
        *,
        k1: float = BM25_K1,
        b: float = BM25_B,
    ) -> "Bm25SparseIndex":
        ordered = tuple(documents)
        postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        lengths: list[int] = []
        for document_index, document in enumerate(ordered):
            counts = Counter(bm25_tokenize(document.text))
            lengths.append(sum(counts.values()))
            for term, frequency in sorted(counts.items()):
                postings[term].append((document_index, frequency))
        average = sum(lengths) / len(lengths) if lengths else 0.0
        return cls(
            documents=ordered,
            postings={term: tuple(entries) for term, entries in postings.items()},
            document_lengths=tuple(lengths),
            average_document_length=average,
            k1=k1,
            b=b,
        )

    @property
    def document_count(self) -> int:
        return len(self._documents)

    @property
    def average_document_length(self) -> float:
        return self._average_document_length

    def search(self, query: str, *, top_k: int) -> tuple[Bm25SearchResult, ...]:
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
            raise ValueError("BM25 top_k must be a positive integer")
        query_counts = Counter(bm25_tokenize(query))
        if not query_counts or not self._documents:
            return ()
        scores: dict[int, float] = defaultdict(float)
        document_count = len(self._documents)
        for term, query_frequency in query_counts.items():
            postings = self._postings.get(term)
            if not postings:
                continue
            document_frequency = len(postings)
            idf = math.log(
                1.0 + (document_count - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            for document_index, term_frequency in postings:
                document_length = self._document_lengths[document_index]
                denominator = term_frequency + self._k1 * (
                    1.0
                    - self._b
                    + self._b * document_length / self._average_document_length
                )
                contribution = idf * (
                    term_frequency * (self._k1 + 1.0)
                ) / denominator
                scores[document_index] += query_frequency * contribution
        ranked = sorted(
            (
                (score, self._documents[index])
                for index, score in scores.items()
                if math.isfinite(score) and score > 0.0
            ),
            key=lambda item: (-item[0], item[1].stable_identity),
        )[:top_k]
        return tuple(
            Bm25SearchResult(document=document, score=score, rank=rank)
            for rank, (score, document) in enumerate(ranked, 1)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": BM25_INDEX_SCHEMA_VERSION,
            "algorithm_ref": BM25_ALGORITHM_REF,
            "tokenizer_ref": BM25_TOKENIZER_REF,
            "k1": self._k1,
            "b": self._b,
            "average_document_length": self._average_document_length,
            "document_lengths": list(self._document_lengths),
            "documents": [
                {
                    "document_id": item.document_id,
                    "chunk_id": item.chunk_id,
                    "text": item.text,
                    "metadata": dict(item.metadata),
                }
                for item in self._documents
            ],
            "postings": {
                term: [[document_index, frequency] for document_index, frequency in entries]
                for term, entries in sorted(self._postings.items())
            },
        }

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> "Bm25SparseIndex":
        payload = json.loads(path.read_text(encoding="utf-8"))
        expected = {
            "schema_version": BM25_INDEX_SCHEMA_VERSION,
            "algorithm_ref": BM25_ALGORITHM_REF,
            "tokenizer_ref": BM25_TOKENIZER_REF,
            "k1": BM25_K1,
            "b": BM25_B,
        }
        if not isinstance(payload, dict) or any(payload.get(key) != value for key, value in expected.items()):
            raise ValueError("BM25_INDEX_INVALID: frozen contract mismatch")
        documents = tuple(
            Bm25Document(
                document_id=item["document_id"],
                chunk_id=item["chunk_id"],
                text=item["text"],
                metadata=item["metadata"],
            )
            for item in payload["documents"]
        )
        postings = {
            term: tuple((int(index), int(frequency)) for index, frequency in entries)
            for term, entries in payload["postings"].items()
        }
        return cls(
            documents=documents,
            postings=postings,
            document_lengths=tuple(int(value) for value in payload["document_lengths"]),
            average_document_length=float(payload["average_document_length"]),
            k1=float(payload["k1"]),
            b=float(payload["b"]),
        )


__all__ = [
    "BM25_ALGORITHM_REF",
    "BM25_B",
    "BM25_INDEX_SCHEMA_VERSION",
    "BM25_K1",
    "BM25_TOKENIZER_REF",
    "Bm25Document",
    "Bm25SearchResult",
    "Bm25SparseIndex",
    "bm25_tokenize",
]
