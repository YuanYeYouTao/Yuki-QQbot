"""Authority-first first-round exposure (R3 §6)."""

from __future__ import annotations

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities.catalog import UnifiedToolCatalog, UnifiedToolCatalogEntry
from qq_ai_bot.capabilities.exposure import AuthorityFirstExposurePlanner
from qq_ai_bot.capabilities.models import (
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityIdempotency,
    CapabilityRisk,
    CapabilityTrustSource,
)
from qq_ai_bot.capabilities.request import REQUEST_TOOLS_NAME, request_tools_definition
from qq_ai_bot.capabilities.search_index import CapabilitySearchHit


def _descriptor(
    name: str,
    *,
    namespace: str,
    trust: CapabilityTrustSource = CapabilityTrustSource.CORE,
    provider_id: str = "core",
    bundle: str = "",
    synthetic: bool = False,
    allowed_origins: frozenset[TurnOrigin] | None = None,
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
        allowed_origins=allowed_origins if allowed_origins is not None else frozenset(TurnOrigin),
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
    assert names == set()
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
    assert names == set()
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
    assert plan.reason == "ready"


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
    assert {entry.descriptor.model_name for entry in opened.entries} == set()

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


def test_request_tools_evicts_non_bundle_mcp_to_fit_order_bundle() -> None:
    cheap = [
        _entry(
            _descriptor(
                name,
                namespace="food.mcdonalds",
                trust=CapabilityTrustSource.MCP,
                provider_id="mcp.mcd",
            )
        )
        for name in (
            "auto_bind_coupons",
            "available_coupons",
            "list_nutrition_foods",
            "mall_order_list",
            "now_time_info",
            "order_list",
            "query_my_account",
        )
    ]
    members = [
        _entry(
            _descriptor(
                name,
                namespace="food.mcdonalds.order",
                trust=CapabilityTrustSource.MCP,
                provider_id="mcp.mcd",
                bundle="food.mcdonalds.order",
            ),
            tokens={"create_order": 2672, "calculate_price": 2430}.get(name, 80),
        )
        for name in (
            "query_meals",
            "query_meal_detail",
            "calculate_price",
            "create_order",
            "query_order",
            "query_nearby_stores",
            "delivery_query_addresses",
            "delivery_query_stores",
            "query_my_coupons",
        )
    ]
    planner = AuthorityFirstExposurePlanner(
        mcp_tool_limit=16,
        schema_token_budget=12000,
        mcp_schema_token_budget=8000,
    )
    catalog = _catalog(*cheap, *members)
    current = frozenset(entry.descriptor.model_name for entry in cheap)
    plan = planner.plan_growth(
        current_ids=current,
        catalog=catalog,
        requestable_ids=frozenset(entry.descriptor.model_name for entry in catalog.entries),
        hits=(_hit("query_order", "food.mcdonalds.order"),),
        limit=4,
        memory_view=None,
        kernel_tools=(request_tools_definition(),),
    )
    names = {entry.descriptor.model_name for entry in plan.entries}
    assert "create_order" in names
    assert "query_meals" in names
    assert "query_order" in names
    assert "available_coupons" not in names
    assert "now_time_info" not in names
    assert plan.reason == "request_tools"


def test_initial_evicts_non_bundle_mcp_instead_of_dropping_order_bundle() -> None:
    cheap = [
        _entry(
            _descriptor(
                name,
                namespace="food.mcdonalds",
                trust=CapabilityTrustSource.MCP,
                provider_id="mcp.mcd",
            )
        )
        for name in (
            "available_coupons",
            "list_nutrition_foods",
            "now_time_info",
            "order_list",
            "query_my_account",
            "mall_order_list",
            "auto_bind_coupons",
        )
    ]
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
        for name in (
            "query_meals",
            "calculate_price",
            "create_order",
            "query_order",
            "query_nearby_stores",
            "delivery_query_addresses",
            "delivery_query_stores",
            "query_meal_detail",
            "query_my_coupons",
        )
    ]
    catalog = _catalog(*cheap, *members)
    plan = _plan(
        catalog,
        (
            *(_hit(entry.descriptor.model_name) for entry in cheap),
            _hit("query_order", "food.mcdonalds.order"),
        ),
        mcp_tool_limit=16,
    )
    names = {entry.descriptor.model_name for entry in plan.entries}
    assert names == set()
    assert plan.reason == "ready"


