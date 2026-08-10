"""ToolRegistry / ToolDescriptor / ToolRegistration 核心契约测试（WP2-A）。

覆盖：descriptor/registration 校验、注册顺序、freeze（幂等）、read-before-freeze、
mutation-after-freeze、duplicate（保留 original binding）、resolve/require/contains/
descriptors/registrations、不可变性、descriptor/adapter Tool-name invariant、
同一 registration/adapter identity、冻结后并发只读。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

import pytest

from core.runtime.tool_adapters import LegacyStringToolAdapter, ToolAdapter
from core.runtime.tool_registry import (
    ToolDescriptor,
    ToolRegistration,
    ToolRegistry,
    ToolRegistryError,
    ToolRegistryErrorCode,
)


def _adapter(tool_name: str) -> ToolAdapter:
    return LegacyStringToolAdapter(tool_name=tool_name, function=lambda _: "ok")


def _descriptor(name: str, description: str | None = None) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        description=description or f"Description for {name}.",
    )


def _registration(name: str) -> ToolRegistration:
    return ToolRegistration(descriptor=_descriptor(name), adapter=_adapter(name))


def _frozen_registry(*names: str) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:
        registry.register(_registration(name))
    registry.freeze()
    return registry


class _NoSpecAdapter(ToolAdapter):
    """无 ToolExecutionSpec 的非法 Adapter，用于 identity 校验 fail-closed 测试。"""

    def build_invocation(self, argument_text: str):  # pragma: no cover
        raise NotImplementedError

    def invoke_once(self, invocation, context):  # pragma: no cover
        raise NotImplementedError


# ---- ToolDescriptor ----

def test_valid_descriptor() -> None:
    descriptor = ToolDescriptor(
        name="list_files", description="  List files in a local directory.  "
    )
    assert descriptor.name == "list_files"
    # description 会 trim 后存储
    assert descriptor.description == "List files in a local directory."


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Uppercase",
        "1leading_digit",
        "has-dash",
        "has.dot",
        "a" * 65,
    ],
)
def test_descriptor_rejects_invalid_name(name: str) -> None:
    with pytest.raises(ToolRegistryError) as captured:
        ToolDescriptor(name=name, description="ok")
    assert captured.value.error_code is ToolRegistryErrorCode.INVALID


def test_descriptor_rejects_non_string_name() -> None:
    with pytest.raises(ToolRegistryError) as captured:
        ToolDescriptor(name=123, description="ok")
    assert captured.value.error_code is ToolRegistryErrorCode.INVALID


@pytest.mark.parametrize(
    "description",
    [
        "",
        "   ",
        "with\u0000nul",
        "with\u0007bell",
        "with\nnewline",
        "with\u007fdel",
        "with\u0085nel",
    ],
)
def test_descriptor_rejects_invalid_description(description: str) -> None:
    with pytest.raises(ToolRegistryError) as captured:
        ToolDescriptor(name="alpha_tool", description=description)
    assert captured.value.error_code is ToolRegistryErrorCode.INVALID


def test_descriptor_accepts_chinese_english_and_ordinary_punctuation() -> None:
    """Unicode-aware 校验不得错误拒绝正常描述（非控制字符）。"""
    for description in (
        "中文 Tool：读取 Excel 文件。",
        "List files in a local directory. Argument: directory path.",
        "Normal — punctuation: ( ) [ ] { } ; : , . ! ? @ # $ % ^ & * + =",
    ):
        descriptor = ToolDescriptor(name="alpha_tool", description=description)
        assert descriptor.description == description


def test_descriptor_is_immutable() -> None:
    descriptor = _descriptor("alpha_tool")
    with pytest.raises(FrozenInstanceError):
        descriptor.name = "mutated"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        descriptor.description = "mutated"  # type: ignore[misc]


# ---- ToolRegistration ----

def test_valid_registration() -> None:
    registration = _registration("alpha_tool")
    assert registration.descriptor.name == "alpha_tool"
    assert registration.adapter.spec.tool_name == "alpha_tool"


def test_registration_requires_tool_descriptor() -> None:
    with pytest.raises(ToolRegistryError) as captured:
        ToolRegistration(descriptor="not-a-descriptor", adapter=_adapter("alpha_tool"))
    assert captured.value.error_code is ToolRegistryErrorCode.INVALID


def test_registration_requires_tool_adapter() -> None:
    with pytest.raises(ToolRegistryError) as captured:
        ToolRegistration(descriptor=_descriptor("alpha_tool"), adapter=object())
    assert captured.value.error_code is ToolRegistryErrorCode.INVALID


def test_registration_rejects_descriptor_adapter_name_mismatch() -> None:
    with pytest.raises(ToolRegistryError) as captured:
        ToolRegistration(
            descriptor=_descriptor("alpha_tool"),
            adapter=_adapter("beta_tool"),
        )
    assert captured.value.error_code is ToolRegistryErrorCode.INVALID


def test_registration_rejects_adapter_without_spec() -> None:
    with pytest.raises(ToolRegistryError) as captured:
        ToolRegistration(
            descriptor=_descriptor("alpha_tool"),
            adapter=_NoSpecAdapter(),
        )
    assert captured.value.error_code is ToolRegistryErrorCode.INVALID


def test_registration_is_immutable() -> None:
    registration = _registration("alpha_tool")
    with pytest.raises(FrozenInstanceError):
        registration.adapter = _adapter("beta_tool")  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        registration.descriptor = _descriptor("beta_tool")  # type: ignore[misc]


# ---- lifecycle / freeze ----

def test_registration_order_preserved_and_deterministic() -> None:
    registry = _frozen_registry("alpha_tool", "beta_tool", "gamma_tool")
    assert tuple(r.descriptor.name for r in registry.registrations()) == (
        "alpha_tool",
        "beta_tool",
        "gamma_tool",
    )
    # 枚举 deterministic
    assert tuple(r.descriptor.name for r in registry.registrations()) == (
        "alpha_tool",
        "beta_tool",
        "gamma_tool",
    )
    assert tuple(d.name for d in registry.descriptors()) == (
        "alpha_tool",
        "beta_tool",
        "gamma_tool",
    )


def test_freeze_is_idempotent() -> None:
    registry = ToolRegistry()
    registry.register(_registration("alpha_tool"))
    registry.freeze()
    registry.freeze()
    registry.freeze()
    assert registry.frozen is True
    assert registry.contains("alpha_tool") is True


@pytest.mark.parametrize(
    "operation",
    [
        lambda registry: registry.resolve("alpha_tool"),
        lambda registry: registry.require("alpha_tool"),
        lambda registry: registry.registrations(),
        lambda registry: registry.descriptors(),
        lambda registry: registry.contains("alpha_tool"),
    ],
)
def test_read_before_freeze_fails_closed(operation) -> None:
    registry = ToolRegistry()
    registry.register(_registration("alpha_tool"))
    with pytest.raises(ToolRegistryError) as captured:
        operation(registry)
    assert captured.value.error_code is ToolRegistryErrorCode.NOT_FROZEN


def test_register_after_freeze_rejected() -> None:
    registry = _frozen_registry("alpha_tool")
    with pytest.raises(ToolRegistryError) as captured:
        registry.register(_registration("beta_tool"))
    assert captured.value.error_code is ToolRegistryErrorCode.FROZEN
    # 不 ignore、不 overwrite、不 queue：alpha_tool 仍在，beta_tool 不在
    assert registry.contains("alpha_tool") is True
    assert registry.contains("beta_tool") is False


def test_duplicate_registration_rejected_and_original_preserved() -> None:
    registry = ToolRegistry()
    original = _registration("alpha_tool")
    registry.register(original)
    with pytest.raises(ToolRegistryError) as captured:
        registry.register(_registration("alpha_tool"))
    assert captured.value.error_code is ToolRegistryErrorCode.DUPLICATE
    registry.freeze()
    # original binding 保留；不允许 last-write-wins
    assert registry.require("alpha_tool") is original


def test_register_rejects_non_registration() -> None:
    registry = ToolRegistry()
    with pytest.raises(ToolRegistryError) as captured:
        registry.register(object())  # type: ignore[arg-type]
    assert captured.value.error_code is ToolRegistryErrorCode.INVALID


# ---- lookup / enumeration ----

def test_resolve_existing() -> None:
    registry = _frozen_registry("alpha_tool")
    assert registry.resolve("alpha_tool").descriptor.name == "alpha_tool"


def test_resolve_unknown_returns_none() -> None:
    registry = _frozen_registry("alpha_tool")
    assert registry.resolve("unknown_tool") is None


def test_require_existing() -> None:
    registry = _frozen_registry("alpha_tool")
    assert registry.require("alpha_tool").descriptor.name == "alpha_tool"


def test_require_unknown_fails_closed() -> None:
    registry = _frozen_registry("alpha_tool")
    with pytest.raises(ToolRegistryError) as captured:
        registry.require("unknown_tool")
    assert captured.value.error_code is ToolRegistryErrorCode.NOT_REGISTERED


def test_contains() -> None:
    registry = _frozen_registry("alpha_tool")
    assert registry.contains("alpha_tool") is True
    assert registry.contains("unknown_tool") is False


def test_enumeration_returns_immutable_collections() -> None:
    registry = _frozen_registry("alpha_tool", "beta_tool")
    registrations = registry.registrations()
    descriptors = registry.descriptors()
    assert isinstance(registrations, tuple)
    assert isinstance(descriptors, tuple)
    with pytest.raises(TypeError):
        registrations[0] = object()  # type: ignore[index]
    with pytest.raises(TypeError):
        descriptors[0] = object()  # type: ignore[index]


# ---- identity preservation ----

def test_same_registration_and_adapter_identity() -> None:
    registry = ToolRegistry()
    registration = _registration("alpha_tool")
    registry.register(registration)
    registry.freeze()
    for _ in range(5):
        assert registry.resolve("alpha_tool") is registration
        assert registry.require("alpha_tool") is registration
        assert registry.require("alpha_tool").adapter is registration.adapter


def test_concurrent_frozen_reads_are_stable() -> None:
    registry = _frozen_registry(
        "alpha_tool", "beta_tool", "gamma_tool", "delta_tool"
    )
    expected = ("alpha_tool", "beta_tool", "gamma_tool", "delta_tool")
    errors: list[BaseException] = []

    def read(name: str) -> None:
        try:
            for _ in range(200):
                assert registry.resolve(name).descriptor.name == name
                assert registry.require(name).adapter is not None
                assert registry.contains(name) is True
                assert tuple(d.name for d in registry.descriptors()) == expected
                assert len(registry.registrations()) == 4
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(read, expected * 2))
    assert errors == []
