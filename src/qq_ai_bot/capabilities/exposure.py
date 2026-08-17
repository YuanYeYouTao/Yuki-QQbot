"""Monotonic exposure ledger and authority-first exposure planning (R3 §6-8)."""

from __future__ import annotations

from dataclasses import dataclass, field

from qq_ai_bot.capabilities.catalog import UnifiedToolCatalog, UnifiedToolCatalogEntry
from qq_ai_bot.capabilities.models import (
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityTrustSource,
)
from qq_ai_bot.capabilities.request import REQUEST_TOOLS_NAME
from qq_ai_bot.capabilities.search_index import CapabilitySearchHit
from qq_ai_bot.domain.messages import ChatTool
from qq_ai_bot.runtime.contracts import CapabilityExposureSnapshot, MemoryCapabilityView

RESIDENT_KERNEL_TOOLS = frozenset({REQUEST_TOOLS_NAME})
CONDITIONAL_KERNEL_TOOLS = frozenset(
    {"get_my_capabilities", "read_tool_artifact", "set_reply_target"}
)
DEFAULT_LEXICAL_CANDIDATE_LIMIT = 10
DEFAULT_NON_RESIDENT_LIMIT = 8
DEFAULT_FIRST_ROUND_HARD_CAP = 12
SCHEMA_REVISION_CONFLICT = "capability_schema_revision_conflict"
NO_LONGER_AUTHORIZED = "capability_no_longer_authorized"

_PERMISSION_QUERY_HINTS = (
    "权限",
    "能改什么",
    "能做什么",
    "你会什么",
    "有哪些设置",
    "能改多少",
    "capability",
    "permission",
)


@dataclass(frozen=True, slots=True)
class DeclaredSchemaRecord:
    capability_id: str
    schema_fingerprint: str
    chat_tool: ChatTool


@dataclass(slots=True)
class DeclaredSchemaLedger:
    """Per-turn declared schemas vs currently callable ids."""

    registry_revision: str
    append_only: bool
    declared: dict[str, DeclaredSchemaRecord] = field(default_factory=dict)
    callable_ids: set[str] = field(default_factory=set)
    schema_token_total: int = 0
    conflict: str | None = None
    had_side_effect: bool = False

    def declare(
        self,
        entries: tuple[UnifiedToolCatalogEntry, ...],
        *,
        extra_tools: tuple[ChatTool, ...] = (),
        callable_ids: frozenset[str],
    ) -> str | None:
        """Merge newly exposed tools.  Returns a conflict code or None."""

        if self.conflict is not None:
            return self.conflict
        if self.append_only:
            for entry in entries:
                tool = entry.descriptor.as_chat_tool()
                record = DeclaredSchemaRecord(
                    capability_id=entry.descriptor.model_name,
                    schema_fingerprint=entry.revision,
                    chat_tool=tool,
                )
                existing = self.declared.get(record.capability_id)
                if (
                    existing is not None
                    and existing.schema_fingerprint != record.schema_fingerprint
                ):
                    self.conflict = SCHEMA_REVISION_CONFLICT
                    return self.conflict
                if existing is None:
                    self.declared[record.capability_id] = record
                    self.schema_token_total += entry.estimated_schema_tokens
            for tool in extra_tools:
                if tool.name not in self.declared:
                    self.declared[tool.name] = DeclaredSchemaRecord(
                        capability_id=tool.name,
                        schema_fingerprint="kernel",
                        chat_tool=tool,
                    )
        else:
            self.declared = {
                entry.descriptor.model_name: DeclaredSchemaRecord(
                    capability_id=entry.descriptor.model_name,
                    schema_fingerprint=entry.revision,
                    chat_tool=entry.descriptor.as_chat_tool(),
                )
                for entry in entries
            }
            for tool in extra_tools:
                self.declared[tool.name] = DeclaredSchemaRecord(
                    capability_id=tool.name,
                    schema_fingerprint="kernel",
                    chat_tool=tool,
                )
            self.schema_token_total = sum(entry.estimated_schema_tokens for entry in entries)
        self.callable_ids = set(callable_ids)
        if REQUEST_TOOLS_NAME in {tool.name for tool in extra_tools}:
            self.callable_ids.add(REQUEST_TOOLS_NAME)
        return None

    def declared_tools(self) -> tuple[ChatTool, ...]:
        return tuple(record.chat_tool for record in self.declared.values())

    def snapshot(self) -> CapabilityExposureSnapshot:
        revision = int(self.registry_revision[:8], 16) if self.registry_revision else 0
        return CapabilityExposureSnapshot(
            revision=revision,
            exposed_capability_ids=tuple(sorted(self.declared)),
            requestable_capability_ids=tuple(sorted(self.callable_ids)),
            schema_token_estimate=self.schema_token_total,
        )


