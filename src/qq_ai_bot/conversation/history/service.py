"""L0 conversation history rollup: observe events, extractive coverage, Flash jobs.

Observed `chat_events` append sites (production ledger, not plugin session transcripts):

- `services/processor.py` inbound `append_inbound`
- `services/chat.py` confirmed outbound `append`
- `plugin_host/facades.py` external/plugin ledger writes
- `plugin_host/notification_delivery.py` notification delivery
- `automation/gateway.py` automation session events
- `services/agent_tools.py` remaining ledger writes
- `persistence/event_repository.py` ConversationRepository compatibility `append`

`memory/quality/runner.py` constructs its own ledger without a history observer.
`plugin_host/session_repository.append_message` is not `chat_events` and is not observed.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.history.errors import FrontierInvariantError, HistoryJobConflictError
from qq_ai_bot.conversation.history.models import (
    ConversationHistoryIdentity,
    ConversationHistoryJob,
    ConversationHistorySummary,
    HistoryJobKind,
    HistoryJobOutcome,
    HistorySummaryMode,
    HistorySummaryStatus,
)
from qq_ai_bot.conversation.history.policy import (
    ChildSummaryView,
    HistoryCompactionConfig,
    HistoryCompactionPolicy,
    RawRangeCandidate,
    SummaryRollupCandidate,
)
from qq_ai_bot.conversation.history.renderer import render_conversation_summary
from qq_ai_bot.conversation.history.repository import ConversationHistoryRepository
from qq_ai_bot.conversation.history.source import (
    ConversationSourceSnapshot,
    build_source_snapshot,
    extractive_compact,
    parent_source_fingerprint,
    source_fingerprint,
)
from qq_ai_bot.conversation.history.summarizer import (
    CompactionChildView,
    ConversationHistorySummarizer,
)
from qq_ai_bot.conversation.history.worker import ConversationHistoryJobResult
from qq_ai_bot.domain.conversations import ConversationIdentity, ScopeType
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.model_runtime.executor import ModelExecutor
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.runtime.origin import TurnOrigin

EXTRACTIVE_SUMMARIZER_VERSION = "extractive-v1"


class ConversationHistoryService:
    """Foreground observe + background L0 Flash upgrade. No Memory V2 writes."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: ConversationHistoryRepository,
        ledger: EventLedgerRepository,
        models: ModelExecutor | None = None,
        notify: Callable[[], None] | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._ledger = ledger
        self._notify = notify
        self._policy = HistoryCompactionPolicy(_config_from_settings(settings))
        self._summarizer = ConversationHistorySummarizer(models) if models is not None else None
        self._llm_origins = _parse_origins(settings.conversation_history_llm_origins)
        self._prompt_version = settings.conversation_history_rollup_prompt_version

    async def observe_event(self, record: EventRecord) -> None:
        if not self._settings.conversation_history_rollup_enabled:
            return
        identity = await self._history_identity(record)
        characters = len(record.content) + len(record.visual_summary)
        state = await self._repository.observe_event(
            identity,
            event_id=record.id,
            character_count=characters,
        )
        if (
            state.pending_event_count < self._policy.config.l0_min_events
            and state.pending_character_count < self._policy.config.l0_min_characters
        ):
            return
        if not self._allows_llm(record.origin):
            return
        if await self._repository.get_open_job(state.id, job_kind=HistoryJobKind.RAW_RANGE):
            return
        snapshot = await self._snapshot(
            identity,
            state_id=state.id,
            coverage_end_event_id=state.active_frontier_end_event_id,
            last_seen_event_id=state.last_seen_event_id,
        )
        candidate = self._policy.select_l0_candidate(
            snapshot,
            coverage_end_event_id=state.active_frontier_end_event_id,
            dropped_prefix_ids=(),
            must_roll=False,
        )
        if candidate is None:
            return
        await self._enqueue_raw_range(state.id, candidate)
        if self._notify is not None:
            self._notify()

    async def ensure_extractive_coverage(
        self,
        record: EventRecord,
        *,
        rendered: tuple[tuple[int, tuple[int, ...], ChatMessage], ...],
        anchor_event_id: int | None,
        high_event_limit: int,
        high_character_limit: int,
        fallback_anchor_event_id: int | None,
    ) -> ConversationHistorySummary | None:
        """Sync extractive L0 when the existing window clock must roll. Zero LLM."""

        if not self._settings.conversation_history_rollup_enabled:
            return None
        identity = await self._history_identity(record)
        state = await self._repository.get_or_create_state(identity)
        if self._policy.allow_raw_window_shift(
            has_active_coverage=state.active_frontier_end_event_id > 0
        ):
            return None
        dropped = self._policy.must_roll_prefix(
            rendered,
            anchor_event_id=anchor_event_id,
            high_event_limit=high_event_limit,
            high_character_limit=high_character_limit,
            fallback_anchor_event_id=fallback_anchor_event_id,
        )
        if not dropped:
            return None
        last_seen = max(
            state.last_seen_event_id,
            max((event_id for event_id, _, _ in rendered), default=0),
        )
        snapshot = await self._snapshot(
            identity,
            state_id=state.id,
            coverage_end_event_id=state.active_frontier_end_event_id,
            last_seen_event_id=last_seen,
        )
        candidate = await self._extractive_candidate(
            identity,
            snapshot,
            state_id=state.id,
            coverage_end_event_id=state.active_frontier_end_event_id,
            dropped=dropped,
        )
        if candidate is None:
            return None
        summary = await self._commit_extractive(snapshot, candidate)
        if self._allows_llm(record.origin):
            await self._enqueue_raw_range(state.id, candidate)
            if self._notify is not None:
                self._notify()
        return summary

    def allow_raw_window_shift(self, *, has_active_coverage: bool) -> bool:
        return self._policy.allow_raw_window_shift(has_active_coverage=has_active_coverage)

    async def process(self, job: ConversationHistoryJob) -> ConversationHistoryJobResult:
        if job.job_kind is HistoryJobKind.RAW_RANGE:
            result = await self._process_raw_range(job)
            if result.outcome is HistoryJobOutcome.SUMMARY:
                await self.consider_parent_rollup(job.state_id)
            return result
        if job.job_kind is HistoryJobKind.SUMMARY_ROLLUP:
            result = await self._process_parent(job)
            if result.outcome is HistoryJobOutcome.SUMMARY:
                await self.consider_parent_rollup(job.state_id)
            return result
        return ConversationHistoryJobResult(outcome=HistoryJobOutcome.NO_CHANGE)

    async def consider_parent_rollup(self, state_id: int) -> None:
        """Enqueue the earliest ready parent job. Safe to call after any frontier write."""

        if not self._settings.conversation_history_rollup_enabled:
            return
        candidate, fingerprint = await self._parent_candidate(state_id)
        if candidate is None or fingerprint is None:
            return
        await self._repository.enqueue_job(
            state_id=state_id,
            job_kind=HistoryJobKind.SUMMARY_ROLLUP,
            source_level=candidate.level - 1,
            source_start_id=candidate.start_event_id,
            source_end_id=candidate.end_event_id,
            source_fingerprint=fingerprint,
            summarizer_version=self._prompt_version,
        )
        if self._notify is not None:
            self._notify()

    async def _process_raw_range(self, job: ConversationHistoryJob) -> ConversationHistoryJobResult:
        if job.job_kind is not HistoryJobKind.RAW_RANGE:
            return ConversationHistoryJobResult(outcome=HistoryJobOutcome.NO_CHANGE)
        if self._summarizer is None:
            raise RuntimeError("conversation compaction model executor is not configured")
        identity = await self._identity_for_state(job.state_id)
        events = await self._repository.load_source_events(
            identity,
            start_event_id=job.source_start_id,
            end_event_id=job.source_end_id,
        )
        snapshot = build_source_snapshot(
            state_id=job.state_id,
            reset_at=identity.reset_at,
            scope_type=identity.scope_type,
            events=events,
        )
        if source_fingerprint(snapshot) != job.source_fingerprint:
            raise HistoryJobConflictError("source fingerprint mismatch")
        output = await self._summarizer.summarize_events(snapshot, level=0)
        rendered = render_conversation_summary(output)
        summary = await self._repository.commit_l0_summary(
            state_id=job.state_id,
            event_ids=snapshot.event_ids,
            fingerprint=job.source_fingerprint,
            mode=HistorySummaryMode.MODEL_SUMMARY,
            summarizer_version=self._prompt_version,
            rendered_text=rendered,
            structured_payload_json=output.model_dump_json(),
            start_occurred_at=snapshot.events[0].occurred_at,
            end_occurred_at=snapshot.events[-1].occurred_at,
            source_character_count=sum(
                len(item.content) + len(item.visual_summary) for item in snapshot.events
            ),
        )
        return ConversationHistoryJobResult(
            outcome=HistoryJobOutcome.SUMMARY,
            result_summary_id=summary.id,
        )

    async def _process_parent(self, job: ConversationHistoryJob) -> ConversationHistoryJobResult:
        if self._summarizer is None:
            raise RuntimeError("conversation compaction model executor is not configured")
        candidate, fingerprint = await self._parent_candidate(job.state_id)
        if candidate is None or fingerprint is None:
            return ConversationHistoryJobResult(outcome=HistoryJobOutcome.NO_CHANGE)
        if fingerprint != job.source_fingerprint:
            return ConversationHistoryJobResult(outcome=HistoryJobOutcome.NO_CHANGE)
        children = await self._repository.load_source_summaries(candidate.child_ids)
        if len(children) != len(candidate.child_ids):
            raise FrontierInvariantError("parent children are missing")
        if any(item.status is not HistorySummaryStatus.ACTIVE for item in children):
            raise FrontierInvariantError("parent children must still be active")
        if any(item.mode is not HistorySummaryMode.MODEL_SUMMARY for item in children):
            raise FrontierInvariantError("parent children must be model summaries")
        views = tuple(
            CompactionChildView(
                summary_id=item.id,
                level=item.level,
                start_event_id=item.start_event_id,
                end_event_id=item.end_event_id,
                rendered_text=item.rendered_text,
            )
            for item in children
        )
        output = await self._summarizer.summarize_children(
            views,
            level=candidate.level,
            fingerprint=fingerprint,
        )
        rendered = render_conversation_summary(output)
        started = children[0].start_occurred_at or children[0].end_occurred_at
        ended = children[-1].end_occurred_at or children[-1].start_occurred_at
        if started is None or ended is None:
            raise FrontierInvariantError("parent children are missing occurrence times")
        summary = await self._repository.commit_parent_summary_and_retire_children(
            state_id=job.state_id,
            child_ids=candidate.child_ids,
            fingerprint=fingerprint,
            summarizer_version=self._prompt_version,
            rendered_text=rendered,
            structured_payload_json=output.model_dump_json(),
            start_occurred_at=started,
            end_occurred_at=ended,
            source_character_count=sum(item.source_character_count for item in children),
        )
        return ConversationHistoryJobResult(
            outcome=HistoryJobOutcome.SUMMARY,
            result_summary_id=summary.id,
        )

    async def _parent_candidate(
        self, state_id: int
    ) -> tuple[SummaryRollupCandidate | None, str | None]:
        snapshot = await self._repository.load_context_snapshot(state_id)
        views = tuple(
            ChildSummaryView(
                summary_id=item.id,
                level=item.level,
                start_event_id=item.start_event_id,
                end_event_id=item.end_event_id,
                rendered_text=item.rendered_text,
            )
            for item in snapshot.frontier
            if item.mode is HistorySummaryMode.MODEL_SUMMARY
        )
        candidate = self._policy.select_parent_candidate(views)
        if candidate is None:
            return None, None
        by_id = {item.id: item for item in snapshot.frontier}
        fingerprints = tuple(by_id[child_id].source_fingerprint for child_id in candidate.child_ids)
        identity = await self._identity_for_state(state_id)
        reset_epoch = "none" if identity.reset_at is None else identity.reset_at.isoformat()
        fingerprint = parent_source_fingerprint(
            state_id=state_id,
            reset_epoch=reset_epoch,
            child_fingerprints=fingerprints,
        )
        return candidate, fingerprint

    async def _extractive_candidate(
        self,
        identity: ConversationHistoryIdentity,
        snapshot: ConversationSourceSnapshot,
        *,
        state_id: int,
        coverage_end_event_id: int,
        dropped: tuple[int, ...],
    ) -> RawRangeCandidate | None:
        open_job = await self._repository.get_open_job(state_id, job_kind=HistoryJobKind.RAW_RANGE)
        if open_job is not None:
            events = await self._repository.load_source_events(
                identity,
                start_event_id=open_job.source_start_id,
                end_event_id=open_job.source_end_id,
            )
            job_snapshot = build_source_snapshot(
                state_id=state_id,
                reset_at=identity.reset_at,
                scope_type=identity.scope_type,
                events=events,
            )
            if source_fingerprint(job_snapshot) != open_job.source_fingerprint:
                raise HistoryJobConflictError("open job source fingerprint mismatch")
            return RawRangeCandidate(
                event_ids=job_snapshot.event_ids,
                start_event_id=open_job.source_start_id,
                end_event_id=open_job.source_end_id,
                character_count=sum(
                    len(item.content) + len(item.visual_summary) for item in job_snapshot.events
                ),
                fingerprint=open_job.source_fingerprint,
                extractive_text=extractive_compact(
                    job_snapshot,
                    max_characters=self._policy.config.extractive_max_characters,
                ),
                prefetch=True,
            )
        return self._policy.select_l0_candidate(
            snapshot,
            coverage_end_event_id=coverage_end_event_id,
            dropped_prefix_ids=dropped,
            must_roll=True,
        )

    async def _commit_extractive(
        self,
        snapshot: ConversationSourceSnapshot,
        candidate: RawRangeCandidate,
    ) -> ConversationHistorySummary:
        selected = tuple(
            item for item in snapshot.events if item.event_id in set(candidate.event_ids)
        )
        payload = json.dumps(
            {
                "mode": "extractive",
                "event_ids": list(candidate.event_ids),
                "narrative": candidate.extractive_text[:900],
            },
            ensure_ascii=False,
        )
        try:
            return await self._repository.commit_l0_summary(
                state_id=snapshot.state_id,
                event_ids=candidate.event_ids,
                fingerprint=candidate.fingerprint,
                mode=HistorySummaryMode.EXTRACTIVE,
                summarizer_version=EXTRACTIVE_SUMMARIZER_VERSION,
                rendered_text=candidate.extractive_text,
                structured_payload_json=payload,
                start_occurred_at=selected[0].occurred_at,
                end_occurred_at=selected[-1].occurred_at,
                source_character_count=candidate.character_count,
            )
        except FrontierInvariantError:
            current = await self._repository.load_context_snapshot(snapshot.state_id)
            for item in current.frontier:
                if item.source_fingerprint == candidate.fingerprint:
                    return item
            raise

    async def _enqueue_raw_range(self, state_id: int, candidate: RawRangeCandidate) -> None:
        await self._repository.enqueue_job(
            state_id=state_id,
            job_kind=HistoryJobKind.RAW_RANGE,
            source_level=0,
            source_start_id=candidate.start_event_id,
            source_end_id=candidate.end_event_id,
            source_fingerprint=candidate.fingerprint,
            summarizer_version=self._prompt_version,
        )

    async def _snapshot(
        self,
        identity: ConversationHistoryIdentity,
        *,
        state_id: int,
        coverage_end_event_id: int,
        last_seen_event_id: int,
    ) -> ConversationSourceSnapshot:
        start_event_id = max(coverage_end_event_id, 0) + 1
        events = await self._repository.load_source_events(
            identity,
            start_event_id=start_event_id,
            end_event_id=max(last_seen_event_id, start_event_id),
        )
        return build_source_snapshot(
            state_id=state_id,
            reset_at=identity.reset_at,
            scope_type=identity.scope_type,
            events=events,
        )

    async def _history_identity(self, record: EventRecord) -> ConversationHistoryIdentity:
        conversation = _conversation_identity(record)
        reset_at = await self._ledger.context_reset(conversation)
        if record.scope_type is ScopeType.PRIVATE:
            return ConversationHistoryIdentity(
                bot_user_id=record.bot_user_id,
                scope_type=ScopeType.PRIVATE,
                private_peer_user_id=record.private_peer_user_id or record.sender_user_id,
                reset_at=reset_at,
            )
        return ConversationHistoryIdentity(
            bot_user_id=record.bot_user_id,
            scope_type=ScopeType.GROUP,
            group_id=record.group_id,
            reset_at=reset_at,
        )

    async def _identity_for_state(self, state_id: int) -> ConversationHistoryIdentity:
        snapshot = await self._repository.load_context_snapshot(state_id)
        state = snapshot.state
        return ConversationHistoryIdentity(
            bot_user_id=state.bot_user_id,
            scope_type=ScopeType(state.scope_type),
            private_peer_user_id=state.private_peer_user_id,
            group_id=state.group_id,
            reset_at=state.reset_at,
        )

    def _allows_llm(self, origin: str) -> bool:
        try:
            parsed = TurnOrigin(origin)
        except ValueError:
            return False
        return parsed in self._llm_origins


