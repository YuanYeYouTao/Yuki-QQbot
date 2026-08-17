"""Per-turn capability runtime: pin revision, expose, search, validate.

Chat and AgentRunner may only consume this object for tool exposure.  Planner
fields are not read.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from qq_ai_bot.capabilities.catalog import (
    AuthorizedCatalogSnapshot,
    DescriptorRegistrySnapshot,
    UnifiedToolCatalog,
    UnifiedToolCatalogEntry,
)
from qq_ai_bot.capabilities.exposure import (
    NO_LONGER_AUTHORIZED,
    SCHEMA_REVISION_CONFLICT,
    AuthorityFirstExposurePlanner,
    DeclaredSchemaLedger,
    ExposurePlan,
    is_memory_write_entry,
)
from qq_ai_bot.capabilities.models import CapabilityDescriptor
from qq_ai_bot.capabilities.namespace import is_valid_namespace_id, lookup_namespace
from qq_ai_bot.capabilities.policy import CapabilityPolicyContext, CapabilityPolicyEngine
from qq_ai_bot.capabilities.request import REQUEST_TOOLS_NAME, request_tools_definition
from qq_ai_bot.capabilities.search_document import (
    SEARCH_DOCUMENT_BODY_MAX,
    CapabilitySearchDocument,
)
from qq_ai_bot.capabilities.search_index import CapabilitySearchHit, FtsCapabilitySearchIndex
from qq_ai_bot.capabilities.validation import (
    TOOL_INPUT_VALIDATION_FAILED,
    JsonSchemaCapabilityValidator,
)
from qq_ai_bot.domain.messages import ChatTool
from qq_ai_bot.runtime.authority import TurnAuthority, TurnSceneFacts
from qq_ai_bot.runtime.contracts import CapabilityExposureSnapshot, MemoryCapabilityView
from qq_ai_bot.runtime.origin import TurnOrigin

EnsureMetadata = Callable[[str], Awaitable[None]]
RefreshRegistry = Callable[[], tuple[DescriptorRegistrySnapshot, FtsCapabilitySearchIndex]]


@dataclass(frozen=True, slots=True)
class CapabilitySearchReport:
    """Content-free local search outcome for host observability."""

    origin: str
    hit_count: int
    latency_ms: int
    capability_ids: tuple[str, ...]


OnCapabilitySearched = Callable[[CapabilitySearchReport], None]


@dataclass(frozen=True, slots=True)
class CapabilityQuery:
    """One retrieval request (from ``request_tools`` or host heuristics)."""

    text: str
    origin: TurnOrigin
    limit: int = 5
    reply_excerpt: str = ""
    affinity_namespace_ids: tuple[str, ...] = ()
    priority_capability_ids: tuple[str, ...] = ()


class CapabilityIndexCache:
    """Reuse one FTS index per registry content hash."""

    def __init__(self) -> None:
        self._revision: str | None = None
        self._index = FtsCapabilitySearchIndex()

    def index_for(self, snapshot: DescriptorRegistrySnapshot) -> FtsCapabilitySearchIndex:
        if self._revision == snapshot.revision:
            return self._index
        documents = tuple(_document_from_entry(entry) for entry in snapshot.catalog.entries)
        self._index.rebuild(revision=snapshot.revision, documents=documents)
        self._revision = snapshot.revision
        return self._index


class TurnCapabilityRuntime:
    """Owns one turn's authorized catalog revision, exposure and callable set."""

    def __init__(
        self,
        *,
        registry: DescriptorRegistrySnapshot,
        index: FtsCapabilitySearchIndex,
        authority: TurnAuthority,
        scene: TurnSceneFacts,
        memory_view: MemoryCapabilityView | None,
        policy_context: CapabilityPolicyContext,
        append_only: bool,
        schema_token_budget: int | None = None,
        mcp_schema_token_budget: int | None = None,
        mcp_tool_limit: int | None = None,
        ensure_metadata: EnsureMetadata | None = None,
        refresh_registry: RefreshRegistry | None = None,
        on_searched: OnCapabilitySearched | None = None,
    ) -> None:
        self._registry = registry
        self._index = index
        self._authority = authority
        self._scene = scene
        self._memory_view = memory_view
        self._policy_context = policy_context
        self._policy = CapabilityPolicyEngine()
        self._planner = AuthorityFirstExposurePlanner(
            schema_token_budget=schema_token_budget,
            mcp_schema_token_budget=mcp_schema_token_budget,
            mcp_tool_limit=mcp_tool_limit,
        )
        self._validator = JsonSchemaCapabilityValidator()
        self._ensure_metadata = ensure_metadata
        self._refresh_registry = refresh_registry
        self._on_searched = on_searched
        self._mcp_schema_token_budget = mcp_schema_token_budget
        self._mcp_tool_limit = mcp_tool_limit
        self._discovered_mcp_providers: tuple[str, ...] = ()
        self._authorized = self._project_authorized()
        self._ledger = DeclaredSchemaLedger(
            registry_revision=registry.revision,
            append_only=append_only,
        )
        self._plan: ExposurePlan | None = None
        self._restart_provider_chain = False
        self._affinity: tuple[str, ...] = ()
        self._exclusive_write = bool(memory_view and memory_view.exclusive_namespace)
        quarantined = self._validator.admit(self._authorized.catalog.entries)
        if quarantined:
            requestable = frozenset(
                item for item in self._authorized.requestable_ids if item not in set(quarantined)
            )
            self._authorized = replace(self._authorized, requestable_ids=requestable)

    @property
    def registry_revision(self) -> str:
        return self._registry.revision

    @property
    def authorized_catalog(self) -> UnifiedToolCatalog:
        return self._authorized.catalog

    def pin_catalog_revision(self) -> int:
        return int(self._registry.revision[:8], 16)

    def sync_memory_view(self, view: MemoryCapabilityView | None) -> None:
        """Re-project authority when the memory contract revision changes.

        Exclusive write and locator-read escalations increment
        ``transition_revision``.  Chat Completions rebuild the exposed set;
        Responses keep declared schemas and only shrink the callable set.
        """

        current_revision = (
            self._memory_view.transition_revision if self._memory_view is not None else None
        )
        next_revision = view.transition_revision if view is not None else None
        if current_revision == next_revision and (self._memory_view is None) == (view is None):
            return
        self._memory_view = view
        self._exclusive_write = bool(view is not None and view.exclusive_namespace)
        self._policy_context = replace(self._policy_context, memory_view=view)
        self._authorized = self._project_authorized()
        kernel = (request_tools_definition(),) if not self._policy_context.tools_closed else ()
        plan = self._planner.plan_initial(
            catalog=self._authorized.catalog,
            requestable_ids=self._authorized.requestable_ids,
            hits=(),
            memory_view=self._memory_view,
            kernel_tools=kernel,
            query="",
            artifact_available=self._policy_context.artifact_available,
            reply_target_available=self._policy_context.reply_target_available,
        )
        if self._ledger.append_only:
            conflict = self._apply_plan(plan)
            if conflict is None:
                self._ledger.callable_ids = set(plan.callable_ids)
                if REQUEST_TOOLS_NAME in {tool.name for tool in kernel}:
                    self._ledger.callable_ids.add(REQUEST_TOOLS_NAME)
            self._plan = plan
            return
        self._plan = plan
        self._apply_plan(plan)

    def exposure_snapshot(self) -> CapabilityExposureSnapshot:
        return self._ledger.snapshot()

    def callable_capability_ids(self) -> frozenset[str]:
        return frozenset(self._ledger.callable_ids)

    def definitions(self) -> tuple[ChatTool, ...]:
        tools = list(self._ledger.declared_tools())
        if self._append_request_tools(tools) and REQUEST_TOOLS_NAME not in {
            tool.name for tool in tools
        }:
            tools.append(request_tools_definition())
        unique: dict[str, ChatTool] = {}
        for tool in tools:
            unique.setdefault(tool.name, tool)
        return tuple(sorted(unique.values(), key=lambda item: item.name))

    def initial_exposure(self, query: CapabilityQuery) -> CapabilityExposureSnapshot:
        started = time.perf_counter()
        hits = self._search_local(query, limit=10)
        self._notify_searched(query, hits, started)
        kernel = (request_tools_definition(),) if not self._policy_context.tools_closed else ()
        self._plan = self._planner.plan_initial(
            catalog=self._authorized.catalog,
            requestable_ids=self._authorized.requestable_ids,
            hits=hits,
            memory_view=self._memory_view,
            kernel_tools=kernel,
            query=query.text,
            artifact_available=self._policy_context.artifact_available,
            reply_target_available=self._policy_context.reply_target_available,
            priority_ids=query.priority_capability_ids,
        )
        self._apply_plan(self._plan)
        self._affinity = tuple(dict.fromkeys(hit.namespace_id for hit in hits[:3]))
        return self._ledger.snapshot()

    async def prepare_initial_exposure(self, query: CapabilityQuery) -> CapabilityExposureSnapshot:
        """Hydrate lazy MCP servers that the query can discover, then expose."""

        await self._hydrate_lazy_mcp(query)
        return self.initial_exposure(query)

    async def search(self, query: CapabilityQuery) -> tuple[CapabilitySearchHit, ...]:
        await self._hydrate_lazy_mcp(query)
        started = time.perf_counter()
        hits = self._search_local(query, limit=query.limit)
        self._notify_searched(query, hits, started)
        return hits

    async def request_tools(self, query: CapabilityQuery) -> dict[str, object]:
        hits = await self.search(query)
        authorized_hits = tuple(
            hit for hit in hits if hit.capability_id in self._authorized.requestable_ids
        )
        if not authorized_hits:
            return {
                "ok": False,
                "error": "capability_not_found",
                "detail": "当前真实用户和场景允许的工具目录中没有匹配能力",
            }
        current = frozenset(self._ledger.declared) - {REQUEST_TOOLS_NAME}
        kernel = (request_tools_definition(),) if not self._policy_context.tools_closed else ()
        plan = self._planner.plan_growth(
            current_ids=current,
            catalog=self._authorized.catalog,
            requestable_ids=self._authorized.requestable_ids,
            hits=authorized_hits,
            limit=query.limit,
            memory_view=self._memory_view,
            kernel_tools=kernel,
        )
        loaded = [
            entry
            for entry in plan.entries
            if entry.descriptor.model_name not in current
        ]
        if any(is_memory_write_entry(entry) for entry in loaded):
            self._exclusive_write = True
            if self._memory_view is not None:
                self._memory_view = self._memory_view.model_copy(
                    update={
                        "exclusive_namespace": "memory.state.write",
                        "transition_revision": self._memory_view.transition_revision + 1,
                    }
                )
        self._plan = plan
        conflict = self._apply_plan(plan)
        if conflict == SCHEMA_REVISION_CONFLICT:
            if not self.rebuild_after_schema_conflict():
                return {"ok": False, "error": SCHEMA_REVISION_CONFLICT}
            self._restart_provider_chain = True
        return {
            "ok": True,
            "data": {
                "loaded_tools": [
                    {
                        "name": entry.descriptor.model_name,
                        "namespace": entry.descriptor.namespace_id,
                        "description": entry.compact_description,
                    }
                    for entry in loaded
                ],
                "instruction": "下一步直接调用 loaded_tools 中的真实工具",
            },
        }

    def validate_call(self, name: str, arguments_json: str) -> tuple[bool, str | None]:
        if name == REQUEST_TOOLS_NAME:
            return True, None
        if name not in self._ledger.declared:
            return False, "undeclared_tool"
        if name not in self._ledger.callable_ids:
            return False, NO_LONGER_AUTHORIZED
        result = self._validator.validate(name, arguments_json)
        if not result.ok:
            return False, result.error_category or TOOL_INPUT_VALIDATION_FAILED
        return True, None

    def mark_side_effect(self) -> None:
        self._ledger.had_side_effect = True

    def can_rebuild_provider_chain(self) -> bool:
        return not self._ledger.had_side_effect

    def rebuild_after_schema_conflict(self) -> bool:
        """Restart the declared set only when no side effect has committed."""

        if self._ledger.had_side_effect or self._plan is None:
            return False
        self._ledger = DeclaredSchemaLedger(
            registry_revision=self._registry.revision,
            append_only=self._ledger.append_only,
        )
        self._restart_provider_chain = True
        return self._apply_plan(self._plan) is None

    def consume_provider_chain_restart(self) -> bool:
        restart = self._restart_provider_chain
        self._restart_provider_chain = False
        return restart

    def descriptor(self, name: str) -> CapabilityDescriptor | None:
        entry = self._authorized.catalog.by_model_name(name)
        return None if entry is None else entry.descriptor

    def requested_exclusive_write(self) -> bool:
        return self._exclusive_write

    @property
    def affinity_namespace_ids(self) -> tuple[str, ...]:
        return self._affinity

    def _apply_plan(self, plan: ExposurePlan) -> str | None:
        extra = plan.kernel_tools
        if self._append_request_tools(extra) and REQUEST_TOOLS_NAME not in {
            tool.name for tool in extra
        }:
            extra = (*extra, request_tools_definition())
        return self._ledger.declare(
            plan.entries,
            extra_tools=extra,
            callable_ids=plan.callable_ids,
        )

    def _append_request_tools(self, tools: list[ChatTool] | tuple[ChatTool, ...]) -> bool:
        if self._policy_context.tools_closed or self._exclusive_write:
            return False
        exposed = {tool.name for tool in tools} | set(self._ledger.declared)
        return any(
            entry.descriptor.model_name not in exposed
            for entry in self._authorized.catalog.entries
            if entry.descriptor.model_name in self._authorized.requestable_ids
        )

    def _notify_searched(
        self,
        query: CapabilityQuery,
        hits: tuple[CapabilitySearchHit, ...],
        started: float,
    ) -> None:
        if self._on_searched is None:
            return
        self._on_searched(
            CapabilitySearchReport(
                origin=query.origin.value,
                hit_count=len(hits),
                latency_ms=int((time.perf_counter() - started) * 1000),
                capability_ids=tuple(hit.capability_id for hit in hits),
            )
        )

    def _search_local(
        self, query: CapabilityQuery, *, limit: int
    ) -> tuple[CapabilitySearchHit, ...]:
        text = query.text.strip()
        if query.reply_excerpt:
            text = f"{text} {query.reply_excerpt[:500]}"
        hits = self._index.search(
            text,
            limit=max(limit, 10),
            affinity_namespace_ids=query.affinity_namespace_ids or self._affinity,
        )
        return tuple(
            hit for hit in hits if hit.capability_id in self._authorized.requestable_ids
        )[:limit]

    async def _hydrate_lazy_mcp(self, query: CapabilityQuery) -> None:
        if self._ensure_metadata is None:
            return
        has_synthetic = any(
            (entry.descriptor.provider_metadata or {}).get("synthetic")
            for entry in self._authorized.catalog.entries
        )
        if not has_synthetic:
            return
        hits = self._search_local(query, limit=10)
        servers = [
            self._server_id(hit.capability_id)
            for hit in hits
            if hit.synthetic and self._server_id(hit.capability_id)
        ]
        if not servers:
            return
        discovered: list[str] = []
        for server_id in tuple(dict.fromkeys(servers))[:2]:
            try:
                await self._ensure_metadata(server_id)
            except (OSError, RuntimeError, TimeoutError, ValueError):
                continue
            discovered.append(f"mcp.{server_id}")
        if not discovered:
            return
        self._discovered_mcp_providers = tuple(
            dict.fromkeys((*self._discovered_mcp_providers, *discovered))
        )
        if self._refresh_registry is None:
            return
        registry, index = self._refresh_registry()
        self._registry = registry
        self._index = index
        self._ledger.registry_revision = registry.revision
        self._authorized = self._project_authorized()
        quarantined = self._validator.admit(self._authorized.catalog.entries)
        if quarantined:
            requestable = frozenset(
                item for item in self._authorized.requestable_ids if item not in set(quarantined)
            )
            self._authorized = replace(self._authorized, requestable_ids=requestable)

    def _project_authorized(self) -> AuthorizedCatalogSnapshot:
        visible = self._policy.visible(
            tuple(entry.descriptor for entry in self._registry.catalog.entries),
            self._policy_context,
        )
        visible_names = {item.model_name for item in visible}
        catalog = UnifiedToolCatalog(
            entries=tuple(
                entry
                for entry in self._registry.catalog.entries
                if entry.descriptor.model_name in visible_names
            ),
            scopes=self._registry.catalog.scopes,
            revision=self._registry.revision,
        )
        return AuthorizedCatalogSnapshot(
            registry_revision=self._registry.revision,
            catalog=catalog,
            requestable_ids=frozenset(visible_names),
        )

    @staticmethod
    def _server_id(capability_id: str) -> str:
        if capability_id.startswith("mcp__"):
            parts = capability_id.split("__", 2)
            return parts[1] if len(parts) > 1 else ""
        return ""