@dataclass(frozen=True, slots=True)
class ExposurePlan:
    entries: tuple[UnifiedToolCatalogEntry, ...]
    kernel_tools: tuple[ChatTool, ...]
    callable_ids: frozenset[str]
    omitted_count: int
    reason: str = "ready"


class AuthorityFirstExposurePlanner:
    """Select kernel tools plus lexical candidates under schema/count caps."""

    def __init__(
        self,
        *,
        lexical_candidate_limit: int = DEFAULT_LEXICAL_CANDIDATE_LIMIT,
        non_resident_limit: int = DEFAULT_NON_RESIDENT_LIMIT,
        first_round_hard_cap: int = DEFAULT_FIRST_ROUND_HARD_CAP,
        schema_token_budget: int | None = None,
        mcp_schema_token_budget: int | None = None,
        mcp_tool_limit: int | None = None,
    ) -> None:
        self._lexical_limit = lexical_candidate_limit
        self._non_resident_limit = non_resident_limit
        self._hard_cap = first_round_hard_cap
        self._schema_token_budget = schema_token_budget
        self._mcp_schema_token_budget = mcp_schema_token_budget
        self._mcp_tool_limit = mcp_tool_limit

    def plan_initial(
        self,
        *,
        catalog: UnifiedToolCatalog,
        requestable_ids: frozenset[str],
        hits: tuple[CapabilitySearchHit, ...],
        memory_view: MemoryCapabilityView | None,
        kernel_tools: tuple[ChatTool, ...],
        query: str,
        artifact_available: bool,
        reply_target_available: bool,
        priority_ids: tuple[str, ...] = (),
        priority_provider_ids: tuple[str, ...] = (),
    ) -> ExposurePlan:
        by_id = {entry.descriptor.model_name: entry for entry in catalog.entries}
        selected: list[UnifiedToolCatalogEntry] = []
        selected_ids: set[str] = set()

        def add(entry: UnifiedToolCatalogEntry | None) -> None:
            if entry is None or entry.descriptor.model_name in selected_ids:
                return
            if entry.descriptor.model_name not in requestable_ids:
                return
            if _is_synthetic(entry):
                return
            selected.append(entry)
            selected_ids.add(entry.descriptor.model_name)

        if memory_view is not None:
            eager = set(memory_view.eager_namespaces)
            for entry in catalog.entries:
                if entry.descriptor.namespace_id in eager:
                    add(entry)

        permission_query = _looks_like_permission_query(query)
        for name in CONDITIONAL_KERNEL_TOOLS:
            entry = by_id.get(name)
            if name == "get_my_capabilities" and not permission_query:
                continue
            if name == "read_tool_artifact" and not artifact_available:
                continue
            if name == "set_reply_target" and not reply_target_available:
                continue
            add(entry)

        for name in priority_ids:
            add(by_id.get(name))

        mcp_providers = {
            provider_id for provider_id in priority_provider_ids if provider_id.strip()
        }
        for hit in hits:
            entry = by_id.get(hit.capability_id)
            if entry is not None and entry.descriptor.trust_source is CapabilityTrustSource.MCP:
                mcp_providers.add(entry.provider_id)
        mcp_count = 0
        mcp_tokens = 0
        if mcp_providers:
            for entry in catalog.entries:
                if entry.provider_id not in mcp_providers:
                    continue
                if self._mcp_tool_limit is not None and mcp_count >= self._mcp_tool_limit:
                    break
                next_tokens = mcp_tokens + entry.estimated_schema_tokens
                if (
                    self._mcp_schema_token_budget is not None
                    and next_tokens > self._mcp_schema_token_budget
                ):
                    continue
                before = entry.descriptor.model_name in selected_ids
                add(entry)
                if not before and entry.descriptor.model_name in selected_ids:
                    mcp_count += 1
                    mcp_tokens = next_tokens

        non_resident = 0
        for hit in hits[: self._lexical_limit]:
            if non_resident >= self._non_resident_limit:
                break
            if hit.capability_id in selected_ids:
                continue
            add(by_id.get(hit.capability_id))
            if (
                hit.capability_id in selected_ids
                and hit.capability_id not in CONDITIONAL_KERNEL_TOOLS
            ):
                non_resident += 1

        selected = _clip_budget(
            selected,
            hard_cap=max(0, self._hard_cap - len(kernel_tools)),
            schema_token_budget=self._schema_token_budget,
        )
        callable_ids = frozenset(item.descriptor.model_name for item in selected) | frozenset(
            tool.name for tool in kernel_tools
        )
        if memory_view is not None and memory_view.exclusive_namespace:
            callable_ids = _restrict_exclusive_write(selected, kernel_tools, memory_view)
            selected = tuple(
                item
                for item in selected
                if item.descriptor.model_name in callable_ids
                or item.descriptor.namespace_id in memory_view.eager_namespaces
            )
            selected_ids = {item.descriptor.model_name for item in selected}
        omitted = sum(
            1 for item in catalog.entries if item.descriptor.model_name not in selected_ids
        )
        return ExposurePlan(
            entries=tuple(selected),
            kernel_tools=kernel_tools,
            callable_ids=callable_ids,
            omitted_count=omitted,
        )

    def plan_growth(
        self,
        *,
        current_ids: frozenset[str],
        catalog: UnifiedToolCatalog,
        requestable_ids: frozenset[str],
        hits: tuple[CapabilitySearchHit, ...],
        limit: int,
        memory_view: MemoryCapabilityView | None,
        kernel_tools: tuple[ChatTool, ...],
    ) -> ExposurePlan:
        by_id = {entry.descriptor.model_name: entry for entry in catalog.entries}
        added: list[UnifiedToolCatalogEntry] = []
        for hit in hits:
            if len(added) >= limit:
                break
            if hit.capability_id in current_ids or hit.capability_id not in requestable_ids:
                continue
            entry = by_id.get(hit.capability_id)
            if entry is None:
                continue
            if _is_synthetic(entry):
                continue
            added.append(entry)
        kept = [by_id[name] for name in current_ids if name in by_id]
        selected = (*kept, *added)
        callable_ids = frozenset(item.descriptor.model_name for item in selected) | frozenset(
            tool.name for tool in kernel_tools
        )
        if memory_view is not None and memory_view.exclusive_namespace:
            callable_ids = _restrict_exclusive_write(selected, kernel_tools, memory_view)
        return ExposurePlan(
            entries=tuple(selected),
            kernel_tools=kernel_tools,
            callable_ids=callable_ids,
            omitted_count=max(0, len(hits) - len(added)),
            reason="request_tools",
        )


