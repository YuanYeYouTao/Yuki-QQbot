"""Bounded Memory V2 fact inspection and local consistency diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.config import Settings
from qq_ai_bot.memory.activation import (
    MemoryActivationRepository,
    effective_activation,
    initial_activation,
)
from qq_ai_bot.memory.enums import MemoryRetrievalMode
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.models import (
    MemoryActivationState,
    MemoryConsistencyHealth,
    MemoryEvidence,
    MemoryFact,
    MemoryFactRelation,
    MemoryFactStateEvent,
    MemoryQuery,
)
from qq_ai_bot.memory.receipt import MemoryRecallRepository
from qq_ai_bot.memory.repository import MemoryFactRepository


class MemoryAuditService:
    def __init__(
        self,
        repository: MemoryFactRepository,
        *,
        metrics: MemoryLifecycleMetrics | None = None,
        settings: Settings | None = None,
        runtime_config: RuntimeConfigService | None = None,
        activation: MemoryActivationRepository | None = None,
        receipts: MemoryRecallRepository | None = None,
    ) -> None:
        self._repository = repository
        self._metrics = metrics or MemoryLifecycleMetrics()
        self._settings = settings
        self._runtime_config = runtime_config
        self._activation = activation
        self._receipts = receipts

    async def get_fact(self, fact_id: int) -> MemoryFact | None:
        return await self._repository.get_fact(fact_id)

    async def get_evidence(self, fact_id: int, *, limit: int = 100) -> tuple[MemoryEvidence, ...]:
        return await self._repository.list_evidence(fact_id, limit=limit)

    async def get_relations(self, fact_id: int) -> tuple[MemoryFactRelation, ...]:
        return await self._repository.list_relations(fact_id)

    async def get_state_history(self, fact_id: int) -> tuple[MemoryFactStateEvent, ...]:
        return await self._repository.list_state_events(fact_id)

    async def get_supersession_chain(
        self, fact_id: int, *, limit: int = 20
    ) -> tuple[MemoryFact, ...]:
        result: list[MemoryFact] = []
        seen: set[int] = set()
        current = await self._repository.get_fact(fact_id)
        while current is not None and current.id not in seen and len(result) < max(1, limit):
            result.append(current)
            seen.add(current.id)
            current = (
                await self._repository.get_fact(current.supersedes_id)
                if current.supersedes_id is not None
                else None
            )
        return tuple(result)

    async def list_conflicts(
        self,
        *,
        subject_user_id: str | None = None,
        group_id: str | None = None,
        limit: int = 100,
    ) -> tuple[MemoryFact, ...]:
        return await self._repository.list_conflicts(
            subject_user_id=subject_user_id,
            group_id=group_id,
            limit=limit,
        )

    async def explain(self, fact_id: int) -> dict[str, Any] | None:
        fact = await self.get_fact(fact_id)
        if fact is None:
            return None
        evidence = await self.get_evidence(fact_id)
        relations = await self.get_relations(fact_id)
        state_events = await self.get_state_history(fact_id)
        chain = await self.get_supersession_chain(fact_id)
        activation = None
        if self._activation is not None:
            activation = (await self._activation.load((fact_id,))).get(fact_id)
            if activation is None:
                activation = MemoryActivationState(
                    fact_id=fact.id,
                    activation=initial_activation(fact),
                    activation_updated_at=fact.created_at,
                )
                self._metrics.increment("memory_activation_state_missing_count")
        policy = await self._activation_policy_query()
        effective = (
            effective_activation(activation, fact, policy, now=datetime.now(UTC))
            if activation is not None
            else None
        )
        recent_receipts = (
            await self._receipts.recent_for_fact(fact_id) if self._receipts is not None else ()
        )
        return {
            "fact_id": fact.id,
            "status": fact.status.value,
            "scope_type": fact.scope_type.value,
            "authority": fact.authority.value,
            "confidence": fact.confidence,
            "conflict_state": fact.conflict_state.value,
            "evidence_count": len(evidence),
            "evidence": [
                {
                    "relation": row.relation.value,
                    "authority": row.authority.value,
                    "confidence": row.confidence,
                    "created_at": row.created_at.isoformat(),
                }
                for row in evidence[-20:]
            ],
            "last_confirmed_at": fact.last_confirmed_at.isoformat(),
            "last_injected_at": (
                fact.last_injected_at.isoformat() if fact.last_injected_at is not None else None
            ),
            "activation": effective,
            "activation_updated_at": (
                activation.activation_updated_at.isoformat() if activation is not None else None
            ),
            "last_recalled_at": (
                activation.last_recalled_at.isoformat()
                if activation is not None and activation.last_recalled_at is not None
                else None
            ),
            "recall_count": activation.recall_count if activation is not None else 0,
            "recent_recall_receipts": list(recent_receipts),
            "supersession_chain": [row.id for row in chain],
            "relations": [
                {
                    "source_fact_id": row.source_fact_id,
                    "target_fact_id": row.target_fact_id,
                    "relation_type": row.relation_type.value,
                    "confidence": row.confidence,
                }
                for row in relations
            ],
            "state_events": [
                {
                    "action": row.action.value,
                    "reason_code": row.reason_code,
                    "created_at": row.created_at.isoformat(),
                }
                for row in state_events[-20:]
            ],
        }

    async def _activation_policy_query(self) -> MemoryQuery:
        if self._runtime_config is not None:
            memory = (await self._runtime_config.snapshot()).memory
            values = (
                memory.activation_half_life_episode_days,
                memory.activation_half_life_fact_days,
                memory.activation_half_life_preference_days,
                memory.activation_half_life_explicit_days,
            )
        elif self._settings is not None:
            values = (
                self._settings.memory_activation_half_life_episode_days,
                self._settings.memory_activation_half_life_fact_days,
                self._settings.memory_activation_half_life_preference_days,
                self._settings.memory_activation_half_life_explicit_days,
            )
        else:
            values = (14.0, 60.0, 120.0, 365.0)
        return MemoryQuery(
            text="",
            normalized_text="",
            mode=MemoryRetrievalMode.RELEVANT,
            targets=(),
            candidate_limit=1,
            limit_per_target=1,
            always_on_explicit_preference_limit=0,
            query_term_limit=1,
            activation_half_life_episode_days=values[0],
            activation_half_life_fact_days=values[1],
            activation_half_life_preference_days=values[2],
            activation_half_life_explicit_days=values[3],
        )

    async def health(self) -> MemoryConsistencyHealth:
        queries = {
            "active_slot_conflicts": """
                SELECT COALESCE(SUM(c - 1), 0) FROM (
                    SELECT COUNT(*) AS c FROM memory_facts WHERE status = 'active'
                    GROUP BY scope_type, COALESCE(subject_user_id, ''),
                        COALESCE(group_id, ''), COALESCE(visibility_type, ''),
                        COALESCE(visibility_user_id, ''), COALESCE(visibility_group_id, ''),
                        CASE WHEN scope_type='self' THEN '' ELSE kind END,
                        memory_key HAVING COUNT(*) > 1
                )
            """,
            "contested_fact_count": "SELECT COUNT(*) FROM memory_facts WHERE status='contested'",
            "active_contested_count": """
                SELECT COUNT(*) FROM memory_facts
                WHERE status='active' AND conflict_state='contested'
            """,
            "orphan_relation_count": """
                SELECT COUNT(*) FROM memory_fact_relations r
                LEFT JOIN memory_facts s ON s.id=r.source_fact_id
                LEFT JOIN memory_facts t ON t.id=r.target_fact_id
                WHERE s.id IS NULL OR t.id IS NULL
            """,
            "cross_target_relation_count": """
                SELECT COUNT(*) FROM memory_fact_relations r
                JOIN memory_facts s ON s.id=r.source_fact_id
                JOIN memory_facts t ON t.id=r.target_fact_id
                WHERE s.scope_type != t.scope_type
                    OR COALESCE(s.subject_user_id, '') != COALESCE(t.subject_user_id, '')
                    OR COALESCE(s.group_id, '') != COALESCE(t.group_id, '')
                    OR COALESCE(s.visibility_type, '') != COALESCE(t.visibility_type, '')
                    OR COALESCE(s.visibility_user_id, '') != COALESCE(t.visibility_user_id, '')
                    OR COALESCE(s.visibility_group_id, '') != COALESCE(t.visibility_group_id, '')
            """,
            "orphan_state_event_count": """
                SELECT COUNT(*) FROM memory_fact_state_events e
                LEFT JOIN memory_facts f ON f.id=e.fact_id WHERE f.id IS NULL
            """,
            "invalidated_without_reason_count": """
                SELECT COUNT(*) FROM memory_facts
                WHERE status='invalidated' AND invalidated_reason IS NULL
            """,
            "superseded_without_chain_count": """
                SELECT COUNT(*) FROM memory_facts
                WHERE status='superseded' AND id NOT IN (
                    SELECT supersedes_id FROM memory_facts WHERE supersedes_id IS NOT NULL
                ) AND id NOT IN (
                    SELECT source_fact_id FROM memory_fact_relations
                    WHERE relation_type='equivalent'
                )
            """,
            "evidence_authority_mismatch_count": """
                SELECT COUNT(*) FROM memory_evidence e JOIN memory_facts f ON f.id=e.fact_id
                WHERE CASE e.authority
                    WHEN 'explicit' THEN 4 WHEN 'agent_reflection' THEN 3
                    WHEN 'self_report' THEN 2
                    WHEN 'group_report' THEN 1 ELSE 0 END
                  > CASE f.authority
                    WHEN 'explicit' THEN 4 WHEN 'agent_reflection' THEN 3
                    WHEN 'self_report' THEN 2
                    WHEN 'group_report' THEN 1 ELSE 0 END
            """,
            "expired_active_count": """
                SELECT COUNT(*) FROM memory_facts
                WHERE status IN ('active','contested')
                    AND valid_until IS NOT NULL AND valid_until <= :now
            """,
        }
        now = datetime.now(UTC)
        values: dict[str, int] = {}
        async with self._repository.database.sessions() as session:
            for key, sql in queries.items():
                values[key] = int((await session.scalar(text(sql), {"now": now})) or 0)
            lifecycle = await self._lifecycle_values()
            values["stale_backlog_count"] = (
                int(
                    (
                        await session.scalar(
                            text(
                                """
                                SELECT COUNT(*) FROM memory_facts
                                WHERE status IN ('active','contested')
                                  AND source_type='automatic' AND authority != 'explicit'
                                  AND scope_type!='self'
                                  AND importance <= :max_importance
                                  AND confidence <= :max_confidence
                                  AND (
                                    (authority='third_party'
                                     AND last_confirmed_at <= :third_party_cutoff)
                                    OR (status='contested'
                                        AND last_confirmed_at <= :contested_cutoff)
                                    OR (authority != 'third_party' AND status != 'contested'
                                        AND last_confirmed_at <= :automatic_cutoff)
                                  )
                                """
                            ),
                            lifecycle,
                        )
                    )
                    or 0
                )
                if lifecycle is not None
                else 0
            )
        return MemoryConsistencyHealth(
            **values,
            classifier_recent_errors=self._metrics.classifier_recent_errors,
            maintenance_last_success_at=self._metrics.maintenance_last_success_at,
        )

    async def _lifecycle_values(self) -> dict[str, object] | None:
        if self._runtime_config is not None:
            memory = (await self._runtime_config.snapshot()).memory
            automatic_days = memory.automatic_stale_days
            third_party_days = memory.third_party_stale_days
            contested_days = memory.contested_stale_days
            max_importance = memory.stale_max_importance
            max_confidence = memory.stale_max_confidence
        elif self._settings is not None:
            automatic_days = self._settings.memory_automatic_stale_days
            third_party_days = self._settings.memory_third_party_stale_days
            contested_days = self._settings.memory_contested_stale_days
            max_importance = self._settings.memory_stale_max_importance
            max_confidence = self._settings.memory_stale_max_confidence
        else:
            return None
        now = datetime.now(UTC)
        return {
            "automatic_cutoff": now - timedelta(days=automatic_days),
            "third_party_cutoff": now - timedelta(days=third_party_days),
            "contested_cutoff": now - timedelta(days=contested_days),
            "max_importance": max_importance,
            "max_confidence": max_confidence,
        }
