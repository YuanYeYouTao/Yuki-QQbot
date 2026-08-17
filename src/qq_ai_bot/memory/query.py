"""Backend-owned query construction for Memory V2 retrieval."""

from __future__ import annotations

import re
import unicodedata

from pydantic import ValidationError

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.memory.enums import (
    MemoryContextMode,
    MemoryRecallPurpose,
    MemoryRetrievalMode,
    MemorySubjectRole,
    MemoryTargetRole,
)
from qq_ai_bot.memory.errors import MemoryRetrievalError
from qq_ai_bot.memory.models import MemoryEntityTarget, MemoryQuery, MemoryQueryIntent
from qq_ai_bot.memory.targets import MemoryTargetResolver

_WHITESPACE = re.compile(r"\s+")


def normalize_query_text(value: str, *, maximum: int = 1200) -> str:
    """Normalize text for safe FTS construction, never for intent classification."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return _WHITESPACE.sub(" ", normalized).strip()[:maximum]


class MemoryQueryBuilder:
    """Build a strict query from current-event text and structured Planner intent."""

    def __init__(self, targets: MemoryTargetResolver) -> None:
        self._targets = targets

    async def resolve_targets(
        self,
        inbound: InboundMessage,
        *,
        max_referenced: int,
        self_recall: bool = False,
    ) -> tuple[MemoryEntityTarget, ...]:
        return await self._targets.resolve(
            inbound,
            max_referenced=max_referenced,
            include_self=self_recall,
        )

    async def build(
        self,
        *,
        inbound: InboundMessage,
        content: str,
        runtime: RuntimeConfigSnapshot,
        planner_intent: str = "",
        memory_mode: MemoryContextMode = MemoryContextMode.HYBRID,
        self_recall: bool = False,
        memory_intent: MemoryQueryIntent | None = None,
    ) -> MemoryQuery:
        # Kept only as a source-compatible input. Generic TurnPlan.intent must not
        # affect memory retrieval.
        del planner_intent
        intent = memory_intent or MemoryQueryIntent(
            mode=memory_mode,
            purpose=MemoryRecallPurpose.BACKGROUND,
            subjects=(MemorySubjectRole.CURRENT_SELF,) if self_recall else (),
        )
        targets = await self.resolve_targets(
            inbound,
            max_referenced=runtime.memory.max_referenced_targets,
            self_recall=intent.self_recall and runtime.memory.self_enabled,
        )
        mode = (
            MemoryRetrievalMode.OVERVIEW
            if intent.mode is MemoryContextMode.OVERVIEW
            else MemoryRetrievalMode.RELEVANT
        )
        if mode is MemoryRetrievalMode.OVERVIEW:
            targets = tuple(
                target
                for target in targets
                if target.role
                in {
                    MemoryTargetRole.CURRENT_PERSON,
                    MemoryTargetRole.CURRENT_SELF,
                    MemoryTargetRole.CURRENT_PERSON_GROUP,
                    MemoryTargetRole.CURRENT_GROUP,
                }
            )

        parts = [content]
        if inbound.reply_text:
            parts.append(inbound.reply_text[:500])
        if intent.entities:
            parts.append(" ".join(intent.entities))
        text = "\n".join(part for part in parts if part.strip())
        query = self.for_targets(
            text=text,
            mode=mode,
            targets=targets,
            runtime=runtime,
            intent=intent,
        )
        if intent.mode in {MemoryContextMode.LEXICAL, MemoryContextMode.OVERVIEW}:
            query = query.model_copy(update={"semantic_enabled": False})
        return query

    @staticmethod
    def for_targets(
        *,
        text: str,
        mode: MemoryRetrievalMode,
        targets: tuple[MemoryEntityTarget, ...],
        runtime: RuntimeConfigSnapshot,
        limit: int | None = None,
        intent: MemoryQueryIntent | None = None,
    ) -> MemoryQuery:
        """Build a query inside pre-resolved targets.

        Calls without Planner intent are management/plugin reads and retain the
        legacy neutral ordering.
        """

        memory = runtime.memory
        default_limit = (
            memory.overview_limit_per_entity
            if mode is MemoryRetrievalMode.OVERVIEW
            else memory.context_limit_per_entity
        )
        try:
            query = MemoryQuery(
                text=text[:1200],
                normalized_text=normalize_query_text(text),
                mode=mode,
                targets=targets,
                # Planner kind preferences are soft rerank signals, never a
                # candidate filter that could hide otherwise exact memories.
                kinds=(),
                candidate_limit=memory.lexical_candidate_limit,
                limit_per_target=limit if limit is not None else default_limit,
                always_on_explicit_preference_limit=(memory.always_on_explicit_preference_limit),
                query_term_limit=memory.query_term_limit,
                short_query_fallback_enabled=memory.short_query_fallback_enabled,
                semantic_enabled=memory.semantic_enabled,
                semantic_candidate_limit=memory.semantic_candidate_limit,
                semantic_min_similarity=memory.semantic_min_similarity,
                hybrid_lexical_weight=memory.hybrid_lexical_weight,
                hybrid_semantic_weight=memory.hybrid_semantic_weight,
                hybrid_rrf_k=memory.hybrid_rrf_k,
                intent=intent,
                intent_rerank_enabled=(memory.intent_rerank_enabled if intent else False),
                activation_ranking_enabled=(memory.activation_ranking_enabled if intent else False),
                activation_half_life_episode_days=(memory.activation_half_life_episode_days),
                activation_half_life_fact_days=memory.activation_half_life_fact_days,
                activation_half_life_preference_days=(memory.activation_half_life_preference_days),
                activation_half_life_explicit_days=(memory.activation_half_life_explicit_days),
                intent_recent_window_days=memory.intent_recent_window_days,
                recall_trace_candidate_limit=memory.recall_trace_candidate_limit,
            )
        except ValidationError as exc:
            raise MemoryRetrievalError("memory_query_invalid") from exc
        if intent is not None and intent.mode in {
            MemoryContextMode.LEXICAL,
            MemoryContextMode.OVERVIEW,
        }:
            return query.model_copy(update={"semantic_enabled": False})
        return query
