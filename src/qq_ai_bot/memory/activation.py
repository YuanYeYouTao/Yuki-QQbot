"""Lazy recall activation and structured-intent reranking."""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult

from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryKind,
    MemoryRecallPurpose,
    MemorySourceType,
    MemoryStatus,
    MemorySubjectRole,
    MemoryTargetRole,
    MemoryTemporalConstraint,
    MemoryTemporalIntentMode,
)
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.models import (
    MemoryActivationState,
    MemoryFact,
    MemoryQuery,
    MemoryQueryIntent,
    MemoryRetrievalHit,
)
from qq_ai_bot.memory.query import normalize_query_text
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    MemoryActivationStateModel,
    MemoryFactModel,
    MemoryRecallItemModel,
    MemoryRecallReceiptModel,
)

logger = logging.getLogger(__name__)

_RERANK_WEIGHTS: dict[MemoryRecallPurpose, tuple[float, ...]] = {
    MemoryRecallPurpose.BACKGROUND: (0.50, 0.10, 0.10, 0.05, 0.05, 0.20),
    MemoryRecallPurpose.CONTINUATION: (0.50, 0.10, 0.10, 0.15, 0.05, 0.10),
    MemoryRecallPurpose.RECALL: (0.65, 0.10, 0.10, 0.05, 0.05, 0.05),
    MemoryRecallPurpose.VERIFY: (0.65, 0.10, 0.05, 0.05, 0.05, 0.10),
    MemoryRecallPurpose.CORRECT: (0.70, 0.10, 0.10, 0.05, 0.05, 0.00),
}


def initial_activation(fact: MemoryFact) -> float:
    if fact.source_type is MemorySourceType.EXPLICIT or fact.authority is MemoryAuthority.EXPLICIT:
        return 0.95
    if fact.kind is MemoryKind.PREFERENCE:
        return 0.80
    if fact.kind is MemoryKind.EPISODE:
        return 0.75 if fact.importance >= 4 else 0.65
    return 0.70


def activation_half_life_days(fact: MemoryFact, query: MemoryQuery) -> float:
    if fact.source_type is MemorySourceType.EXPLICIT or fact.authority is MemoryAuthority.EXPLICIT:
        half_life = query.activation_half_life_explicit_days
    elif fact.kind is MemoryKind.EPISODE:
        half_life = query.activation_half_life_episode_days
    elif fact.kind is MemoryKind.PREFERENCE:
        half_life = query.activation_half_life_preference_days
    else:
        half_life = query.activation_half_life_fact_days
    if fact.importance >= 4:
        half_life *= 2
    if fact.source_type is MemorySourceType.AUTOMATIC and (
        fact.confidence < 0.7 or fact.importance <= 2
    ):
        half_life *= 0.5
    return half_life


def effective_activation(
    state: MemoryActivationState,
    fact: MemoryFact,
    query: MemoryQuery,
    *,
    now: datetime,
) -> float:
    anchor = state.activation_updated_at
    elapsed_days = max(0.0, (now - anchor).total_seconds() / 86_400)
    half_life = activation_half_life_days(fact, query)
    return max(0.0, min(1.0, state.activation * math.exp(-math.log(2) * elapsed_days / half_life)))