def _bounded_text(value: str, maximum: int) -> str:
    text = value.strip()
    if len(text) <= maximum:
        return text
    return text[:maximum].rstrip()


def _document_from_entry(entry: UnifiedToolCatalogEntry) -> CapabilitySearchDocument:
    descriptor = entry.descriptor
    namespace_id = descriptor.namespace_id
    if not is_valid_namespace_id(namespace_id):
        namespace_id = "plugin.unnamed"
    namespace = lookup_namespace(namespace_id)
    properties = descriptor.input_schema.get("properties")
    parameter_names: tuple[str, ...] = ()
    parameter_descriptions: tuple[str, ...] = ()
    if isinstance(properties, dict):
        parameter_names = tuple(str(name) for name in properties)
        descriptions: list[str] = []
        for spec in properties.values():
            if isinstance(spec, dict):
                descriptions.append(str(spec.get("description") or ""))
        parameter_descriptions = tuple(item for item in descriptions if item)
    synthetic = bool((descriptor.provider_metadata or {}).get("synthetic"))
    model_name = _bounded_text(descriptor.model_name, 64) or "tool"
    return CapabilitySearchDocument(
        capability_id=_bounded_text(descriptor.model_name, 128) or model_name,
        model_name=model_name,
        canonical_name=_bounded_text(descriptor.canonical_name, 256) or model_name,
        namespace_id=namespace_id,
        namespace_description=_bounded_text(
            "" if namespace is None else namespace.description,
            500,
        ),
        description=_bounded_text(
            entry.compact_description or descriptor.compact_description or descriptor.description,
            SEARCH_DOCUMENT_BODY_MAX,
        )
        or model_name,
        aliases=descriptor.aliases,
        tags=descriptor.tags,
        use_when=descriptor.use_when,
        parameter_names=parameter_names,
        parameter_descriptions=tuple(
            _bounded_text(item, 80) for item in parameter_descriptions[:12]
        ),
        provider_id=_bounded_text(entry.provider_id, 128),
        trust_source=descriptor.trust_source,
        effect=descriptor.effect,
        risk=descriptor.risk,
        estimated_schema_tokens=max(1, entry.estimated_schema_tokens),
        synthetic=synthetic,
    )


__all__ = [
    "CapabilityIndexCache",
    "CapabilityQuery",
    "CapabilitySearchReport",
    "TurnCapabilityRuntime",
]
