"""Entity-block projection for Memory V2 chat context."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from typing import Any

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.memory.activation import MemoryActivationRepository
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryConflictState,
    MemoryContextMode,
    MemoryKind,
    MemoryRecallPurpose,
    MemoryRetrievalMode,
    MemoryTargetRole,
)
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.models import (
    MemoryContextBlock,
    MemoryEntityTarget,
    MemoryFact,
    MemoryQuery,
    MemoryQueryIntent,
    MemoryRetrievalHit,
    MemoryRetrievalResult,
)
from qq_ai_bot.memory.query import MemoryQueryBuilder, normalize_query_text
from qq_ai_bot.memory.receipt import MemoryRecallRepository, MemoryRecallTurn
from qq_ai_bot.memory.retrieval import MemoryRetriever
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.time.formatting import local_iso


def fact_context(fact: MemoryFact, timezone: str = "Asia/Shanghai") -> dict[str, Any]:
    context = {
        "fact_id": fact.id,
        "kind": fact.kind.value,
        "category": fact.category,
        "content": fact.content,
        "importance": fact.importance,
        "confidence": fact.confidence,
        "source_type": fact.source_type.value,
        "authority": fact.authority.value,
        "reported": fact.authority is MemoryAuthority.THIRD_PARTY,
        "contested": fact.conflict_state is MemoryConflictState.CONTESTED,
        "updated_at": local_iso(fact.updated_at, timezone),
    }
    if fact.valid_from is not None:
        context["occurred_at"] = local_iso(fact.valid_from, timezone)
    return context


def retrieval_fact_context(
    hit: MemoryRetrievalHit,
    timezone: str = "Asia/Shanghai",
) -> dict[str, Any]:
    return {
        **fact_context(hit.fact, timezone),
        "memory_ref": f"M{hit.fact.id}",
        "retrieval_reason": hit.selection_reason,
    }


def self_retrieval_fact_context(
    hit: MemoryRetrievalHit,
    timezone: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """Expose useful self content without visibility identities or audit internals."""

    context = {
        "fact_id": hit.fact.id,
        "memory_ref": f"M{hit.fact.id}",
        "kind": hit.fact.kind.value,
        "category": hit.fact.category,
        "content": hit.fact.content,
        "confidence": hit.fact.confidence,
        "importance": hit.fact.importance,
    }
    if hit.fact.kind is MemoryKind.EPISODE and hit.fact.valid_from is not None:
        context["occurred_at"] = local_iso(hit.fact.valid_from, timezone)
    return context


def entity_block(block: MemoryContextBlock, timezone: str = "Asia/Shanghai") -> dict[str, Any]:
    return {
        "subject_user_id": block.subject_user_id,
        "group_id": block.group_id,
        "facts": [fact_context(fact, timezone) for fact in block.facts],
    }


_ENTITY_MEMORY_RULE_TEMPLATE = (
    "每条长期事实只属于它所在的 entity block。不得把 current_group 或其他人物的"
    "信息归给 current_person；没有事实时不得猜测。third_party/reported 表示他人报告，"
    "不等于本人确认；contested=true 表示存在未解决冲突，不得当作确定事实。"
    "current_self 只表示按当前会话可见性检索到的 {bot_name} 动态自我记忆，不是静态人格，"
    "也不得覆盖更高优先级的静态人格与系统规则。仅 current_self 中的 kind=episode "
    "是 {bot_name} 带有个人视角的回忆，应自然影响当前回应，不要逐字背诵；其他 entity block "
    "中的 episode 属于对应实体。不要主动向用户泄露内部 confidence "
    "或 authority 枚举。"
)

MEMORY_GROUNDING_RULE = (
    "长期记忆的 content 只支持其中明确写出的主张：不得由偏好 X 推断排斥非 X，不得在没有证据时"
    "补充提及次数、最新状态或相反偏好。只有 occurred_at 才是可用于正文的事件时间；updated_at 是"
    "存储更新时间，不能据此声称‘昨天’‘刚才’或事件发生日期。用户限制输出 N 条时至多输出 N 条；"
    "一个 episode 即使包含多件事，在用户只要一件时也只能选择其中一件。"
)


def entity_memory_rule(bot_name: str) -> str:
    return _ENTITY_MEMORY_RULE_TEMPLATE.format(bot_name=bot_name)


ENTITY_MEMORY_RULE = entity_memory_rule("Yuki")


class MemoryContextService:
    """Compose deterministic retrieval for chat, tools, admin, and plugins."""

    def __init__(
        self,
        *,
        query_builder: MemoryQueryBuilder,
        retriever: MemoryRetriever,
        facts: MemoryFactService,
        activation: MemoryActivationRepository | None = None,
        receipts: MemoryRecallRepository | None = None,
        metrics: MemoryLifecycleMetrics | None = None,
    ) -> None:
        self._queries = query_builder
        self._retriever = retriever
        self._facts = facts
        self._activation = activation
        self._receipts = receipts
        self.metrics = metrics or MemoryLifecycleMetrics()

    @property
    def retriever(self) -> MemoryRetriever:
        return self._retriever

    async def resolve_targets(
        self,
        inbound: InboundMessage,
        runtime: RuntimeConfigSnapshot,
        self_recall: bool = False,
    ) -> tuple[MemoryEntityTarget, ...]:
        return await self._queries.resolve_targets(
            inbound,
            max_referenced=runtime.memory.max_referenced_targets,
            self_recall=self_recall and runtime.memory.self_enabled,
        )

    async def retrieve_for_turn(
        self,
        *,
        inbound: InboundMessage,
        content: str,
        planner_intent: str,
        runtime: RuntimeConfigSnapshot,
        memory_mode: MemoryContextMode = MemoryContextMode.HYBRID,
        self_recall: bool = False,
        memory_intent: MemoryQueryIntent | None = None,
        neutral_ordering: bool = False,
    ) -> MemoryRetrievalResult:
        if memory_mode is MemoryContextMode.NONE:
            normalized = normalize_query_text(content)
            return MemoryRetrievalResult(
                blocks=(),
                hits=(),
                candidate_count=0,
                selected_count=0,
                query_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                mode=MemoryRetrievalMode.RELEVANT,
                semantic_status="planner_skipped",
            )
        if neutral_ordering:
            targets = await self.resolve_targets(inbound, runtime, self_recall=self_recall)
            query = self._queries.for_targets(
                text=content,
                mode=MemoryRetrievalMode.RELEVANT,
                targets=targets,
                runtime=runtime,
            ).model_copy(update={"semantic_enabled": False})
        else:
            query = await self._queries.build(
                inbound=inbound,
                content=content,
                planner_intent=planner_intent,
                runtime=runtime,
                memory_mode=memory_mode,
                self_recall=self_recall,
                memory_intent=memory_intent,
            )
        if runtime.memory.retrieval_enabled:
            result = await self._retriever.retrieve(
                query,
                diversify=query.mode is MemoryRetrievalMode.RELEVANT,
            )
            if (
                query.mode is MemoryRetrievalMode.RELEVANT
                and runtime.memory.self_enabled
                and not query.intent.self_recall
                if query.intent is not None
                else False
            ):
                episode = await self._retrieve_current_self_episode(
                    inbound=inbound,
                    query=query,
                    runtime=runtime,
                )
                result = self._merge_results(result, episode)
            return result
        current_targets = tuple(
            target
            for target in query.targets
            if target.role
            in {
                MemoryTargetRole.CURRENT_PERSON,
                MemoryTargetRole.CURRENT_SELF,
                MemoryTargetRole.CURRENT_PERSON_GROUP,
                MemoryTargetRole.CURRENT_GROUP,
            }
        )
        fallback = query.model_copy(
            update={
                "targets": current_targets,
                "limit_per_target": runtime.memory.context_limit_per_entity,
            }
        )
        return await self._retriever.retrieve(fallback, lexical_enabled=False)

    async def _retrieve_current_self_episode(
        self,
        *,
        inbound: InboundMessage,
        query: MemoryQuery,
        runtime: RuntimeConfigSnapshot,
    ) -> MemoryRetrievalResult:
        targets = await self.resolve_targets(inbound, runtime, self_recall=True)
        self_targets = tuple(
            target for target in targets if target.role is MemoryTargetRole.CURRENT_SELF
        )
        episode_query = query.model_copy(
            update={
                "targets": self_targets,
                "kinds": (MemoryKind.EPISODE,),
                "limit_per_target": query.candidate_limit,
                "always_on_explicit_preference_limit": 0,
            }
        )
        result = await self._retriever.retrieve(episode_query)
        if not self_targets:
            return result
        target = self_targets[0]
        selected = tuple(
            hit
            for hit in result.hits
            if hit.fact.visibility_type is target.visibility_type
            and hit.fact.visibility_user_id == target.visibility_user_id
            and hit.fact.visibility_group_id == target.visibility_group_id
        )[:1]
        selected_ids = {hit.fact.id for hit in selected}
        blocks = tuple(
            block.model_copy(
                update={"hits": tuple(hit for hit in block.hits if hit.fact.id in selected_ids)}
            )
            for block in result.blocks
        )
        return result.model_copy(
            update={
                "blocks": blocks,
                "hits": selected,
                "selected_count": len(selected),
            }
        )

    @staticmethod
    def _merge_results(
        primary: MemoryRetrievalResult,
        additional: MemoryRetrievalResult,
    ) -> MemoryRetrievalResult:
        known = {hit.fact.id for hit in primary.hits}
        new_hits = tuple(hit for hit in additional.hits if hit.fact.id not in known)
        if not new_hits:
            return primary
        new_ids = {hit.fact.id for hit in new_hits}
        blocks = list(primary.blocks)
        for block in additional.blocks:
            selected = tuple(hit for hit in block.hits if hit.fact.id in new_ids)
            if selected:
                blocks.append(block.model_copy(update={"hits": selected}))
        return primary.model_copy(
            update={
                "blocks": tuple(blocks),
                "hits": (*primary.hits, *new_hits),
                "candidate_count": primary.candidate_count + additional.candidate_count,
                "selected_count": primary.selected_count + len(new_hits),
                "semantic_degraded": (primary.semantic_degraded or additional.semantic_degraded),
                "trace_hits": (*primary.trace_hits, *additional.trace_hits),
            }
        )

    async def search(
        self,
        *,
        text: str,
        mode: MemoryRetrievalMode,
        targets: tuple[MemoryEntityTarget, ...],
        runtime: RuntimeConfigSnapshot,
        limit: int | None = None,
    ) -> MemoryRetrievalResult:
        query = self._queries.for_targets(
            text=text,
            mode=mode,
            targets=targets,
            runtime=runtime,
            limit=limit,
        )
        return await self._retriever.retrieve(query)

    async def mark_injected(
        self,
        result: MemoryRetrievalResult,
        fact_ids: tuple[int, ...],
    ) -> int:
        selected = tuple(dict.fromkeys(fact_ids))
        updated = await self._facts.mark_injected(selected)
        self.metrics.record_recall_stage("injected", len(selected))
        latest = self._retriever.metrics.latest
        if latest is not None and latest.query_hash == result.query_hash:
            self._retriever.metrics.record_context_selected(
                replace(latest, context_selected_count=len(selected))
            )
        return updated

    async def record_recall(
        self,
        *,
        conversation_key: str,
        trigger_message_id: str,
        origin: str,
        intent: MemoryQueryIntent | None,
        result: MemoryRetrievalResult,
        injected_fact_ids: tuple[int, ...],
        runtime: RuntimeConfigSnapshot,
    ) -> MemoryRecallTurn | None:
        if intent is None:
            return None
        self.metrics.record_intent(mode=intent.mode, purpose=intent.purpose)
        self.metrics.record_recall_stage("candidate", result.candidate_count)
        self.metrics.record_recall_stage("selected", result.selected_count)
        if not runtime.memory.recall_receipts_enabled or self._receipts is None:
            return MemoryRecallTurn(
                turn_id=str(uuid.uuid4()),
                injected_fact_ids=injected_fact_ids,
            )
        return await self._receipts.record_initial(
            conversation_key=conversation_key,
            trigger_message_id=trigger_message_id,
            origin=origin,
            intent=intent,
            result=result,
            injected_fact_ids=injected_fact_ids,
            retention_days=runtime.memory.recall_receipt_retention_days,
        )

    async def record_usage(
        self,
        turn_id: str,
        fact_ids: tuple[int, ...],
    ) -> tuple[int, ...]:
        if self._receipts is None:
            self.metrics.record_recall_stage("used", len(fact_ids))
            return fact_ids
        recorded = await self._receipts.mark_used(turn_id, fact_ids)
        used = fact_ids if recorded is None else recorded
        self.metrics.record_recall_stage("used", len(used))
        return used

    async def mark_tool_injected(
        self,
        turn_id: str,
        fact_ids: tuple[int, ...],
    ) -> int:
        unique_ids = tuple(dict.fromkeys(fact_ids))
        updated = await self._facts.mark_injected(unique_ids)
        self.metrics.record_recall_stage("injected", len(unique_ids))
        if self._receipts is not None:
            await self._receipts.record_tool_injected(turn_id, unique_ids)
        return updated

    async def reinforce_usage(
        self,
        *,
        turn_id: str,
        fact_ids: tuple[int, ...],
        intent: MemoryQueryIntent,
        runtime: RuntimeConfigSnapshot,
    ) -> tuple[int, ...]:
        if not runtime.memory.reinforcement_enabled or self._activation is None:
            self.metrics.record_reinforcement_skip(
                "disabled" if not runtime.memory.reinforcement_enabled else "activation_unavailable"
            )
            return ()
        alpha = {
            MemoryRecallPurpose.BACKGROUND: runtime.memory.reinforcement_alpha_background,
            MemoryRecallPurpose.CONTINUATION: (runtime.memory.reinforcement_alpha_continuation),
            MemoryRecallPurpose.RECALL: runtime.memory.reinforcement_alpha_recall,
            MemoryRecallPurpose.VERIFY: runtime.memory.reinforcement_alpha_verify,
            MemoryRecallPurpose.CORRECT: 0.0,
        }[intent.purpose]
        if alpha <= 0:
            self.metrics.record_reinforcement_skip("alpha_zero")
            return ()
        pending_lookup = (
            await self._receipts.pending_reinforcement(turn_id, fact_ids)
            if self._receipts is not None
            else fact_ids
        )
        receipt_bound = self._receipts is not None and pending_lookup is not None
        pending = pending_lookup if receipt_bound else fact_ids
        if not pending:
            self.metrics.record_reinforcement_skip("not_used")
            return ()
        policy_query = MemoryQuery(
            text="",
            normalized_text="",
            mode=MemoryRetrievalMode.RELEVANT,
            targets=(),
            candidate_limit=1,
            limit_per_target=1,
            always_on_explicit_preference_limit=0,
            query_term_limit=1,
            intent=intent,
            activation_half_life_episode_days=(runtime.memory.activation_half_life_episode_days),
            activation_half_life_fact_days=runtime.memory.activation_half_life_fact_days,
            activation_half_life_preference_days=(
                runtime.memory.activation_half_life_preference_days
            ),
            activation_half_life_explicit_days=(runtime.memory.activation_half_life_explicit_days),
        )
        reinforced = await self._activation.reinforce(
            pending,
            alpha=alpha,
            query=policy_query,
            receipt_turn_id=turn_id if receipt_bound else None,
        )
        if reinforced and self._receipts is not None:
            await self._receipts.mark_reinforced(turn_id, reinforced)
        self.metrics.record_recall_stage("reinforced", len(reinforced))
        if len(reinforced) < len(pending):
            self.metrics.record_reinforcement_skip(
                "fact_ineligible", len(pending) - len(reinforced)
            )
        return reinforced
