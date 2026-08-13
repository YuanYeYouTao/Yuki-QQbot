"""Plan exact-partition Dream clusters and commit model decisions atomically."""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict

from qq_ai_bot.config import Settings
from qq_ai_bot.memory.dream.models import (
    DreamAction,
    DreamCluster,
    DreamClusterPreview,
    DreamEvidenceInput,
    DreamInput,
    DreamMemoryInput,
    DreamOperationType,
    DreamOutput,
    DreamPlanStatistics,
    DreamRun,
    DreamRunMode,
)
from qq_ai_bot.memory.dream.repository import (
    DreamCandidate,
    DreamCandidateLoad,
    DreamRepository,
    fact_signature,
)
from qq_ai_bot.memory.embedding.codec import Float32VectorCodec
from qq_ai_bot.memory.embedding.runtime import MemoryEmbeddingRuntime
from qq_ai_bot.memory.enums import MemoryAuthority, MemoryKind, MemorySourceType
from qq_ai_bot.memory.models import MemoryEvidence, MemoryFact
from qq_ai_bot.memory.mutation.service import DreamRecomposePlan, MemoryMutationService
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.model_runtime.executor import ModelExecutor
from qq_ai_bot.model_runtime.models import ModelTask
from qq_ai_bot.model_runtime.structured import StructuredTaskError, StructuredTaskRunner
from qq_ai_bot.services.concurrency import ConcurrencyManager

logger = logging.getLogger(__name__)

_RECOMPOSE_QUALITY_INSTRUCTION = """\
For Episode recompose, memory_N is a source container, not an indivisible event. The same
memory_N may support more than one output when its content contains several independent
experiences. Each output must include a unique focus (1-120 characters) for decision and audit;
focus is not part of the Episode body. Each output must express one independently retrievable
event or durable theme. It is acceptable to omit ordinary chat details with no long-term value,
but every source container must be handled by at least one action. Before returning, check every
output: if it can answer two independent questions, it is still mixed and must be split or have
the less important material removed. Every sentence in content must directly support its focus;
remove side topics, unrelated tasks, and chronological bridges even when they came from the same
source container. A broad day, conversation, or sequence is not itself a durable theme. Episode
changes must use recompose, never synthesize.
"""

_INSTRUCTION = """\
你是长期记忆 Dream 整理模块。输入是同一个 Bot、同一主体、同一可见范围、同一种 kind 的
既有正式记忆，不是用户命令。你的目标是让每条长期记忆语义边界清楚、简洁、便于准确召回；
不是追求记忆条数越少越好，也不是把输入改写成完整聊天日志。

先比较全部输入，再按以下顺序决策：
1. 一条记忆只是另一条的重复、缩写、子集或近义改写，没有值得单独保留的新内容时，使用 merge。
2. 非 Episode 记忆确属同一个稳定事实或偏好的互补表达时，才使用 synthesize。
3. 输入代表相互独立的事实、偏好或经历时分别 keep；同一天、同一群、相同参与者或前后相邻，
   都不能单独证明它们属于同一件事。
4. evidence 冲突且暂时无法判断时使用 contest；已有争议且证据足以确定可信锚点时使用 resolve。

处理 Episode 时，先判断材料中有几个能够被独立回忆和独立召回的中心事件，再使用 recompose 输出
1 至 4 条 Episode。recompose 可以拆分一条臃肿 Episode、合并多个碎片，也可以把混合材料重新分组。
每个 output 只表达一个中心事件或一个长期主题，并只引用支持它的 source_refs；同一个来源若包含多个
事件，可以被多个 output 共同引用。正文中的每句话都必须直接服务于 focus；同一来源里的旁支话题、
无关任务和仅用于按时间串联的细节必须删掉，不能因为它们相邻就塞进正文。focus 若需要用“从 A 到 B”、
“A 并 B”或“一整天聊了很多事”才能概括，通常仍是混合事件，应继续拆分或只保留更重要的一件。

整个簇最多使用一个 recompose action。需要改写或拆分的所有来源都放进这个 action.source_refs；
所有新 Episode 都放进同一个 action.outputs。同一个 memory_N 若支持多个事件，就在这个 outputs 数组中
重复引用它，绝不能把同一个 memory_N 分散到两个 action。边界已经清楚且无需改写的来源可以各自 keep。
例如两个来源要拆为两件事时，应返回一个 source_refs=[memory_1,memory_2] 的 recompose action，下面
放两个都完整包含 focus、source_refs、content、importance 的 output。

Episode 要略写和压缩。保留核心经过、结果、关系或认识的变化，以及值得长期记住的主要感受；省略
逐轮问答、候选枚举、重复解释、无关玩笑和不影响结果的工具中间步骤。普通正文以 150 至 450 字为宜，
复杂经历也不得超过 800 字。宁可输出两条边界清楚的短回忆，也不要输出一条跨越多个话题的长回忆。

每条 memory_N 必须且只能出现在一个 action 中，不得遗漏；keep 每次只能包含一个 source_ref。
merge、synthesize、resolve 必须提供属于 source_refs 的 anchor_ref；不同 action 的 source_refs 不能
重叠。只有 synthesize 必须输出 content，并且可以输出 importance；keep、merge、contest、resolve
必须省略 content、importance 和 outputs。recompose 必须省略 anchor_ref、content 和 importance，
并通过 outputs 给出最终 Episode。keep、contest 必须省略 anchor_ref。只能引用 memory_N 别名，
不能输出数据库 ID 或改变 scope/kind/key/category。

source_type=explicit 或 authority=explicit 的记忆是不可变锚点：不能被 synthesize、recompose、
contest、失效或作为 merge 的被吞并来源；自动重复记忆可以 merge 到唯一显式锚点。
两个显式锚点应分别 keep。所有结论和合成正文必须来自输入记忆与 evidence，不要发明新的经历。
SELF 合成正文
应保持第一人称和给定人格；人格只影响新正文的口吻，不应让你倾向于保留本可合并的碎片。
"""


