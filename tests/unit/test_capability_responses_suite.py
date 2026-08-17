"""R3 §12.4 Responses continuation contract for the exposure ledger."""

from __future__ import annotations

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities.catalog import UnifiedToolCatalogEntry
from qq_ai_bot.capabilities.exposure import (
    SCHEMA_REVISION_CONFLICT,
    DeclaredSchemaLedger,
)
from qq_ai_bot.capabilities.models import (
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityIdempotency,
    CapabilityRisk,
    CapabilityTrustSource,
)
from qq_ai_bot.domain.messages import ChatTool


def _entry(name: str, *, revision: str = "1", tokens: int = 12) -> UnifiedToolCatalogEntry:
    descriptor = CapabilityDescriptor(
        canonical_name=name,
        model_name=name,
        group="web.search" if name == "web_search" else "web.read",
        namespace="web.search" if name == "web_search" else "web.read",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        output_schema={"type": "object"},
        effect=CapabilityEffect.READ_STATE,
        risk=CapabilityRisk.READ,
        trust_source=CapabilityTrustSource.CORE,
        allowed_origins=frozenset(TurnOrigin),
        required_permissions=frozenset(),
        uses_external_data=False,
        cancellable=True,
        idempotency=CapabilityIdempotency.IDEMPOTENT,
        schema_version=revision,
        generation=revision,
    )
    return UnifiedToolCatalogEntry(
        descriptor=descriptor,
        provider_id="core",
        scope_ids=descriptor.scope_ids,
        compact_description=name,
        tags=(),
        searchable_text=name,
        estimated_schema_tokens=tokens,
        available=True,
        revision=revision,
    )


def test_responses_keeps_old_tools_and_appends_without_duplicates() -> None:
    ledger = DeclaredSchemaLedger(registry_revision="abcd1234", append_only=True)
    extra = (ChatTool(name="request_tools", description="x", parameters={}),)
    search = _entry("web_search")
    page = _entry("read_webpage")
    ledger.declare((search,), extra_tools=extra, callable_ids=frozenset({"web_search"}))
    ledger.declare(
        (search, page), extra_tools=extra, callable_ids=frozenset({"web_search", "read_webpage"})
    )
    names = [tool.name for tool in ledger.declared_tools()]
    assert names.count("request_tools") == 1
    assert names.count("web_search") == 1
    assert "read_webpage" in names
    assert "web_search" in ledger.declared
    ledger.declare((page,), extra_tools=extra, callable_ids=frozenset({"read_webpage"}))
    assert "web_search" in ledger.declared
    assert "web_search" not in ledger.callable_ids


def test_responses_schema_revision_conflict_aborts() -> None:
    ledger = DeclaredSchemaLedger(registry_revision="abcd1234", append_only=True)
    first = _entry("web_search", revision="1")
    second = _entry("web_search", revision="2")
    assert ledger.declare((first,), callable_ids=frozenset({"web_search"})) is None
    assert (
        ledger.declare((second,), callable_ids=frozenset({"web_search"}))
        == SCHEMA_REVISION_CONFLICT
    )
    assert ledger.declare((_entry("read_webpage"),), callable_ids=frozenset({"read_webpage"})) == (
        SCHEMA_REVISION_CONFLICT
    )


def test_responses_cumulative_schema_budget_is_recorded() -> None:
    ledger = DeclaredSchemaLedger(registry_revision="abcd1234", append_only=True)
    search = _entry("web_search", tokens=40)
    page = _entry("read_webpage", tokens=25)
    ledger.declare((search,), callable_ids=frozenset({"web_search"}))
    ledger.declare((search, page), callable_ids=frozenset({"web_search", "read_webpage"}))
    assert ledger.schema_token_total == 65