def test_selected_order_bundle_may_exceed_mcp_schema_budget() -> None:
    members = [
        _entry(
            _descriptor(
                name,
                namespace="food.mcdonalds.order",
                trust=CapabilityTrustSource.MCP,
                provider_id="mcp.mcd",
                bundle="food.mcdonalds.order",
            ),
            tokens=1000,
        )
        for name in (
            "query_meals",
            "calculate_price",
            "create_order",
            "query_order",
            "query_nearby_stores",
            "delivery_query_addresses",
            "delivery_query_stores",
            "query_meal_detail",
            "query_my_coupons",
        )
    ]
    catalog = _catalog(*members)
    plan = _plan(
        catalog,
        (_hit("create_order", "food.mcdonalds.order"),),
        mcp_tool_limit=16,
        mcp_schema_token_budget=8000,
        schema_token_budget=12000,
    )
    names = {entry.descriptor.model_name for entry in plan.entries}
    assert names == set()
    assert plan.reason == "ready"


def test_order_bundle_still_drops_when_global_schema_budget_is_exceeded() -> None:
    members = [
        _entry(
            _descriptor(
                name,
                namespace="food.mcdonalds.order",
                trust=CapabilityTrustSource.MCP,
                provider_id="mcp.mcd",
                bundle="food.mcdonalds.order",
            ),
            tokens=2000,
        )
        for name in (
            "query_meals",
            "calculate_price",
            "create_order",
            "query_order",
            "query_nearby_stores",
            "delivery_query_addresses",
            "delivery_query_stores",
            "query_meal_detail",
            "query_my_coupons",
        )
    ]
    plan = _plan(
        _catalog(*members),
        (_hit("create_order", "food.mcdonalds.order"),),
        mcp_tool_limit=16,
        mcp_schema_token_budget=8000,
        schema_token_budget=12000,
    )
    assert plan.entries == ()
    assert plan.reason == "ready"


def test_first_round_ignores_retrieval_hits_and_elevated_tools() -> None:
    catalog = _catalog(
        _entry(_descriptor("album_share", namespace="music.share")),
        _entry(_descriptor("web_search", namespace="web.search")),
        _entry(
            _descriptor(
                "admin_get_config",
                namespace="admin.config.read",
                trust=CapabilityTrustSource.ADMIN,
            )
        ),
    )
    idle = _plan(catalog, (), query="在吗")
    music = _plan(catalog, (_hit("album_share", "music.share"),), query="来张专辑")
    admin = _plan(
        catalog,
        (_hit("admin_get_config", "admin.config.read"),),
        query="改配置",
        priority_ids=("admin_get_config",),
    )
    assert {entry.descriptor.model_name for entry in idle.entries} == set()
    assert {entry.descriptor.model_name for entry in music.entries} == set()
    assert {entry.descriptor.model_name for entry in admin.entries} == set()


def test_priority_pins_are_declared_even_when_not_requestable() -> None:
    catalog = _catalog(
        _entry(_descriptor("memory_change", namespace="memory.state.write")),
        _entry(
            _descriptor(
                "decline_reply",
                namespace="reply.admission.decline",
                allowed_origins=frozenset({TurnOrigin.AUTONOMOUS_GROUP}),
            )
        ),
        _entry(
            _descriptor(
                "admin_execute_action",
                namespace="admin.action.write",
                trust=CapabilityTrustSource.ADMIN,
            )
        ),
    )
    planner = AuthorityFirstExposurePlanner(first_round_hard_cap=16)
    plan = planner.plan_initial(
        catalog=catalog,
        requestable_ids=frozenset(),
        hits=(),
        memory_view=None,
        kernel_tools=(request_tools_definition(),),
        query="",
        artifact_available=False,
        reply_target_available=False,
        priority_ids=("memory_change", "decline_reply", "admin_execute_action"),
    )
    names = {entry.descriptor.model_name for entry in plan.entries}
    assert names == {"memory_change"}
    assert "memory_change" not in plan.callable_ids
    assert REQUEST_TOOLS_NAME in plan.callable_ids