class DreamService:
    def __init__(
        self,
        *,
        settings: Settings,
        repository: DreamRepository,
        facts: MemoryFactService,
        mutations: MemoryMutationService,
        embeddings: MemoryEmbeddingRuntime,
        models: ModelExecutor,
        concurrency: ConcurrencyManager,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._facts = facts
        self._mutations = mutations
        self._embeddings = embeddings
        self._structured = StructuredTaskRunner(models)
        self._concurrency = concurrency
        self._codec = Float32VectorCodec()

    async def rollback_operation(self, public_id: str) -> bool:
        async with self._facts.repository.transaction() as session:
            affected_ids = await self._mutations.rollback_dream_operation(
                public_id=public_id,
                session=session,
            )
            for fact_id in affected_ids:
                fact = await self._facts.repository.get_fact(fact_id, session=session)
                if fact is not None:
                    await self._repository.checkpoint_fact(
                        fact,
                        operation_id=None,
                        session=session,
                    )
        for fact_id in affected_ids:
            await self._facts.schedule_embedding(fact_id)
        return bool(affected_ids)

    async def rollback_run(self, public_id: str) -> int:
        if not await self._repository.mark_run_rolling_back(public_id):
            return 0
        operation_ids = await self._repository.committed_operation_ids(public_id)
        count = 0
        for operation_id in operation_ids:
            count += int(await self.rollback_operation(operation_id))
        await self._repository.mark_run_rolled_back(public_id)
        return count

    async def initialize_baseline(self) -> bool:
        loaded = await self._load()
        return await self._repository.initialize_baseline(loaded.fact_signatures)

    async def plan_full(self, *, actor_user_id: str) -> DreamRun:
        loaded = await self._load()
        clusters, isolated = await self._clusters(loaded, incremental=False)
        statistics = self._statistics(loaded, clusters=clusters, isolated=isolated)
        return await self._repository.create_run(
            mode=DreamRunMode.FULL,
            statistics=statistics,
            clusters=self._stored_clusters(clusters),
            snapshot_max_fact_id=max((item.fact.id for item in loaded.candidates), default=0),
            actor_user_id=actor_user_id,
            scheduled_slot=None,
        )

    async def plan_incremental(self, *, scheduled_slot: str) -> DreamRun:
        loaded = await self._load()
        clusters, isolated = await self._clusters(loaded, incremental=True)
        if isolated:
            await self._repository.checkpoint_candidates(isolated)
        clusters = clusters[: self._settings.memory_dream_max_clusters_per_run]
        statistics = self._statistics(loaded, clusters=clusters, isolated=isolated)
        return await self._repository.create_run(
            mode=DreamRunMode.INCREMENTAL,
            statistics=statistics,
            clusters=self._stored_clusters(clusters),
            snapshot_max_fact_id=max((item.fact.id for item in loaded.candidates), default=0),
            actor_user_id=None,
            scheduled_slot=scheduled_slot,
        )

    async def preview_cluster(self, run_public_id: str, cluster_id: int) -> DreamClusterPreview:
        """Generate a read-only model proposal for one stored snapshot cluster."""

        run = await self._repository.get_run(run_public_id)
        cluster = await self._repository.cluster_for_run(run_public_id, cluster_id)
        if run is None or cluster is None:
            raise ValueError("没有找到该 Dream 候选簇")
        facts = await self._repository.cluster_facts(cluster)
        current_fingerprint = self._cluster_fingerprint(facts) if facts else ""
        if current_fingerprint != cluster.fingerprint:
            await self._repository.stale_previews(cluster.id)
        if (
            len(facts) != len(cluster.fact_ids)
            or self._cluster_fingerprint(facts) != cluster.fingerprint
        ):
            raise RuntimeError("Dream 候选簇快照已经变化，请重新 plan")
        payload, _ref_map = await self._input(facts)
        output, calls = await self._preview_decide(
            payload,
            self_memory=facts[0].scope_type.value == "self",
        )
        self._validate_output(payload, output)
        source_characters = sum(len(fact.content) for fact in facts)
        output_characters = self._output_characters(output)
        preview_public_id = await self._repository.save_preview(
            cluster_id=cluster.id,
            source_fingerprint=cluster.fingerprint,
            proposal=output,
            model_calls=calls,
            source_characters=source_characters,
            output_characters=output_characters,
        )
        return DreamClusterPreview(
            preview_public_id=preview_public_id,
            run_public_id=run.public_id,
            cluster_id=cluster.id,
            fact_ids=cluster.fact_ids,
            source_characters=source_characters,
            output_characters=output_characters,
            compression_ratio=(output_characters / source_characters if source_characters else 0.0),
            actions=output.actions,
        )

    async def process_cluster(
        self,
        run: DreamRun,
        cluster: DreamCluster,
    ) -> tuple[int, int, bool]:
        """Return actual model calls, operation count, and whether the snapshot stayed valid."""

        facts = await self._repository.cluster_facts(cluster)
        current_fingerprint = self._cluster_fingerprint(facts) if facts else ""
        if current_fingerprint != cluster.fingerprint:
            await self._repository.stale_previews(cluster.id)
        if (
            len(facts) != len(cluster.fact_ids)
            or self._cluster_fingerprint(facts) != cluster.fingerprint
        ):
            await self._repository.stale_previews(cluster.id)
            return 0, 0, False
        payload, ref_map = await self._input(facts)
        ready_preview = await self._repository.ready_preview(
            cluster_id=cluster.id,
            source_fingerprint=cluster.fingerprint,
        )
        preview_id: int | None = None
        if ready_preview is not None:
            preview_id, _preview_public_id, output = ready_preview
            calls = 0
        else:
            output, calls = await self._decide(
                payload,
                self_memory=facts[0].scope_type.value == "self",
                run=run,
                cluster=cluster,
            )
        self._validate_output(payload, output)
        embedding_ids: set[int] = set()
        operation_count = 0
        async with self._facts.repository.transaction() as session:
            current_map: dict[str, MemoryFact] = {}
            for ref, snapshot in ref_map.items():
                current = await self._facts.repository.get_fact(snapshot.id, session=session)
                if current is None or fact_signature(current) != fact_signature(snapshot):
                    raise RuntimeError("dream_cluster_stale")
                current_map[ref] = current
            used: set[str] = set()
            for action_index, action in enumerate(output.actions, start=1):
                sources = tuple(
                    current_map[ref] for ref in action.source_refs if ref in current_map
                )
                if len(sources) != len(action.source_refs):
                    raise ValueError("dream output referenced an unknown memory alias")
                if used.intersection(action.source_refs):
                    raise ValueError("dream output reused a memory alias")
                used.update(action.source_refs)
                anchor = self._anchor(action, sources, current_map)
                recompose_outputs = tuple(
                    DreamRecomposePlan(
                        source_facts=tuple(current_map[ref] for ref in item.source_refs),
                        content=item.content,
                        importance=item.importance,
                    )
                    for item in action.outputs
                )
                operation = await self._repository.create_operation(
                    cluster_id=cluster.id,
                    action_index=action_index,
                    operation_type=action.operation,
                    source_facts=sources,
                    anchor_fact_id=anchor.id if anchor is not None else None,
                    session=session,
                    decision_focuses=tuple(item.focus for item in action.outputs),
                )
                result = await self._mutations.mutate_dream(
                    dream_operation_id=operation.id,
                    operation_type=action.operation,
                    source_facts=sources,
                    anchor_fact_id=anchor.id if anchor is not None else None,
                    content=action.content,
                    importance=action.importance,
                    recompose_outputs=recompose_outputs,
                    bot_user_id=cluster.bot_user_id,
                    run_public_id=run.public_id,
                    session=session,
                )
                if result.changed:
                    embedding_ids.update(source.id for source in sources)
                loaded_outputs: list[MemoryFact] = []
                for fact_id in result.output_fact_ids:
                    loaded_output = await self._facts.repository.get_fact(fact_id, session=session)
                    if loaded_output is not None:
                        loaded_outputs.append(loaded_output)
                output_facts = tuple(loaded_outputs)
                if len(output_facts) != len(result.output_fact_ids):
                    raise RuntimeError("dream output fact disappeared before commit")
                latest_sources: dict[int, MemoryFact] = {}
                for source in sources:
                    latest = await self._facts.repository.get_fact(source.id, session=session)
                    if latest is not None:
                        latest_sources[source.id] = latest
                await self._repository.commit_operation(
                    operation.id,
                    output_fact_id=result.output_fact_id,
                    output_results=tuple((fact.id, fact_signature(fact)) for fact in output_facts),
                    added_evidence_ids=result.added_evidence_ids,
                    added_relation_ids=result.added_relation_ids,
                    result_signature=(fact_signature(output_facts[0]) if output_facts else None),
                    source_signatures={
                        fact_id: fact_signature(latest)
                        for fact_id, latest in latest_sources.items()
                    },
                    session=session,
                )
                for latest in latest_sources.values():
                    await self._repository.checkpoint_fact(
                        latest, operation_id=operation.id, session=session
                    )
                for output_fact in output_facts:
                    await self._repository.checkpoint_fact(
                        output_fact, operation_id=operation.id, session=session
                    )
                    embedding_ids.add(output_fact.id)
                operation_count += 1
            for ref, fact in current_map.items():
                if ref in used:
                    continue
                latest = await self._facts.repository.get_fact(fact.id, session=session)
                if latest is not None:
                    await self._repository.checkpoint_fact(
                        latest, operation_id=None, session=session
                    )
            if preview_id is not None:
                await self._repository.mark_preview_applied(preview_id, session=session)
        for fact_id in embedding_ids:
            await self._facts.schedule_embedding(fact_id)
        return calls, operation_count, True

    async def _load(self) -> DreamCandidateLoad:
        if not self._settings.memory_embedding_enabled:
            raise RuntimeError("Memory Dream 需要启用 memory embedding")
        profile_id = self._embeddings.profile_id
        if profile_id is None:
            raise RuntimeError("Memory Dream embedding profile 尚未就绪")
        if self._embeddings.jobs is not None:
            await self._embeddings.jobs.reconcile()
        return await self._repository.load_candidates(
            profile_id=profile_id,
            dimensions=self._embeddings.dimensions,
            documents=self._embeddings.documents,
        )

    async def _clusters(
        self,
        loaded: DreamCandidateLoad,
        *,
        incremental: bool,
    ) -> tuple[tuple[tuple[DreamCandidate, ...], ...], tuple[DreamCandidate, ...]]:
        checkpoints = await self._repository.checkpoint_map() if incremental else {}
        changed = {
            item.fact.id
            for item in loaded.candidates
            if not incremental or checkpoints.get(item.fact.id) != item.signature
        }
        partitions: dict[tuple[object, ...], list[DreamCandidate]] = defaultdict(list)
        for candidate in loaded.candidates:
            partitions[candidate.partition_identity].append(candidate)
        clusters: list[tuple[DreamCandidate, ...]] = []
        clustered_ids: set[int] = set()
        for partition in sorted(partitions, key=repr):
            rows = sorted(partitions[partition], key=lambda item: item.fact.id)
            by_id = {item.fact.id: item for item in rows}
            similarities: dict[tuple[int, int], float] = {}
            for index, left in enumerate(rows):
                for right in rows[index + 1 :]:
                    similarities[(left.fact.id, right.fact.id)] = self._codec.dot(
                        left.vector, right.vector
                    )
            remaining = set(by_id)
            while remaining:
                seed = max(
                    remaining,
                    key=lambda fact_id: (
                        sum(
                            self._similarity(fact_id, other, similarities)
                            >= self._settings.memory_dream_similarity_threshold
                            for other in remaining
                            if other != fact_id
                        ),
                        -fact_id,
                    ),
                )
                group = [seed]
                remaining.remove(seed)
                candidates = sorted(
                    remaining,
                    key=lambda fact_id: (
                        -self._similarity(seed, fact_id, similarities),
                        fact_id,
                    ),
                )
                for candidate_id in candidates:
                    if len(group) >= self._settings.memory_dream_max_cluster_size:
                        break
                    if all(
                        self._similarity(candidate_id, member, similarities)
                        >= self._settings.memory_dream_similarity_threshold
                        for member in group
                    ):
                        group.append(candidate_id)
                        remaining.remove(candidate_id)
                ids = tuple(sorted(group))
                should_recompose_single = (
                    len(ids) == 1
                    and by_id[ids[0]].fact.kind is MemoryKind.EPISODE
                    and len(by_id[ids[0]].fact.content)
                    > self._settings.memory_dream_episode_max_characters
                    and by_id[ids[0]].fact.source_type is not MemorySourceType.EXPLICIT
                    and by_id[ids[0]].fact.authority is not MemoryAuthority.EXPLICIT
                )
                if len(ids) < 2 and not should_recompose_single:
                    continue
                if not incremental or changed.intersection(ids):
                    cluster = tuple(by_id[fact_id] for fact_id in ids)
                    clusters.append(cluster)
                    clustered_ids.update(ids)
        isolated = tuple(
            item
            for item in loaded.candidates
            if item.fact.id in changed and item.fact.id not in clustered_ids
        )
        clusters.sort(key=lambda items: tuple(item.fact.id for item in items))
        return tuple(clusters), isolated

    @staticmethod
    def _similarity(
        left: int,
        right: int,
        similarities: dict[tuple[int, int], float],
    ) -> float:
        if left == right:
            return 1.0
        return similarities[(min(left, right), max(left, right))]

    def _stored_clusters(
        self,
        clusters: tuple[tuple[DreamCandidate, ...], ...],
    ) -> tuple[tuple[str, str, str, str, tuple[int, ...], str], ...]:
        rows = []
        for cluster in clusters:
            fact_ids = tuple(item.fact.id for item in cluster)
            partition_key = hashlib.sha256(repr(cluster[0].partition_identity).encode()).hexdigest()
            fingerprint = self._candidate_cluster_fingerprint(cluster)
            cluster_key = hashlib.sha256(
                f"{partition_key}:{','.join(map(str, fact_ids))}:{fingerprint}".encode()
            ).hexdigest()
            rows.append(
                (
                    cluster_key,
                    partition_key,
                    cluster[0].bot_user_id,
                    cluster[0].fact.kind.value,
                    fact_ids,
                    fingerprint,
                )
            )
        return tuple(rows)

    @staticmethod
    def _candidate_cluster_fingerprint(cluster: tuple[DreamCandidate, ...]) -> str:
        return hashlib.sha256(
            ":".join(
                item.signature for item in sorted(cluster, key=lambda row: row.fact.id)
            ).encode()
        ).hexdigest()

    @staticmethod
    def _cluster_fingerprint(facts: tuple[MemoryFact, ...]) -> str:
        return hashlib.sha256(
            ":".join(
                fact_signature(item) for item in sorted(facts, key=lambda row: row.id)
            ).encode()
        ).hexdigest()

    @staticmethod
    def _statistics(
        loaded: DreamCandidateLoad,
        *,
        clusters: tuple[tuple[DreamCandidate, ...], ...],
        isolated: tuple[DreamCandidate, ...],
    ) -> DreamPlanStatistics:
        return DreamPlanStatistics(
            eligible_facts=loaded.eligible_facts,
            ready_facts=len(loaded.candidates),
            missing_embeddings=loaded.missing_embeddings,
            ambiguous_bot_facts=loaded.ambiguous_bot_facts,
            partitions=len({item.partition_identity for item in loaded.candidates}),
            candidate_clusters=len(clusters),
            isolated_facts=len(isolated),
            estimated_model_calls=len(clusters),
        )

    async def _input(
        self, facts: tuple[MemoryFact, ...]
    ) -> tuple[DreamInput, dict[str, MemoryFact]]:
        ref_map = {f"memory_{index}": fact for index, fact in enumerate(facts, start=1)}
        remaining = self._settings.memory_dream_max_input_characters
        rows: list[DreamMemoryInput] = []
        for index, (ref, fact) in enumerate(ref_map.items()):
            facts_left = len(ref_map) - index
            content_budget = max(0, remaining // max(1, facts_left))
            content = fact.content[:content_budget]
            remaining -= len(content)
            evidence_rows = await self._facts.list_evidence(fact.id, limit=100_000)
            selected = self._select_evidence(evidence_rows)
            evidence: list[DreamEvidenceInput] = []
            for item in selected:
                if remaining <= 0:
                    break
                excerpt = item.excerpt[
                    : min(
                        self._settings.memory_dream_evidence_excerpt_characters,
                        remaining,
                    )
                ]
                remaining -= len(excerpt)
                evidence.append(
                    DreamEvidenceInput(
                        occurred_at=item.created_at,
                        relation=item.relation.value,
                        excerpt=excerpt,
                    )
                )
            rows.append(
                DreamMemoryInput(
                    ref=ref,
                    kind=fact.kind.value,
                    category=fact.category,
                    memory_key=fact.memory_key,
                    content=content,
                    importance=fact.importance,
                    confidence=fact.confidence,
                    source_type=fact.source_type.value,
                    authority=fact.authority.value,
                    status=fact.status.value,
                    conflict_state=fact.conflict_state.value,
                    valid_from=fact.valid_from,
                    valid_until=fact.valid_until,
                    evidence=tuple(evidence),
                )
            )
        first = facts[0]
        payload = DreamInput(
            scope_type=first.scope_type.value,
            subject_user_id=first.subject_user_id,
            group_id=first.group_id,
            visibility_type=(
                first.visibility_type.value if first.visibility_type is not None else None
            ),
            visibility_user_id=first.visibility_user_id,
            visibility_group_id=first.visibility_group_id,
            kind=first.kind.value,
            memories=tuple(rows),
        )
        return self._fit_input(payload), ref_map

    def _validate_output(self, payload: DreamInput, output: DreamOutput) -> None:
        expected = {item.ref for item in payload.memories}
        used = {ref for action in output.actions for ref in action.source_refs}
        if used != expected:
            raise ValueError("dream output must cover every input memory exactly once")
        by_ref = {item.ref: item for item in payload.memories}
        recomposed_refs: set[str] = set()
        for action in output.actions:
            if payload.kind != MemoryKind.EPISODE.value:
                if action.operation is DreamOperationType.RECOMPOSE:
                    raise ValueError("dream recompose is only available for episodes")
                continue
            if action.operation is DreamOperationType.SYNTHESIZE:
                raise ValueError("episodes must use recompose instead of synthesize")
            if action.operation is not DreamOperationType.RECOMPOSE:
                continue
            recomposed_refs.update(action.source_refs)
            source_rows = tuple(by_ref[ref] for ref in action.source_refs)
            if any(
                row.source_type == MemorySourceType.EXPLICIT.value
                or row.authority == MemoryAuthority.EXPLICIT.value
                for row in source_rows
            ):
                raise ValueError("dream cannot recompose an explicit episode")
            contents = tuple(item.content.strip() for item in action.outputs)
            if len({item.casefold() for item in contents}) != len(contents):
                raise ValueError("dream recompose emitted duplicate episode outputs")
            if any(
                len(content) > self._settings.memory_dream_episode_max_characters
                for content in contents
            ):
                raise ValueError("dream recompose episode exceeds the character limit")
            source_characters = sum(len(row.content) for row in source_rows)
            output_characters = sum(len(content) for content in contents)
            allowed = max(
                min(450, self._settings.memory_dream_episode_max_characters),
                int(source_characters * self._settings.memory_dream_episode_compression_ratio),
            )
            if output_characters > allowed:
                raise ValueError("dream recompose did not compress the source episodes")
        if recomposed_refs:
            source_characters = sum(len(by_ref[ref].content) for ref in recomposed_refs)
            output_characters = self._output_characters(output)
            allowed = max(
                min(450, self._settings.memory_dream_episode_max_characters),
                int(source_characters * self._settings.memory_dream_episode_compression_ratio),
            )
            if output_characters > allowed:
                raise ValueError("dream output did not meet the total episode compression ratio")

    @staticmethod
    def _output_characters(output: DreamOutput) -> int:
        return sum(
            len(action.content or "") + sum(len(item.content) for item in action.outputs)
            for action in output.actions
        )

    def _fit_input(self, payload: DreamInput) -> DreamInput:
        maximum = self._settings.memory_dream_max_input_characters
        current = payload
        while len(current.model_dump_json()) > maximum:
            overflow = len(current.model_dump_json()) - maximum
            rows = list(current.memories)
            changed = False
            for row_index in range(len(rows) - 1, -1, -1):
                row = rows[row_index]
                evidence = list(row.evidence)
                for evidence_index in range(len(evidence) - 1, -1, -1):
                    excerpt = evidence[evidence_index].excerpt
                    if not excerpt:
                        continue
                    cut = min(len(excerpt), max(1, overflow))
                    evidence[evidence_index] = evidence[evidence_index].model_copy(
                        update={"excerpt": excerpt[:-cut]}
                    )
                    overflow -= cut
                    changed = True
                    if overflow <= 0:
                        break
                row = row.model_copy(update={"evidence": tuple(evidence)})
                if overflow > 0 and row.content:
                    cut = min(len(row.content), max(1, overflow))
                    row = row.model_copy(update={"content": row.content[:-cut]})
                    overflow -= cut
                    changed = True
                rows[row_index] = row
                if overflow <= 0:
                    break
            if not changed:
                raise ValueError("memory_dream_input_metadata_exceeds_limit")
            current = current.model_copy(update={"memories": tuple(rows)})
        return current

    def _select_evidence(self, rows: tuple[MemoryEvidence, ...]) -> tuple[MemoryEvidence, ...]:
        limit = self._settings.memory_dream_evidence_per_fact
        if not rows or limit <= 0:
            return ()
        ordered = tuple(sorted(rows, key=lambda item: item.created_at))
        if len(ordered) <= limit:
            return ordered
        if limit == 1:
            return (ordered[-1],)
        if limit == 2:
            return (ordered[0], ordered[-1])
        middle = ordered[1:-1]
        return (ordered[0], *middle[-(limit - 2) :], ordered[-1])

    async def _decide(
        self,
        payload: DreamInput,
        *,
        self_memory: bool,
        run: DreamRun,
        cluster: DreamCluster,
    ) -> tuple[DreamOutput, int]:
        instruction = self._instruction(self_memory=self_memory)
        calls = 0
        try:
            if not await self._reserve_model_call(run, cluster):
                raise RuntimeError("memory_dream_model_call_budget_exhausted")
            calls += 1
            result = await self._run_model(instruction, payload)
            self._validate_output(payload, result)
        except (StructuredTaskError, ValueError) as exc:
            if not await self._reserve_model_call(run, cluster):
                raise
            calls += 1
            reason = exc.reason_code if isinstance(exc, StructuredTaskError) else str(exc)
            repair = self._repair_instruction(instruction, exc, reason=reason)
            try:
                result = await self._run_model(repair, payload)
                self._validate_output(payload, result)
            except (StructuredTaskError, ValueError) as repair_exc:
                reason_code = (
                    repair_exc.reason_code
                    if isinstance(repair_exc, StructuredTaskError)
                    else "dream_quality_validation_failed"
                )
                raise StructuredTaskError(
                    "Memory Dream structured output remained invalid after repair",
                    reason_code=reason_code,
                    detail=(
                        repair_exc.detail
                        if isinstance(repair_exc, StructuredTaskError)
                        else str(repair_exc)
                    ),
                    attempts=calls,
                ) from repair_exc
        refs = {item.ref for item in payload.memories}
        for action in result.actions:
            if not set(action.source_refs).issubset(refs):
                raise ValueError("dream output contains an unknown alias")
        return result, calls

    async def _preview_decide(
        self,
        payload: DreamInput,
        *,
        self_memory: bool,
    ) -> tuple[DreamOutput, int]:
        instruction = self._instruction(self_memory=self_memory)
        try:
            result = await self._run_model(instruction, payload)
            self._validate_output(payload, result)
            return result, 1
        except (StructuredTaskError, ValueError) as exc:
            reason = exc.reason_code if isinstance(exc, StructuredTaskError) else str(exc)
            repair = self._repair_instruction(instruction, exc, reason=reason)
            result = await self._run_model(repair, payload)
            self._validate_output(payload, result)
            return result, 2

    @staticmethod
    def _repair_instruction(
        instruction: str,
        error: StructuredTaskError | ValueError,
        *,
        reason: str,
    ) -> str:
        detail = error.detail if isinstance(error, StructuredTaskError) else str(error)
        return (
            f"{instruction}\n上一次输出无效：{reason}；具体位置：{detail or 'unknown'}。"
            "请重新划分语义事件、删掉所有不直接服务 focus 的旁支，并充分压缩。"
            "必须重新返回完整结果：每个 recompose output 都要同时包含 focus、source_refs、"
            "content、importance；整个簇最多一个 recompose action、合计最多 4 个 outputs。"
            "同一来源的拆分必须放在该 action 的 outputs 内，不能把来源重复放进多个 action。"
        )

    def _instruction(self, *, self_memory: bool) -> str:
        instruction = f"{_INSTRUCTION}\n{_RECOMPOSE_QUALITY_INSTRUCTION}"
        if self_memory:
            instruction += (
                f"\n【{self._settings.bot_display_name} 共享核心人格】\n"
                f"{self._settings.bot_persona}\n"
                "SELF 记忆应保持第一人称和这一人格的自然口吻。"
            )
        return instruction

    async def _reserve_model_call(self, run: DreamRun, cluster: DreamCluster) -> bool:
        return await self._repository.reserve_model_call(
            run_public_id=run.public_id,
            cluster_id=cluster.id,
            maximum=(
                self._settings.memory_dream_max_model_calls_per_run
                if run.mode is DreamRunMode.INCREMENTAL
                else None
            ),
        )

    async def _run_model(self, instruction: str, payload: DreamInput) -> DreamOutput:
        return await self._concurrency.run_llm(
            "memory-dream",
            lambda: self._structured.run(
                task=ModelTask.MEMORY_DREAM,
                instruction=instruction,
                structured_input=payload,
                output_model=DreamOutput,
                temperature=0.1,
                max_output_tokens=self._settings.memory_dream_max_output_tokens,
                allow_text_json=True,
                compact_schema=True,
                validation_retries=0,
            ),
            translate_cancellation=False,
        )

    def _anchor(
        self,
        action: DreamAction,
        sources: tuple[MemoryFact, ...],
        ref_map: dict[str, MemoryFact],
    ) -> MemoryFact | None:
        if action.operation not in {
            DreamOperationType.MERGE,
            DreamOperationType.SYNTHESIZE,
            DreamOperationType.RECOMPOSE,
            DreamOperationType.RESOLVE,
        }:
            return None
        explicit = tuple(
            item
            for item in sources
            if item.source_type is MemorySourceType.EXPLICIT
            or item.authority is MemoryAuthority.EXPLICIT
        )
        if explicit:
            return explicit[0]
        if action.operation is DreamOperationType.RESOLVE and action.anchor_ref is not None:
            return ref_map[action.anchor_ref]
        return self._mutations.select_dream_anchor(sources)