class MemoryActivationRepository:
    """Persist Activation separately from fact truth and validity."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def load(self, fact_ids: tuple[int, ...]) -> dict[int, MemoryActivationState]:
        unique_ids = tuple(dict.fromkeys(fact_ids))
        if not unique_ids:
            return {}
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(MemoryActivationStateModel).where(
                        MemoryActivationStateModel.fact_id.in_(unique_ids)
                    )
                )
            ).all()
        return {
            row.fact_id: MemoryActivationState(
                fact_id=row.fact_id,
                activation=row.activation,
                activation_updated_at=row.activation_updated_at,
                last_recalled_at=row.last_recalled_at,
                recall_count=row.recall_count,
                revision=row.revision,
            )
            for row in rows
        }

    async def reinforce(
        self,
        fact_ids: tuple[int, ...],
        *,
        alpha: float,
        query: MemoryQuery,
        now: datetime | None = None,
        max_attempts: int = 3,
        receipt_turn_id: str | None = None,
    ) -> tuple[int, ...]:
        """Apply bounded CAS updates after delivery; inactive facts are skipped."""

        if alpha <= 0:
            return ()
        timestamp = now or datetime.now(UTC)
        reinforced: list[int] = []
        for fact_id in dict.fromkeys(fact_ids):
            for _attempt in range(max_attempts):
                async with self._database.sessions() as session, session.begin():
                    receipt_id = None
                    if receipt_turn_id is not None:
                        receipt_id = await session.scalar(
                            select(MemoryRecallReceiptModel.id).where(
                                MemoryRecallReceiptModel.turn_id == receipt_turn_id
                            )
                        )
                        if receipt_id is None:
                            break
                        claim = await session.execute(
                            update(MemoryRecallItemModel)
                            .where(
                                MemoryRecallItemModel.receipt_id == receipt_id,
                                MemoryRecallItemModel.fact_id == fact_id,
                                MemoryRecallItemModel.used.is_(True),
                                MemoryRecallItemModel.reinforced.is_(False),
                            )
                            .values(reinforced=True, reinforced_at=timestamp)
                        )
                        if int(cast(CursorResult[Any], claim).rowcount or 0) != 1:
                            break
                    row = await session.get(MemoryActivationStateModel, fact_id)
                    fact_row = await session.get(MemoryFactModel, fact_id)
                    if (
                        row is None
                        or fact_row is None
                        or fact_row.status != MemoryStatus.ACTIVE.value
                        or fact_row.review_state == "quarantined"
                    ):
                        if receipt_id is not None:
                            await session.execute(
                                update(MemoryRecallItemModel)
                                .where(
                                    MemoryRecallItemModel.receipt_id == receipt_id,
                                    MemoryRecallItemModel.fact_id == fact_id,
                                )
                                .values(reinforced=False, reinforced_at=None)
                            )
                        break
                    fact = _project_fact_for_activation(fact_row)
                    state = MemoryActivationState(
                        fact_id=row.fact_id,
                        activation=row.activation,
                        activation_updated_at=row.activation_updated_at,
                        last_recalled_at=row.last_recalled_at,
                        recall_count=row.recall_count,
                        revision=row.revision,
                    )
                    before = effective_activation(state, fact, query, now=timestamp)
                    after = before + alpha * (1.0 - before)
                    result = await session.execute(
                        update(MemoryActivationStateModel)
                        .where(
                            MemoryActivationStateModel.fact_id == fact_id,
                            MemoryActivationStateModel.revision == row.revision,
                        )
                        .values(
                            activation=after,
                            activation_updated_at=timestamp,
                            last_recalled_at=timestamp,
                            recall_count=row.recall_count + 1,
                            revision=row.revision + 1,
                        )
                    )
                    if int(cast(CursorResult[Any], result).rowcount or 0) == 1:
                        reinforced.append(fact_id)
                        break
                    if receipt_id is not None:
                        await session.execute(
                            update(MemoryRecallItemModel)
                            .where(
                                MemoryRecallItemModel.receipt_id == receipt_id,
                                MemoryRecallItemModel.fact_id == fact_id,
                            )
                            .values(reinforced=False, reinforced_at=None)
                        )
        return tuple(reinforced)


class MemoryIntentRanker:
    """Apply only explicit Planner features inside an already legal target."""

    def __init__(self, metrics: MemoryLifecycleMetrics | None = None) -> None:
        self._metrics = metrics

    def rerank(
        self,
        hits: tuple[MemoryRetrievalHit, ...],
        *,
        query: MemoryQuery,
        states: dict[int, MemoryActivationState],
        now: datetime | None = None,
    ) -> tuple[MemoryRetrievalHit, ...]:
        intent = query.intent
        if intent is None or not query.intent_rerank_enabled or not hits:
            return hits
        timestamp = now or datetime.now(UTC)
        weights = _RERANK_WEIGHTS[intent.purpose]
        scored: list[MemoryRetrievalHit] = []
        for hit in hits:
            fact = hit.fact
            base = 1.0 / math.log2(hit.rank + 1)
            subject = _subject_score(hit, intent.subjects)
            entity = _entity_score(fact, intent.entities)
            temporal = _temporal_score(
                fact,
                intent.temporal.mode,
                intent.temporal.start_at,
                intent.temporal.end_at,
                timestamp,
                query.intent_recent_window_days,
            )
            kind = 0.5 if not intent.preferred_kinds else float(fact.kind in intent.preferred_kinds)
            state = states.get(fact.id)
            if state is None:
                state = MemoryActivationState(
                    fact_id=fact.id,
                    activation=initial_activation(fact),
                    activation_updated_at=fact.created_at,
                )
                logger.warning("memory_activation_state_missing fact_id=%d", fact.id)
                if self._metrics is not None:
                    self._metrics.increment("memory_activation_state_missing_count")
            activation = (
                effective_activation(state, fact, query, now=timestamp)
                if query.activation_ranking_enabled
                else 0.5
            )
            if self._metrics is not None:
                self._metrics.record_activation(activation)
            score = sum(
                weight * feature
                for weight, feature in zip(
                    weights,
                    (base, subject, entity, temporal, kind, activation),
                    strict=True,
                )
            )
            scored.append(
                hit.model_copy(
                    update={
                        "base_rank_score": base,
                        "subject_score": subject,
                        "entity_score": entity,
                        "temporal_score": temporal,
                        "kind_score": kind,
                        "activation_score": activation,
                        "rerank_score": score,
                    }
                )
            )
        ordered = sorted(
            scored,
            key=lambda hit: (
                0 if hit.selection_reason.endswith("_exact") else 1,
                -hit.rerank_score,
                hit.rank,
                hit.fact.id,
            ),
        )
        return tuple(
            hit.model_copy(update={"rank": rank}) for rank, hit in enumerate(ordered, start=1)
        )


def apply_strict_temporal_constraint(
    hits: tuple[MemoryRetrievalHit, ...],
    intent: MemoryQueryIntent | None,
) -> tuple[MemoryRetrievalHit, ...]:
    """Exclude facts whose event time cannot satisfy an explicit hard range."""

    if intent is None or intent.temporal.constraint is not MemoryTemporalConstraint.STRICT:
        return hits
    start_at = intent.temporal.start_at
    end_at = intent.temporal.end_at
    matched: list[MemoryRetrievalHit] = []
    for hit in hits:
        occurred_at = hit.fact.valid_from
        if occurred_at is None:
            continue
        if start_at is not None and occurred_at < start_at:
            continue
        if end_at is not None and occurred_at > end_at:
            continue
        matched.append(hit)
    return tuple(hit.model_copy(update={"rank": rank}) for rank, hit in enumerate(matched, start=1))


def _subject_score(
    hit: MemoryRetrievalHit,
    subjects: tuple[MemorySubjectRole, ...],
) -> float:
    if not subjects:
        return 0.5
    roles = {
        MemorySubjectRole.CURRENT_PERSON: {
            MemoryTargetRole.CURRENT_PERSON,
            MemoryTargetRole.CURRENT_PERSON_GROUP,
        },
        MemorySubjectRole.CURRENT_GROUP: {MemoryTargetRole.CURRENT_GROUP},
        MemorySubjectRole.REFERENCED_PERSON: {
            MemoryTargetRole.REFERENCED_PERSON,
            MemoryTargetRole.REFERENCED_PERSON_GROUP,
        },
        MemorySubjectRole.CURRENT_SELF: {MemoryTargetRole.CURRENT_SELF},
    }
    return float(any(hit.target.role in roles[subject] for subject in subjects))


def _entity_score(fact: MemoryFact, entities: tuple[str, ...]) -> float:
    if not entities:
        return 0.5
    haystack = normalize_query_text(f"{fact.memory_key} {fact.category} {fact.content}")
    matched = sum(1 for entity in entities if normalize_query_text(entity) in haystack)
    return matched / len(entities)


def _temporal_score(
    fact: MemoryFact,
    mode: MemoryTemporalIntentMode,
    start_at: datetime | None,
    end_at: datetime | None,
    now: datetime,
    recent_window_days: int,
) -> float:
    occurred_at = fact.valid_from
    if mode is MemoryTemporalIntentMode.UNSPECIFIED or occurred_at is None:
        return 0.5
    age_days = max(0.0, (now - occurred_at).total_seconds() / 86_400)
    if mode is MemoryTemporalIntentMode.RECENT:
        return max(0.0, 1.0 - age_days / recent_window_days)
    if mode is MemoryTemporalIntentMode.HISTORICAL:
        return max(0.0, min(1.0, (age_days - 90.0) / (365.0 - 90.0)))
    if start_at is not None and occurred_at < start_at:
        return 0.0
    if end_at is not None and occurred_at > end_at:
        return 0.0
    return 1.0


def _project_fact_for_activation(row: MemoryFactModel) -> MemoryFact:
    return MemoryFact(
        id=row.id,
        scope_type=row.scope_type,
        subject_user_id=row.subject_user_id,
        group_id=row.group_id,
        visibility_type=row.visibility_type,
        visibility_user_id=row.visibility_user_id,
        visibility_group_id=row.visibility_group_id,
        kind=row.kind,
        memory_key=row.memory_key,
        category=row.category,
        content=row.content,
        normalized_content=row.normalized_content,
        importance=row.importance,
        confidence=row.confidence,
        source_type=row.source_type,
        authority=row.authority,
        status=row.status,
        conflict_state=row.conflict_state,
        supersedes_id=row.supersedes_id,
        valid_from=row.valid_from,
        valid_until=row.valid_until,
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_confirmed_at=row.last_confirmed_at,
        invalidated_reason=row.invalidated_reason,
        last_injected_at=row.last_injected_at,
        validation_version=row.validation_version,
        last_audited_at=row.last_audited_at,
        review_state=row.review_state,
    )
