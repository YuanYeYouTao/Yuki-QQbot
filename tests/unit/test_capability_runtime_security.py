"""R3 capability runtime security, validation, and Responses ledger tests."""

from __future__ import annotations

import json

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities.catalog import (
    DescriptorRegistrySnapshot,
    UnifiedToolCatalog,
    UnifiedToolCatalogEntry,
)
from qq_ai_bot.capabilities.exposure import (
    NO_LONGER_AUTHORIZED,
    SCHEMA_REVISION_CONFLICT,
    DeclaredSchemaLedger,
)
from qq_ai_bot.capabilities.models import (
    AuthorityContext,
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityIdempotency,
    CapabilityRisk,
    CapabilityTrustSource,
)
from qq_ai_bot.capabilities.policy import CapabilityPolicyContext, CapabilityPolicyEngine
from qq_ai_bot.capabilities.request import REQUEST_TOOLS_NAME, request_tools_definition
from qq_ai_bot.capabilities.runtime import (
    CapabilityIndexCache,
    CapabilityQuery,
    TurnCapabilityRuntime,
)
from qq_ai_bot.capabilities.validation import (
    TOOL_INPUT_VALIDATION_FAILED,
    JsonSchemaCapabilityValidator,
)
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatTool
from qq_ai_bot.runtime.authority import TurnAuthority, TurnSceneFacts
from qq_ai_bot.runtime.contracts import MemoryCapabilityView
from qq_ai_bot.runtime.origin import TurnOrigin as RuntimeTurnOrigin


