"""Dynamic scope selection and whole-schema budgeting."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from qq_ai_bot.capabilities.catalog import UnifiedToolCatalog, UnifiedToolCatalogEntry
from qq_ai_bot.capabilities.models import CapabilityExposure


class ToolSelectionMode(StrEnum):
    ALL = "all"
    CATALOG = "catalog"
    HYBRID = "hybrid"
    GATEWAY = "gateway"


class UnknownToolScopeError(ValueError):
    pass


class ToolBundleBudgetError(ValueError):
    """A selected bundle cannot be represented without dropping required tools."""


@dataclass(frozen=True, slots=True)
class SchemaBudgetResult:
    entries: tuple[UnifiedToolCatalogEntry, ...]
    estimated_tokens: int
    omitted_count: int


@dataclass(frozen=True, slots=True)
class ToolCandidateResult:
    entries: tuple[UnifiedToolCatalogEntry, ...]
    scores: tuple[tuple[str, int], ...]


class ToolCandidateSelector:
    """Rank catalog entries locally without requiring an LLM or full schemas."""

    def select(
        self,
        catalog: UnifiedToolCatalog,
        *,
        scopes: tuple[str, ...] = (),
        user_request: str = "",
        limit: int | None = None,
        minimum_score: int | None = None,
    ) -> ToolCandidateResult:
        if limit is not None and limit <= 0:
            raise ValueError("tool candidate limit must be positive or null")
        if minimum_score is not None and minimum_score < 0:
            raise ValueError("minimum tool candidate score must not be negative")
        known = {scope.scope_id for scope in catalog.scopes}
        unknown = sorted(set(scopes) - known)
        if unknown:
            raise UnknownToolScopeError(f"unknown tool scopes: {', '.join(unknown)}")
        terms = _query_terms(user_request)
        ranked: list[tuple[int, UnifiedToolCatalogEntry]] = []
        for entry in catalog.entries:
            if (
                scopes
                and not any(scope in entry.scope_ids for scope in scopes)
                and entry.descriptor.exposure is not CapabilityExposure.DIRECT_ALWAYS
            ):
                continue
            score = sum(_term_score(term, entry) for term in terms)
            if any(scope in entry.scope_ids for scope in scopes):
                score += 20
            if minimum_score is not None and score < minimum_score:
                continue
            ranked.append((score, entry))
        ranked.sort(key=lambda item: (-item[0], item[1].descriptor.model_name))
        if limit is not None:
            required = _required_entries(
                tuple(item[1] for item in ranked),
                scopes=scopes,
            )
            selected = [item for item in ranked[:limit]]
            selected_names = {item.descriptor.model_name for _score, item in selected}
            selected.extend(
                (0, item) for item in required if item.descriptor.model_name not in selected_names
            )
            ranked = selected
        return ToolCandidateResult(
            entries=tuple(item[1] for item in ranked),
            scores=tuple((item.descriptor.model_name, score) for score, item in ranked),
        )


class ToolSchemaBudgeter:
    """Select complete schemas only; never truncate an individual JSON Schema."""

    def __init__(
        self,
        *,
        selected_tool_limit: int | None,
        schema_token_budget: int | None,
    ) -> None:
        if selected_tool_limit is not None and selected_tool_limit <= 0:
            raise ValueError("selected tool limit must be positive or null")
        if schema_token_budget is not None and schema_token_budget <= 0:
            raise ValueError("schema token budget must be positive or null")
        self._selected_tool_limit = selected_tool_limit
        self._schema_token_budget = schema_token_budget

    def select(
        self,
        catalog: UnifiedToolCatalog,
        *,
        scopes: tuple[str, ...] = (),
        query: str = "",
    ) -> SchemaBudgetResult:
        known = {scope.scope_id for scope in catalog.scopes}
        unknown = sorted(set(scopes) - known)
        if unknown:
            raise UnknownToolScopeError(f"unknown tool scopes: {', '.join(unknown)}")
        entries = [
            item
            for item in catalog.entries
            if not scopes
            or any(scope in item.scope_ids for scope in scopes)
            or item.descriptor.exposure is CapabilityExposure.DIRECT_ALWAYS
        ]
        required = _required_entries(tuple(entries), scopes=scopes)
        required_names = {item.descriptor.model_name for item in required}
        required_tokens = sum(item.estimated_schema_tokens for item in required)
        if self._schema_token_budget is not None and required_tokens > self._schema_token_budget:
            bundle_names = ", ".join(
                sorted(
                    {
                        scope
                        for item in required
                        for scope in item.bundle_scope_ids
                        if scope in scopes
                    }
                )
            )
            raise ToolBundleBudgetError(
                "selected tool bundle exceeds schema token budget"
                + (f": {bundle_names}" if bundle_names else "")
            )
        terms = tuple(token for token in query.casefold().split() if token)
        if terms:
            entries.sort(
                key=lambda item: (
                    -sum(term in item.searchable_text for term in terms),
                    item.descriptor.model_name,
                )
            )
        selected: list[UnifiedToolCatalogEntry] = list(required)
        used = required_tokens
        for item in entries:
            if item.descriptor.model_name in required_names:
                continue
            if self._selected_tool_limit is not None and len(selected) >= max(
                self._selected_tool_limit, len(required)
            ):
                break
            next_used = used + item.estimated_schema_tokens
            if self._schema_token_budget is not None and next_used > self._schema_token_budget:
                continue
            selected.append(item)
            used = next_used
        return SchemaBudgetResult(tuple(selected), used, len(entries) - len(selected))


def _required_entries(
    entries: tuple[UnifiedToolCatalogEntry, ...],
    *,
    scopes: tuple[str, ...],
) -> tuple[UnifiedToolCatalogEntry, ...]:
    selected_scopes = set(scopes)
    return tuple(
        item
        for item in entries
        if item.descriptor.exposure is CapabilityExposure.DIRECT_ALWAYS
        or bool(selected_scopes.intersection(item.bundle_scope_ids))
    )


def _query_terms(value: str) -> tuple[str, ...]:
    normalized = value.casefold()
    words = re.findall(r"[a-z0-9_.-]{2,}|[\u3400-\u9fff]{1,4}", normalized)
    return tuple(dict.fromkeys(words))


def _term_score(term: str, entry: UnifiedToolCatalogEntry) -> int:
    descriptor = entry.descriptor
    if term == descriptor.model_name.casefold() or term == descriptor.canonical_name.casefold():
        return 100
    if term in descriptor.model_name.casefold() or term in descriptor.canonical_name.casefold():
        return 30
    if term in {tag.casefold() for tag in entry.tags}:
        return 20
    if term in entry.searchable_text:
        return 8
    return 0