def _is_synthetic(entry: UnifiedToolCatalogEntry) -> bool:
    return bool((entry.descriptor.provider_metadata or {}).get("synthetic"))


def _looks_like_permission_query(query: str) -> bool:
    folded = query.casefold()
    return any(hint.casefold() in folded for hint in _PERMISSION_QUERY_HINTS)


def _clip_budget(
    entries: list[UnifiedToolCatalogEntry],
    *,
    hard_cap: int,
    schema_token_budget: int | None,
) -> list[UnifiedToolCatalogEntry]:
    selected: list[UnifiedToolCatalogEntry] = []
    used = 0
    for entry in entries:
        if len(selected) >= hard_cap:
            break
        next_used = used + entry.estimated_schema_tokens
        if schema_token_budget is not None and next_used > schema_token_budget:
            continue
        selected.append(entry)
        used = next_used
    return selected


def _restrict_exclusive_write(
    entries: tuple[UnifiedToolCatalogEntry, ...] | list[UnifiedToolCatalogEntry],
    kernel_tools: tuple[ChatTool, ...],
    memory_view: MemoryCapabilityView,
) -> frozenset[str]:
    allowed: set[str] = {tool.name for tool in kernel_tools}
    exclusive = memory_view.exclusive_namespace
    eager = set(memory_view.eager_namespaces)
    for entry in entries:
        namespace = entry.descriptor.namespace_id
        effect = entry.descriptor.effect
        if namespace == exclusive:
            allowed.add(entry.descriptor.model_name)
            continue
        if namespace in eager and effect is not CapabilityEffect.WRITE_STATE:
            allowed.add(entry.descriptor.model_name)
            continue
        if effect is CapabilityEffect.WRITE_STATE:
            continue
        if entry.descriptor.model_name in CONDITIONAL_KERNEL_TOOLS:
            allowed.add(entry.descriptor.model_name)
    return frozenset(allowed)


def is_memory_write_entry(entry: UnifiedToolCatalogEntry) -> bool:
    return (
        entry.descriptor.namespace_id == "memory.state.write"
        or (
            entry.descriptor.model_name == "memory_change"
            and entry.descriptor.effect is CapabilityEffect.WRITE_STATE
        )
    )


def descriptor_is_business_write(descriptor: CapabilityDescriptor) -> bool:
    return descriptor.effect in {
        CapabilityEffect.WRITE_STATE,
        CapabilityEffect.PLATFORM_MUTATE,
        CapabilityEffect.PLATFORM_SEND,
    } and descriptor.namespace_id != "memory.state.write"