def _descriptor(
    name: str,
    *,
    namespace: str,
    effect: CapabilityEffect = CapabilityEffect.READ_STATE,
    risk: CapabilityRisk = CapabilityRisk.READ,
    schema: dict[str, object] | None = None,
    origins: frozenset[TurnOrigin] | None = None,
    permissions: frozenset[str] = frozenset(),
    revision: str = "1",
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        canonical_name=name,
        model_name=name,
        group=namespace,
        namespace=namespace,
        input_schema=schema
        or {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        effect=effect,
        risk=risk,
        trust_source=CapabilityTrustSource.CORE,
        allowed_origins=origins or frozenset(TurnOrigin),
        required_permissions=permissions,
        uses_external_data=False,
        cancellable=True,
        idempotency=CapabilityIdempotency.IDEMPOTENT,
        schema_version=revision,
        generation=revision,
    )


def _entry(descriptor: CapabilityDescriptor) -> UnifiedToolCatalogEntry:
    return UnifiedToolCatalogEntry(
        descriptor=descriptor,
        provider_id="core",
        scope_ids=descriptor.scope_ids,
        compact_description=descriptor.description or descriptor.model_name,
        tags=descriptor.tags,
        searchable_text=descriptor.model_name,
        estimated_schema_tokens=12,
        available=True,
        revision=descriptor.schema_version,
    )


def test_schema_validation_rejects_invalid_arguments() -> None:
    entry = _entry(_descriptor("web_search", namespace="web.search"))
    validator = JsonSchemaCapabilityValidator()
    assert validator.admit((entry,)) == ()
    failed = validator.validate("web_search", json.dumps({"query": 1}))
    assert failed.ok is False
    assert failed.error_category == TOOL_INPUT_VALIDATION_FAILED
    ok = validator.validate("web_search", json.dumps({"query": "news"}))
    assert ok.ok is True


def test_unknown_dialect_and_unsafe_regex_are_quarantined() -> None:
    validator = JsonSchemaCapabilityValidator()
    dialect = _descriptor(
        "legacy",
        namespace="web.search",
        schema={"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"},
    )
    nested = _descriptor(
        "nested",
        namespace="web.search",
        schema={
            "type": "object",
            "properties": {"query": {"type": "string", "pattern": "(a+)+"}},
        },
    )
    assert validator.admit((_entry(dialect),)) == ("legacy",)
    assert validator.admit((_entry(nested),)) == ("nested",)


def test_remote_ref_schema_is_quarantined() -> None:
    descriptor = _descriptor(
        "unsafe",
        namespace="web.search",
        schema={"$ref": "https://example.invalid/schema.json"},
    )
    validator = JsonSchemaCapabilityValidator()
    quarantined = validator.admit((_entry(descriptor),))
    assert quarantined == ("unsafe",)
    result = validator.validate("unsafe", "{}")
    assert result.ok is False
    assert result.error_category == "capability_schema_quarantined"


def test_undeclared_and_revoked_tools_are_rejected() -> None:
    search = _entry(_descriptor("web_search", namespace="web.search"))
    mutate = _entry(
        _descriptor(
            "memory_change",
            namespace="memory.state.write",
            effect=CapabilityEffect.WRITE_STATE,
            risk=CapabilityRisk.MUTATE,
        )
    )
    # Completions may shrink declared schemas; Responses keep revoked tools declared.
    ledger = DeclaredSchemaLedger(registry_revision="abc", append_only=True)
    extra = (ChatTool(name="request_tools", description="x", parameters={}),)
    ledger.declare(
        (search, mutate),
        extra_tools=extra,
        callable_ids=frozenset({"web_search", "memory_change"}),
    )
    assert "web_search" in ledger.callable_ids
    ledger.declare((mutate,), extra_tools=extra, callable_ids=frozenset({"memory_change"}))
    assert "web_search" in ledger.declared
    assert "web_search" not in ledger.callable_ids
    validator = JsonSchemaCapabilityValidator()
    validator.admit((search, mutate))
    assert "web_search" not in ledger.callable_ids
    revoked = NO_LONGER_AUTHORIZED
    assert revoked == "capability_no_longer_authorized"


def test_append_only_schema_revision_conflict() -> None:
    first = _entry(_descriptor("web_search", namespace="web.search", revision="1"))
    second = _entry(_descriptor("web_search", namespace="web.search", revision="2"))
    ledger = DeclaredSchemaLedger(registry_revision="abc", append_only=True)
    assert ledger.declare((first,), callable_ids=frozenset({"web_search"})) is None
    conflict = ledger.declare((second,), callable_ids=frozenset({"web_search"}))
    assert conflict == SCHEMA_REVISION_CONFLICT
    third = _entry(_descriptor("read_webpage", namespace="web.read", revision="1"))
    blocked = ledger.declare((third,), callable_ids=frozenset({"web_search", "read_webpage"}))
    assert blocked == SCHEMA_REVISION_CONFLICT


def test_append_only_can_add_new_tools_without_dropping_old() -> None:
    search = _entry(_descriptor("web_search", namespace="web.search"))
    page = _entry(_descriptor("read_webpage", namespace="web.read"))
    ledger = DeclaredSchemaLedger(registry_revision="abc", append_only=True)
    ledger.declare(
        (search,),
        extra_tools=(ChatTool(name="request_tools", description="x", parameters={}),),
        callable_ids=frozenset({"web_search"}),
    )
    ledger.declare((search, page), callable_ids=frozenset({"web_search", "read_webpage"}))
    names = {tool.name for tool in ledger.declared_tools()}
    assert {"web_search", "read_webpage", "request_tools"} <= names


def test_namespace_is_not_a_permission() -> None:
    plugin = _descriptor(
        "plugin__x__admin_set_config",
        namespace="admin.config.write",
        effect=CapabilityEffect.WRITE_STATE,
        risk=CapabilityRisk.MUTATE,
        permissions=frozenset({"superuser"}),
    )
    visible = CapabilityPolicyEngine().visible(
        (plugin,),
        CapabilityPolicyContext(
            authority=AuthorityContext(actor_user_id="u1", is_superuser=False),
            origin=TurnOrigin.USER_MESSAGE,
        ),
    )
    assert visible == ()


def test_image_turns_deny_writes_and_platform_mutate() -> None:
    write = _descriptor(
        "memory_change",
        namespace="memory.state.write",
        effect=CapabilityEffect.WRITE_STATE,
        risk=CapabilityRisk.MUTATE,
    )
    mutate = _descriptor(
        "call_onebot_api",
        namespace="qq.platform.mutate",
        effect=CapabilityEffect.PLATFORM_MUTATE,
        risk=CapabilityRisk.MUTATE,
    )
    read = _descriptor("get_person_memories", namespace="memory.person.read")
    visible = CapabilityPolicyEngine().visible(
        (write, mutate, read),
        CapabilityPolicyContext(
            authority=AuthorityContext(actor_user_id="u1", is_superuser=False),
            origin=TurnOrigin.USER_MESSAGE,
            contains_images=True,
        ),
    )
    assert [item.model_name for item in visible] == ["get_person_memories"]


def test_exclusive_write_hides_other_business_writes() -> None:
    memory_write = _descriptor(
        "memory_change",
        namespace="memory.state.write",
        effect=CapabilityEffect.WRITE_STATE,
        risk=CapabilityRisk.MUTATE,
    )
    admin_write = _descriptor(
        "admin_set_config",
        namespace="admin.config.write",
        effect=CapabilityEffect.WRITE_STATE,
        risk=CapabilityRisk.MUTATE,
        permissions=frozenset({"superuser"}),
    )
    view = MemoryCapabilityView(
        eager_namespaces=(),
        requestable_namespaces=("memory.state.write",),
        hidden_namespaces=(),
        exclusive_namespace="memory.state.write",
        transition_revision=1,
    )
    visible = CapabilityPolicyEngine().visible(
        (memory_write, admin_write),
        CapabilityPolicyContext(
            authority=AuthorityContext(
                actor_user_id="u1",
                is_superuser=True,
                permissions=frozenset({"superuser"}),
            ),
            origin=TurnOrigin.USER_MESSAGE,
            memory_view=view,
        ),
    )
    assert [item.model_name for item in visible] == ["memory_change"]


def test_read_only_origin_hides_destructive_and_writes() -> None:
    write = _descriptor(
        "memory_change",
        namespace="memory.state.write",
        effect=CapabilityEffect.WRITE_STATE,
        risk=CapabilityRisk.MUTATE,
    )
    read = _descriptor("web_search", namespace="web.search")
    visible = CapabilityPolicyEngine().visible(
        (write, read),
        CapabilityPolicyContext(
            authority=AuthorityContext(actor_user_id="u1", is_superuser=False),
            origin=TurnOrigin.PLUGIN_BACKGROUND,
            read_only=True,
        ),
    )
    assert [item.model_name for item in visible] == ["web_search"]


def test_destructive_is_visible_for_user_and_autonomous_not_plugin() -> None:
    destructive = _descriptor(
        "admin_execute_action",
        namespace="admin.action.write",
        effect=CapabilityEffect.WRITE_STATE,
        risk=CapabilityRisk.DESTRUCTIVE,
        permissions=frozenset({"superuser"}),
    )
    engine = CapabilityPolicyEngine()
    context = {
        "authority": AuthorityContext(
            actor_user_id="u1",
            is_superuser=True,
            permissions=frozenset({"superuser"}),
        )
    }
    user = engine.visible(
        (destructive,),
        CapabilityPolicyContext(**context, origin=TurnOrigin.USER_MESSAGE),
    )
    autonomous = engine.visible(
        (destructive,),
        CapabilityPolicyContext(**context, origin=TurnOrigin.AUTONOMOUS_GROUP),
    )
    plugin = engine.visible(
        (destructive,),
        CapabilityPolicyContext(**context, origin=TurnOrigin.PLUGIN_BACKGROUND),
    )
    assert [item.model_name for item in user] == ["admin_execute_action"]
    assert [item.model_name for item in autonomous] == ["admin_execute_action"]
    assert plugin == ()


def test_catalog_entry_round_trip_for_security_fixtures() -> None:
    catalog = UnifiedToolCatalog(
        entries=(_entry(_descriptor("web_search", namespace="web.search")),),
        scopes=(),
        revision="rev",
    )
    assert catalog.by_model_name("web_search") is not None
    assert catalog.by_model_name("missing") is None


def _runtime(*entries: UnifiedToolCatalogEntry, append_only: bool = True) -> TurnCapabilityRuntime:
    catalog = UnifiedToolCatalog(entries=entries, scopes=(), revision="abcd1234")
    snapshot = DescriptorRegistrySnapshot(catalog)
    return TurnCapabilityRuntime(
        registry=snapshot,
        index=CapabilityIndexCache().index_for(snapshot),
        authority=TurnAuthority(
            actor_user_id="1001",
            bot_user_id="9999",
            origin=RuntimeTurnOrigin.USER_MESSAGE,
            permission_ceiling=frozenset(),
            delegated_authority=None,
            authority_revision=1,
        ),
        scene=TurnSceneFacts(scope_type=ScopeType.PRIVATE, group_id=None),
        memory_view=None,
        policy_context=CapabilityPolicyContext(
            authority=AuthorityContext(actor_user_id="1001", is_superuser=False),
            origin=TurnOrigin.USER_MESSAGE,
        ),
        append_only=append_only,
    )


def test_definitions_never_duplicate_request_tools() -> None:
    runtime = _runtime(_entry(_descriptor("web_search", namespace="web.search")))
    runtime.initial_exposure(
        CapabilityQuery(text="search the public web", origin=RuntimeTurnOrigin.USER_MESSAGE)
    )
    names = [tool.name for tool in runtime.definitions()]
    assert names.count(REQUEST_TOOLS_NAME) == 1


def test_schema_conflict_rebuilds_only_without_side_effects() -> None:
    first = _entry(_descriptor("web_search", namespace="web.search", revision="1"))
    runtime = _runtime(first, append_only=True)
    runtime.initial_exposure(
        CapabilityQuery(
            text="search the public web",
            origin=RuntimeTurnOrigin.USER_MESSAGE,
            priority_capability_ids=("web_search",),
        )
    )
    second = _entry(_descriptor("web_search", namespace="web.search", revision="2"))
    runtime._plan = runtime._planner.plan_initial(
        catalog=UnifiedToolCatalog(entries=(second,), scopes=(), revision="abcd1234"),
        requestable_ids=frozenset({"web_search"}),
        hits=(),
        memory_view=None,
        kernel_tools=(request_tools_definition(),),
        query="search",
        artifact_available=False,
        reply_target_available=False,
        priority_ids=("web_search",),
    )
    assert runtime._apply_plan(runtime._plan) == SCHEMA_REVISION_CONFLICT
    runtime.mark_side_effect()
    assert runtime.can_rebuild_provider_chain() is False
    assert runtime.rebuild_after_schema_conflict() is False

    clean = _runtime(first, append_only=True)
    clean.initial_exposure(
        CapabilityQuery(
            text="search the public web",
            origin=RuntimeTurnOrigin.USER_MESSAGE,
            priority_capability_ids=("web_search",),
        )
    )
    clean._plan = runtime._plan
    assert clean._apply_plan(clean._plan) == SCHEMA_REVISION_CONFLICT
    assert clean.rebuild_after_schema_conflict() is True
    assert clean.consume_provider_chain_restart() is True
    assert clean.consume_provider_chain_restart() is False
