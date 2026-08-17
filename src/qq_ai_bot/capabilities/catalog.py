"""Provider registry and immutable unified tool catalog snapshots."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from qq_ai_bot.capabilities.models import CapabilityDescriptor
from qq_ai_bot.domain.messages import ChatTool

_MODEL_NAME = re.compile(r"[^a-zA-Z0-9_-]+")


def descriptor_content_fingerprint(descriptor: CapabilityDescriptor) -> str:
    """Hash the full discovery+policy surface so index caches stay coherent."""

    payload = {
        "canonical_name": descriptor.canonical_name,
        "model_name": descriptor.model_name,
        "namespace": descriptor.namespace_id,
        "aliases": list(descriptor.aliases),
        "use_when": list(descriptor.use_when),
        "tags": list(descriptor.tags),
        "description": descriptor.description,
        "compact_description": descriptor.compact_description,
        "input_schema": descriptor.input_schema,
        "output_schema": descriptor.output_schema,
        "effect": descriptor.effect.value,
        "risk": descriptor.risk.value,
        "trust_source": descriptor.trust_source.value,
        "allowed_origins": sorted(origin.value for origin in descriptor.allowed_origins),
        "required_permissions": sorted(descriptor.required_permissions),
        "schema_version": descriptor.schema_version,
        "generation": descriptor.generation,
        "provider_id": descriptor.provider_id,
        "provider_tool_name": descriptor.provider_tool_name,
        "finalize_after_commit": descriptor.finalize_after_commit,
        "additional_scopes": list(descriptor.additional_scopes),
        "bundle_scopes": list(descriptor.bundle_scopes),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def estimate_chat_tool_tokens(tool: ChatTool) -> int:
    """Estimate the complete function-calling envelope with one stable heuristic."""

    envelope = {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }
    encoded = json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return max(1, (len(encoded) + 3) // 4)


def safe_model_tool_name(*parts: str, maximum: int = 64) -> str:
    """Build a provider-neutral model name with a stable collision suffix."""

    raw = "__".join(part.strip() for part in parts if part.strip())
    normalized = _MODEL_NAME.sub("_", raw).strip("_") or "tool"
    if len(normalized) <= maximum:
        return normalized
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{normalized[: maximum - len(digest) - 2]}__{digest}"


class ToolProvider(Protocol):
    @property
    def provider_id(self) -> str: ...

    def descriptors(self, context: Any) -> tuple[CapabilityDescriptor, ...]: ...

    async def refresh(self, *, force: bool = False) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class UnifiedToolCatalogEntry:
    descriptor: CapabilityDescriptor
    provider_id: str
    scope_ids: tuple[str, ...]
    compact_description: str
    tags: tuple[str, ...]
    searchable_text: str
    estimated_schema_tokens: int
    available: bool
    revision: str
    bundle_scope_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolScopeSummary:
    scope_id: str
    parent: str | None
    display_name: str
    description: str
    tool_count: int
    provider_ids: tuple[str, ...]
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UnifiedToolCatalog:
    entries: tuple[UnifiedToolCatalogEntry, ...]
    scopes: tuple[ToolScopeSummary, ...]
    revision: str

    def by_model_name(self, name: str) -> UnifiedToolCatalogEntry | None:
        return next((item for item in self.entries if item.descriptor.model_name == name), None)


@dataclass(frozen=True, slots=True)
class DescriptorRegistrySnapshot:
    """Context-free catalog used to cache the search index.

    ``revision`` is the full-content hash.  It must not include per-user
    authority projections.
    """

    catalog: UnifiedToolCatalog

    @property
    def revision(self) -> str:
        return self.catalog.revision

    def entry(self, capability_id: str) -> UnifiedToolCatalogEntry | None:
        return self.catalog.by_model_name(capability_id)


@dataclass(frozen=True, slots=True)
class AuthorizedCatalogSnapshot:
    """Per-turn authority/availability projection of one registry revision."""

    registry_revision: str
    catalog: UnifiedToolCatalog
    requestable_ids: frozenset[str]
    hidden_ids: frozenset[str] = frozenset()

    def entry(self, capability_id: str) -> UnifiedToolCatalogEntry | None:
        if capability_id not in self.requestable_ids:
            return None
        return self.catalog.by_model_name(capability_id)


class ToolProviderRegistry:
    """Own provider lifecycle and reject ambiguous model-facing names centrally."""

    def __init__(self) -> None:
        self._providers: dict[str, ToolProvider] = {}

    def register(self, provider: ToolProvider) -> None:
        if provider.provider_id in self._providers:
            raise ValueError(f"duplicate tool provider: {provider.provider_id}")
        self._providers[provider.provider_id] = provider

    def unregister(self, provider_id: str) -> None:
        self._providers.pop(provider_id, None)

    def providers(self) -> tuple[ToolProvider, ...]:
        return tuple(self._providers.values())

    def provider(self, provider_id: str) -> ToolProvider | None:
        return self._providers.get(provider_id)

    def descriptor(self, model_name: str, context: Any) -> CapabilityDescriptor | None:
        entry = self.catalog(context).by_model_name(model_name)
        return entry.descriptor if entry is not None else None

    def binding(self, model_name: str, context: Any) -> Any:
        descriptor = self.descriptor(model_name, context)
        return descriptor.binding if descriptor is not None else None

    def catalog(self, context: Any) -> UnifiedToolCatalog:
        entries: list[UnifiedToolCatalogEntry] = []
        names: set[str] = set()
        canonical_names: set[str] = set()
        for provider in self._providers.values():
            for descriptor in provider.descriptors(context):
                if descriptor.model_name in names:
                    raise ValueError(f"duplicate model capability: {descriptor.model_name}")
                if descriptor.canonical_name in canonical_names:
                    raise ValueError(f"duplicate canonical capability: {descriptor.canonical_name}")
                names.add(descriptor.model_name)
                canonical_names.add(descriptor.canonical_name)
                description = descriptor.compact_description or descriptor.description
                searchable = " ".join(
                    (
                        descriptor.model_name,
                        descriptor.canonical_name,
                        descriptor.namespace_id,
                        *descriptor.aliases,
                        *descriptor.use_when,
                        *descriptor.scope_ids,
                        description,
                        *descriptor.tags,
                    )
                ).casefold()
                entries.append(
                    UnifiedToolCatalogEntry(
                        descriptor=descriptor,
                        provider_id=descriptor.provider_id or provider.provider_id,
                        scope_ids=descriptor.scope_ids,
                        compact_description=description,
                        tags=descriptor.tags,
                        searchable_text=searchable,
                        estimated_schema_tokens=estimate_chat_tool_tokens(
                            descriptor.as_chat_tool()
                        ),
                        available=True,
                        revision=descriptor_content_fingerprint(descriptor),
                        bundle_scope_ids=descriptor.bundle_scopes,
                    )
                )
        entries.sort(key=lambda item: (item.scope_ids, item.descriptor.model_name))
        scopes: list[ToolScopeSummary] = []
        for scope_id in sorted({scope for entry in entries for scope in entry.scope_ids}):
            selected = [entry for entry in entries if scope_id in entry.scope_ids]
            summaries = dict(
                pair for entry in selected for pair in entry.descriptor.scope_summaries
            )
            scopes.append(
                ToolScopeSummary(
                    scope_id=scope_id,
                    parent=scope_id.rpartition(".")[0] or None,
                    display_name=scope_id,
                    description=summaries.get(scope_id, f"{scope_id} tools"),
                    tool_count=len(selected),
                    provider_ids=tuple(sorted({item.provider_id for item in selected})),
                    tags=tuple(sorted({tag for item in selected for tag in item.tags})),
                )
            )
        digest = hashlib.sha256(
            "\n".join(
                f"{item.provider_id}:{item.descriptor.model_name}:{item.revision}"
                for item in entries
            ).encode("utf-8")
        ).hexdigest()
        return UnifiedToolCatalog(tuple(entries), tuple(scopes), digest)

    async def refresh(self, *, force: bool = False) -> None:
        for provider in self._providers.values():
            await provider.refresh(force=force)

    async def close(self) -> None:
        for provider in reversed(self._providers.values()):
            await provider.close()
