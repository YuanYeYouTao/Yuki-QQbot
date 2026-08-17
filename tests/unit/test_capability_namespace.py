"""Unit tests for capability namespace model and provider registry (R1 commit 2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from qq_ai_bot.application.provider_registry import ProviderRegistry
from qq_ai_bot.capabilities.namespace import (
    NAMESPACE_BY_ID,
    NAMESPACE_ID_MAX_LENGTH,
    CapabilityNamespace,
    is_valid_namespace_id,
)
from qq_ai_bot.capabilities.provider import _CORE_METADATA
from qq_ai_bot.capabilities.validation import CapabilityValidationResult
from qq_ai_bot.mcp.descriptors import mcp_tool_namespace
from qq_ai_bot.mcp.models import MCPServerConfig
from qq_ai_bot.runtime.errors import (
    ProviderRegistryFrozenError,
    ProviderRegistryNotFrozenError,
)


class TestNamespaceId:
    @pytest.mark.parametrize(
        "value",
        [
            "web",
            "web.search",
            "memory.person.read",
            "reply.voice.preference.write",
            "qq.platform.mutate",
            "kernel.artifact.read",
        ],
    )
    def test_valid_ids(self, value: str) -> None:
        assert is_valid_namespace_id(value)

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "Web.search",
            "web..search",
            ".web",
            "web.",
            "web.Search",
            "web search",
            "1web.search",
            "a.b.c.d.e.f",
            "x" * (NAMESPACE_ID_MAX_LENGTH + 1),
        ],
    )
    def test_invalid_ids(self, value: str) -> None:
        assert not is_valid_namespace_id(value)


class TestCapabilityNamespace:
    def test_valid_namespace(self) -> None:
        namespace = CapabilityNamespace(
            id="memory.person.read",
            parent="memory.person",
            display_name="人物记忆读取",
            description="读取人物长期记忆",
            aliases=("person_memory",),
            tags=("memory",),
        )
        assert namespace.path == ("memory", "person", "read")
        assert namespace.depth == 3

    def test_parent_must_be_prefix(self) -> None:
        with pytest.raises(ValidationError, match="nested under its parent"):
            CapabilityNamespace(id="web.search", parent="memory", display_name="x")

    def test_invalid_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="invalid namespace id"):
            CapabilityNamespace(id="Web.Search", display_name="x")

    def test_aliases_must_be_lowercase_unique(self) -> None:
        with pytest.raises(ValidationError, match="lowercase"):
            CapabilityNamespace(id="web.search", display_name="x", aliases=("WebSearch",))
        with pytest.raises(ValidationError, match="unique"):
            CapabilityNamespace(id="web.search", display_name="x", aliases=("s", "s"))

    def test_namespace_is_frozen(self) -> None:
        namespace = CapabilityNamespace(id="web.search", display_name="x")
        with pytest.raises(ValidationError):
            namespace.id = "web.read"  # type: ignore[misc]


_FROZEN_CORE_NAMESPACES = {
    "web_search": "web.search",
    "read_webpage": "web.read",
    "get_recent_chat_history": "memory.history.recent",
    "search_chat_history": "memory.history.search",
    "get_person_memories": "memory.person.read",
    "get_self_memories": "memory.self.read",
    "get_group_memories": "memory.group.read",
    "get_memory_fact": "memory.fact.read",
    "get_memory_evidence": "memory.evidence.read",
    "memory_change": "memory.state.write",
    "get_relationship": "relationship.read",
    "call_onebot_api": "qq.platform.mutate",
    "send_voice": "reply.voice",
    "set_voice_preference": "reply.voice.preference.write",
    "send_emoji": "reply.emoji",
    "set_reply_layout": "reply.layout",
    "set_reply_target": "reply.target",
    "decline_reply": "reply.admission.decline",
    "read_tool_artifact": "kernel.artifact.read",
    "get_my_capabilities": "kernel.authority.read",
}


class TestFrozenCoreNamespaces:
    def test_every_core_tool_uses_a_registered_namespace(self) -> None:
        for name, expected in _FROZEN_CORE_NAMESPACES.items():
            assert _CORE_METADATA[name][0] == expected
            assert expected in NAMESPACE_BY_ID
        for name, (namespace, _effect, _risk) in _CORE_METADATA.items():
            assert namespace in NAMESPACE_BY_ID, name


class TestMcpToolNamespace:
    def test_prefers_per_tool_then_single_bundle_then_server(self) -> None:
        config = MCPServerConfig.model_validate(
            {
                "url": "https://example.test/mcp",
                "yuki": {
                    "scope": "food.mcdonalds",
                    "toolBundles": {
                        "coupons": {
                            "scope": "food.mcdonalds.coupons",
                            "summary": "查询可用优惠券",
                            "includeTools": ["available-coupons"],
                        }
                    },
                    "toolAnnotations": {
                        "create-order": {"namespace": "food.mcdonalds.order"},
                    },
                },
            }
        )
        assert mcp_tool_namespace("mcd", config, "create-order") == "food.mcdonalds.order"
        assert mcp_tool_namespace("mcd", config, "available-coupons") == "food.mcdonalds.coupons"
        assert mcp_tool_namespace("mcd", config, "now-time-info") == "food.mcdonalds"


class TestValidationResult:
    def test_failure_requires_error_category(self) -> None:
        ok = CapabilityValidationResult(ok=True)
        assert ok.error_category is None
        failed = CapabilityValidationResult(ok=False, error_category="schema_mismatch")
        assert failed.error_category == "schema_mismatch"
        with pytest.raises(ValueError, match="error category"):
            CapabilityValidationResult(ok=False)


class TestProviderRegistry:
    def test_register_then_freeze_then_get(self) -> None:
        registry = ProviderRegistry()
        provider = object()
        registry.register("memory", provider)
        assert not registry.frozen
        assert registry.freeze() == 1
        assert registry.frozen
        assert registry.get("memory") is provider
        assert registry.names() == ("memory",)

    def test_read_before_freeze_rejected(self) -> None:
        registry = ProviderRegistry()
        registry.register("memory", object())
        with pytest.raises(ProviderRegistryNotFrozenError):
            registry.get("memory")

    def test_register_after_freeze_rejected(self) -> None:
        registry = ProviderRegistry()
        registry.freeze()
        with pytest.raises(ProviderRegistryFrozenError):
            registry.register("late", object())

    def test_double_freeze_rejected(self) -> None:
        registry = ProviderRegistry()
        registry.freeze()
        with pytest.raises(ProviderRegistryFrozenError):
            registry.freeze()

    def test_duplicate_registration_rejected(self) -> None:
        registry = ProviderRegistry()
        registry.register("memory", object())
        with pytest.raises(ValueError, match="already registered"):
            registry.register("memory", object())

    def test_unknown_provider_after_freeze(self) -> None:
        registry = ProviderRegistry()
        registry.freeze()
        with pytest.raises(KeyError, match="unknown provider"):
            registry.get("missing")
