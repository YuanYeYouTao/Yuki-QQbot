"""request_tools must be able to recall automation_create after first-round unpinning."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from qq_ai_bot.automation.models import TurnOrigin as AutomationTurnOrigin
from qq_ai_bot.automation.tools import AutomationToolService
from qq_ai_bot.capabilities.catalog import DescriptorRegistrySnapshot, ToolProviderRegistry
from qq_ai_bot.capabilities.models import AuthorityContext, CapabilityTrustSource
from qq_ai_bot.capabilities.policy import CapabilityPolicyContext
from qq_ai_bot.capabilities.provider import InProcessToolProvider
from qq_ai_bot.capabilities.runtime import (
    CapabilityIndexCache,
    CapabilityQuery,
    TurnCapabilityRuntime,
    _document_from_entry,
)
from qq_ai_bot.capabilities.search_index import FtsCapabilitySearchIndex
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.runtime.authority import TurnAuthority, TurnSceneFacts
from qq_ai_bot.runtime.origin import TurnOrigin


async def _noop_execute(name: str, arguments: str, runtime: object) -> object:
    del name, arguments, runtime
    return {"ok": True}


def _automation_catalog():
    registry = ToolProviderRegistry()
    service = AutomationToolService(SimpleNamespace(enabled=True))
    registry.register(
        InProcessToolProvider(
            provider_id="automation",
            source=CapabilityTrustSource.AUTOMATION,
            definitions=lambda _context: service.definitions(),
            execute=_noop_execute,
        )
    )
    return registry.catalog(None)


def _index_from_catalog(catalog):
    index = FtsCapabilitySearchIndex()
    index.rebuild(
        revision=catalog.revision,
        documents=[_document_from_entry(entry) for entry in catalog.entries],
    )
    return index


def _runtime(catalog) -> TurnCapabilityRuntime:
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
        append_only=True,
    )


@pytest.mark.parametrize(
    "query",
    (
        "automation_create",
        "automation create",
        "automation-create",
        "请求 automation create",
        "你去请求automation create",
    ),
)
def test_spaced_automation_create_outranks_read_and_cancel_tools(query: str) -> None:
    catalog = _automation_catalog()
    index = _index_from_catalog(catalog)
    hits = index.search(query, limit=4)

    assert hits
    assert hits[0].capability_id == "automation_create"
    assert {hit.capability_id for hit in hits} != {
        "time_get_current",
        "time_get_timezone",
        "automation_cancel",
        "automation_pause",
    }


@pytest.mark.asyncio
async def test_request_tools_loads_automation_create_from_spaced_name() -> None:
    runtime = _runtime(_automation_catalog())
    runtime.initial_exposure(
        CapabilityQuery(text="你好", origin=TurnOrigin.USER_MESSAGE, limit=4)
    )
    assert "automation_create" not in runtime.callable_capability_ids()

    payload = await runtime.request_tools(
        CapabilityQuery(
            text="automation create",
            origin=TurnOrigin.USER_MESSAGE,
            limit=4,
        )
    )

    assert payload["ok"] is True
    loaded = {item["name"] for item in payload["data"]["loaded_tools"]}
    assert "automation_create" in loaded
    assert "automation_create" in runtime.callable_capability_ids()
