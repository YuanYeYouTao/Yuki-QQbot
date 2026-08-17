"""Authority-first first-round exposure (R3 §6)."""

from __future__ import annotations

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities.catalog import UnifiedToolCatalog, UnifiedToolCatalogEntry
from qq_ai_bot.capabilities.exposure import (
    BUNDLE_EXCEEDS_BUDGET,
    AuthorityFirstExposurePlanner,
)
from qq_ai_bot.capabilities.models import (
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityIdempotency,
    CapabilityRisk,
    CapabilityTrustSource,
)
from qq_ai_bot.capabilities.request import request_tools_definition
from qq_ai_bot.capabilities.search_index import CapabilitySearchHit


def _descriptor(
    name: str,
    *,
    namespace: str,
    trust: CapabilityTrustSource = CapabilityTrustSource.CORE,
    provider_id: str = "core",
    bundle: str = "",
    synthetic: bool = False,
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        canonical_name=name,
        model_name=name,
        group=namespace,
        namespace=namespace,
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object"},
        effect=CapabilityEffect.READ_STATE,
        risk=CapabilityRisk.READ,
        trust_source=trust,
        allowed_origins=frozenset(TurnOrigin),
        required_permissions=frozenset(),
        uses_external_data=trust is CapabilityTrustSource.MCP,
        cancellable=True,
        idempotency=CapabilityIdempotency.IDEMPOTENT,
        provider_id=provider_id,
        bundle_scopes=(bundle,) if bundle else (),
        additional_scopes=(bundle,) if bundle else (),
        provider_metadata={"synthetic": True} if synthetic else None,
    )


def _entry(descriptor: CapabilityDescriptor, *, tokens: int = 8) -> UnifiedToolCatalogEntry:
    return UnifiedToolCatalogEntry(
        descriptor=descriptor,
        provider_id=descriptor.provider_id,
        scope_ids=descriptor.scope_ids,
        compact_description=descriptor.model_name,
        tags=descriptor.tags,
        searchable_text=descriptor.model_name,
        estimated_schema_tokens=tokens,
        available=True,
        revision="1",
        bundle_scope_ids=descriptor.bundle_scopes,
    )


def _catalog(*entries: UnifiedToolCatalogEntry) -> UnifiedToolCatalog:
    return UnifiedToolCatalog(entries=entries, scopes=(), revision="rev1")


def _hit(capability_id: str, namespace_id: str = "food.mcdonalds") -> CapabilitySearchHit:
    return CapabilitySearchHit(
        capability_id=capability_id,
        namespace_id=namespace_id,
        score=1.0,
    )


def _plan(
    catalog: UnifiedToolCatalog,
    hits: tuple[CapabilitySearchHit, ...],
    *,
    query: str = "等待",
    mcp_tool_limit: int | None = 10,
    first_round_hard_cap: int = 12,
    schema_token_budget: int | None = None,
    mcp_schema_token_budget: int | None = None,
    priority_ids: tuple[str, ...] = (),
    artifact_available: bool = False,
    reply_target_available: bool = False,
):
    planner = AuthorityFirstExposurePlanner(
        first_round_hard_cap=first_round_hard_cap,
        schema_token_budget=schema_token_budget,
        mcp_schema_token_budget=mcp_schema_token_budget,
        mcp_tool_limit=mcp_tool_limit,
    )
    return planner.plan_initial(
        catalog=catalog,
        requestable_ids=frozenset(entry.descriptor.model_name for entry in catalog.entries),
        hits=hits,
        memory_view=None,
        kernel_tools=(request_tools_definition(),),
        query=query,
        artifact_available=artifact_available,
        reply_target_available=reply_target_available,
        priority_ids=priority_ids,
    )


def test_weak_mcp_hit_does_not_dump_the_provider() -> None:
    members = [
        _entry(
            _descriptor(
                f"mcp_mcd_{index}",
                namespace="food.mcdonalds",
                trust=CapabilityTrustSource.MCP,
                provider_id="mcp.mcd",
            )
        )
        for index in range(12)
    ]
    catalog = _catalog(*members)
    plan = _plan(catalog, (_hit("mcp_mcd_0"),))

    names = {entry.descriptor.model_name for entry in plan.entries}
    assert names == {"mcp_mcd_0"}
    assert plan.reason == "ready"


