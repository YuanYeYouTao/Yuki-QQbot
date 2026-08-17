"""Prefetch and request_tools share one revision-cached FTS index (R3 §7)."""

from __future__ import annotations

import asyncio

from qq_ai_bot.automation.models import TurnOrigin as AutomationTurnOrigin
from qq_ai_bot.capabilities.catalog import (
    DescriptorRegistrySnapshot,
    UnifiedToolCatalog,
    UnifiedToolCatalogEntry,
)
from qq_ai_bot.capabilities.models import (
    AuthorityContext,
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityIdempotency,
    CapabilityRisk,
    CapabilityTrustSource,
)
from qq_ai_bot.capabilities.policy import CapabilityPolicyContext
from qq_ai_bot.capabilities.runtime import (
    CapabilityIndexCache,
    CapabilityQuery,
    TurnCapabilityRuntime,
)
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.runtime.authority import TurnAuthority, TurnSceneFacts
from qq_ai_bot.runtime.origin import TurnOrigin


def _entry(name: str, namespace: str, description: str) -> UnifiedToolCatalogEntry:
    descriptor = CapabilityDescriptor(
        canonical_name=name,
        model_name=name,
        group=namespace,
        namespace=namespace,
        description=description,
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object"},
        effect=CapabilityEffect.READ_STATE,
        risk=CapabilityRisk.READ,
        trust_source=CapabilityTrustSource.CORE,
        allowed_origins=frozenset(AutomationTurnOrigin),
        required_permissions=frozenset(),
        uses_external_data=False,
        cancellable=True,
        idempotency=CapabilityIdempotency.IDEMPOTENT,
        aliases=(name.replace("_", " "),),
        use_when=(description,),
    )
    return UnifiedToolCatalogEntry(
        descriptor=descriptor,
        provider_id="core",
        scope_ids=descriptor.scope_ids,
        compact_description=description,
        tags=(),
        searchable_text=f"{name} {description}",
        estimated_schema_tokens=8,
        available=True,
        revision="1",
    )


def _runtime() -> TurnCapabilityRuntime:
    catalog = UnifiedToolCatalog(
        entries=(
            _entry("web_search", "web.search", "search the public web"),
            _entry("send_emoji", "reply.emoji", "send a sticker"),
            _entry("plugin__netease__search_song", "music.search", "搜索并发送网易云单曲"),
        ),
        scopes=(),
        revision="abcd1234",
    )
    snapshot = DescriptorRegistrySnapshot(catalog)
    return TurnCapabilityRuntime(
        registry=snapshot,
        index=CapabilityIndexCache().index_for(snapshot),
        authority=TurnAuthority(
            actor_user_id="1001",
            bot_user_id="9999",
            origin=TurnOrigin.USER_MESSAGE,
            permission_ceiling=frozenset(),
            delegated_authority=None,
            authority_revision=1,
        ),
        scene=TurnSceneFacts(scope_type=ScopeType.PRIVATE, group_id=None),
        memory_view=None,
        policy_context=CapabilityPolicyContext(
            authority=AuthorityContext(actor_user_id="1001", is_superuser=False),
            origin=AutomationTurnOrigin.USER_MESSAGE,
        ),
        append_only=False,
    )


def test_prefetch_and_request_tools_share_the_same_index_hits() -> None:
    runtime = _runtime()
    runtime.initial_exposure(
        CapabilityQuery(text="等一下", origin=TurnOrigin.USER_MESSAGE, limit=4)
    )
    assert "plugin__netease__search_song" not in runtime.callable_capability_ids()

    song = CapabilityQuery(
        text="搜索并发送网易云单曲",
        origin=TurnOrigin.USER_MESSAGE,
        limit=4,
    )
    hits = asyncio.run(runtime.search(song))
    payload = asyncio.run(runtime.request_tools(song))

    assert hits[0].capability_id == "plugin__netease__search_song"
    assert payload["ok"] is True
    loaded = {item["name"] for item in payload["data"]["loaded_tools"]}
    assert "plugin__netease__search_song" in loaded