def _conversation_identity(record: EventRecord) -> ConversationIdentity:
    if record.scope_type is ScopeType.PRIVATE:
        return ConversationIdentity.private(record.private_peer_user_id or record.sender_user_id)
    return ConversationIdentity.group(record.group_id or "", record.sender_user_id)


def _config_from_settings(settings: Settings) -> HistoryCompactionConfig:
    return HistoryCompactionConfig(
        raw_tail_events=settings.conversation_history_raw_tail_events,
        raw_tail_characters=settings.conversation_history_raw_tail_characters,
        l0_min_events=settings.conversation_history_rollup_l0_min_events,
        l0_min_characters=settings.conversation_history_rollup_l0_min_characters,
        l0_max_events=settings.conversation_history_rollup_l0_max_events,
        l0_max_characters=settings.conversation_history_rollup_l0_max_characters,
        extractive_max_characters=settings.conversation_history_extractive_max_characters,
        fan_in=settings.conversation_history_rollup_fan_in,
        fan_in_characters=settings.conversation_history_rollup_fan_in_characters,
        max_level=settings.conversation_history_rollup_max_level,
        history_window_low_watermark_ratio=settings.history_window_low_watermark_ratio,
    )


def _parse_origins(raw: str) -> frozenset[TurnOrigin]:
    origins: list[TurnOrigin] = []
    for item in raw.split(","):
        name = item.strip()
        if not name:
            continue
        origins.append(TurnOrigin(name))
    return frozenset(origins) or frozenset({TurnOrigin.USER_MESSAGE})