def test_selected_bundle_expands_every_required_member() -> None:
    members = [
        _entry(
            _descriptor(
                name,
                namespace="food.mcdonalds.order",
                trust=CapabilityTrustSource.MCP,
                provider_id="mcp.mcd",
                bundle="food.mcdonalds.order",
            )
        )
        for name in ("query_meals", "calculate_price", "create_order")
    ]
    other = _entry(
        _descriptor(
            "now_time_info",
            namespace="food.mcdonalds",
            trust=CapabilityTrustSource.MCP,
            provider_id="mcp.mcd",
        )
    )
    catalog = _catalog(*members, other)
    plan = _plan(catalog, (_hit("calculate_price", "food.mcdonalds.order"),))

    names = {entry.descriptor.model_name for entry in plan.entries}
    assert names == {"query_meals", "calculate_price", "create_order"}
    assert "now_time_info" not in names
    assert plan.reason == "ready"


def test_over_budget_bundle_is_dropped_not_clipped() -> None:
    members = [
        _entry(
            _descriptor(
                f"order_{index}",
                namespace="food.mcdonalds.order",
                trust=CapabilityTrustSource.MCP,
                provider_id="mcp.mcd",
                bundle="food.mcdonalds.order",
            ),
            tokens=40,
        )
        for index in range(6)
    ]
    catalog = _catalog(*members)
    plan = _plan(
        catalog,
        (_hit("order_0", "food.mcdonalds.order"),),
        mcp_tool_limit=3,
    )

    assert plan.entries == ()
    assert plan.reason.startswith(BUNDLE_EXCEEDS_BUDGET)
    assert "food.mcdonalds.order" in plan.reason


def test_permission_kernel_stays_closed_unless_query_asks() -> None:
    catalog = _catalog(
        _entry(_descriptor("get_my_capabilities", namespace="kernel.authority.read")),
        _entry(_descriptor("read_tool_artifact", namespace="kernel.artifact.read")),
        _entry(_descriptor("set_reply_target", namespace="reply.target")),
        _entry(_descriptor("web_search", namespace="web.search")),
    )
    closed = _plan(catalog, ())
    assert {entry.descriptor.model_name for entry in closed.entries} == set()

    opened = _plan(catalog, (), query="你会什么权限")
    assert {entry.descriptor.model_name for entry in opened.entries} == {"get_my_capabilities"}

    with_artifact = _plan(catalog, (), artifact_available=True)
    assert {entry.descriptor.model_name for entry in with_artifact.entries} == {
        "read_tool_artifact"
    }


def test_hydrate_is_not_an_exposure_signal() -> None:
    members = [
        _entry(
            _descriptor(
                f"mcp_mcd_{index}",
                namespace="food.mcdonalds",
                trust=CapabilityTrustSource.MCP,
                provider_id="mcp.mcd",
            )
        )
        for index in range(8)
    ]
    catalog = _catalog(*members)
    plan = _plan(catalog, ())
    assert plan.entries == ()


def test_request_tools_uses_the_same_bundle_rule() -> None:
    members = [
        _entry(
            _descriptor(
                name,
                namespace="food.mcdonalds.order",
                trust=CapabilityTrustSource.MCP,
                provider_id="mcp.mcd",
                bundle="food.mcdonalds.order",
            )
        )
        for name in ("query_meals", "calculate_price", "create_order")
    ]
    planner = AuthorityFirstExposurePlanner()
    catalog = _catalog(*members)
    plan = planner.plan_growth(
        current_ids=frozenset(),
        catalog=catalog,
        requestable_ids=frozenset(entry.descriptor.model_name for entry in members),
        hits=(_hit("query_meals", "food.mcdonalds.order"),),
        limit=1,
        memory_view=None,
        kernel_tools=(request_tools_definition(),),
    )
    assert {entry.descriptor.model_name for entry in plan.entries} == {
        "query_meals",
        "calculate_price",
        "create_order",
    }
