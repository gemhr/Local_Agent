#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Trace Contract Fingerprint：canonicalize + SHA-256 的唯一 Owner。

指纹识别的是 export contract 的 schema + semantic compatibility，而不是某个
Trace 实例、Run、Span、Run 配置或内容身份。本模块**不**独立构建公共合同
schema/domain/policy：权威规范语义描述符由 Consumer-neutral Trace Export
Contract Semantic Owner（``core/runtime/trace_export_contract.py``）构建，
本模块只负责消费它、canonicalize 并计算 digest。它绝不读取 live SpanRecord、
Run、Plan、Journal 或任何业务正文。

Canonical 机制复用仓库既有 precedent（PlanFingerprinter / Journal / Snapshot
digest）：canonical JSON（sort_keys + compact separators）+ SHA-256 hexdigest。
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from core.runtime.snapshot_serialization import canonical_json, sha256_digest
from core.runtime.trace_export_contract import export_contract_semantic_descriptor

TRACE_CONTRACT_FINGERPRINT_ALGORITHM = "sha256"
TRACE_CONTRACT_FINGERPRINT_CANONICAL_ENCODING = "canonical_json_v1"


def _canonical_sort_key(value: object) -> str:
    """对 JSON primitive 提供确定性全序，使无序集合可排序。"""
    if isinstance(value, Mapping):
        return canonical_json(value)
    if isinstance(value, (tuple, list)):
        return canonical_json([_canonical_sort_key(item) for item in value])
    return canonical_json(value)


def _canonicalize_semantic(value: object) -> object:
    """递归使语义描述对无序集合顺序不敏感（dict 由 sort_keys 处理）。"""
    if isinstance(value, Mapping):
        return {key: _canonicalize_semantic(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return sorted(
            (_canonicalize_semantic(item) for item in value),
            key=_canonical_sort_key,
        )
    return value


class TraceContractFingerprinter:
    """Trace Contract Fingerprint 的唯一 Owner（canonicalize + digest）。

    权威语义描述符来自 export contract Owner（``trace_export_contract.py``）；
    本类不维护第二份 field/domain/policy literals。
    """

    ALGORITHM = TRACE_CONTRACT_FINGERPRINT_ALGORITHM
    CANONICAL_ENCODING = TRACE_CONTRACT_FINGERPRINT_CANONICAL_ENCODING

    @classmethod
    def semantic_descriptor(cls) -> Mapping[str, object]:
        return MappingProxyType(export_contract_semantic_descriptor())

    @classmethod
    def fingerprint(cls) -> str:
        return cls.fingerprint_from_semantic_descriptor(
            export_contract_semantic_descriptor()
        )

    @staticmethod
    def fingerprint_from_semantic_descriptor(
        descriptor: Mapping[str, object],
    ) -> str:
        if not isinstance(descriptor, Mapping):
            raise TypeError("descriptor must be a Mapping")
        return sha256_digest(_canonicalize_semantic(descriptor))


# 当前冻结 contract 的权威 fingerprint（lowercase 64-hex）。
TRACE_CONTRACT_FINGERPRINT = TraceContractFingerprinter.fingerprint()


__all__ = [
    "TRACE_CONTRACT_FINGERPRINT",
    "TRACE_CONTRACT_FINGERPRINT_ALGORITHM",
    "TRACE_CONTRACT_FINGERPRINT_CANONICAL_ENCODING",
    "TraceContractFingerprinter",
]
