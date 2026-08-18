"""Local CLI operations for conversation history rollup. Not agent tools."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.history.db_models import (
    ConversationHistoryRollupJobModel,
    ConversationHistoryStateModel,
    ConversationHistorySummaryMemberModel,
    ConversationHistorySummaryModel,
)
from qq_ai_bot.conversation.history.errors import HistoryIdentityError, HistoryJobConflictError
from qq_ai_bot.conversation.history.models import (
    ConversationHistoryIdentity,
    ConversationHistoryJob,
    ConversationHistoryState,
    ConversationHistorySummary,
    HistoryJobOutcome,
    HistoryJobStatus,
    HistoryMemberType,
    HistorySummaryMode,
    HistorySummaryStatus,
)
from qq_ai_bot.conversation.history.policy import HistoryCompactionConfig, HistoryCompactionPolicy
from qq_ai_bot.conversation.history.repository import ConversationHistoryRepository
from qq_ai_bot.conversation.history.service import EXTRACTIVE_SUMMARIZER_VERSION
from qq_ai_bot.conversation.history.source import ConversationSourceSnapshot, build_source_snapshot
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.models import ChatEventModel

logger = logging.getLogger(__name__)

_REBUILD_FINGERPRINT = "rebuild"
_OPS_VERSION = "ops-v1"


class HistoryHealthFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    state_id: int | None = None
    summary_id: int | None = None
    job_id: int | None = None
    member_id: int | None = None
    detail: str = Field(min_length=1)


class ConversationHistoryOperations:
    """Inspect, rebuild, invalidate, and reconcile derived summaries only."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: ConversationHistoryRepository,
        ledger: EventLedgerRepository,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._ledger = ledger
        self._policy = HistoryCompactionPolicy(_compaction_config(settings))

    async def status(self) -> dict[str, Any]:
        states = await self._repository.list_states()
        jobs = await self._repository.list_jobs(
            statuses=(
                HistoryJobStatus.PENDING,
                HistoryJobStatus.PROCESSING,
                HistoryJobStatus.FAILED,
            )
        )
        health = await self.health()
        findings = health["findings"]
        payload: dict[str, Any] = {
            "states": [_redact_state(item) for item in states],
            "jobs": [_redact_job(item) for item in jobs],
            "health": health,
        }
        logger.info(
            "history_rollup_status states=%s jobs=%s findings=%s",
            len(states),
            len(jobs),
            len(findings) if isinstance(findings, list) else 0,
        )
        return payload

    async def inspect(self, identity: ConversationHistoryIdentity) -> dict[str, Any]:
        _require_exact_identity(identity)
        state = await self._repository.get_state(identity)
        if state is None:
            missing: dict[str, Any] = {
                "state": None,
                "frontier": [],
                "summaries": [],
                "jobs": [],
                "coverage_end_event_id": 0,
            }
            logger.info("history_rollup_inspect missing_state=1")
            return missing
        summaries = await self._repository.list_summaries(state.id)
        frontier = tuple(item for item in summaries if item.status is HistorySummaryStatus.ACTIVE)
        jobs = await self._repository.list_jobs(state_id=state.id)
        found: dict[str, Any] = {
            "state": _redact_state(state),
            "frontier": [_redact_summary(item) for item in frontier],
            "summaries": [_redact_summary(item) for item in summaries],
            "jobs": [_redact_job(item) for item in jobs],
            "coverage_end_event_id": state.active_frontier_end_event_id,
        }
        logger.info(
            "history_rollup_inspect state_id=%s frontier=%s summaries=%s coverage_end=%s",
            state.id,
            len(frontier),
            len(summaries),
            state.active_frontier_end_event_id,
        )
        return found

    async def rebuild(
        self,
        identity: ConversationHistoryIdentity,
        *,
        commit: bool = False,
    ) -> dict[str, Any]:
        _require_exact_identity(identity)
        plan = await self._plan_rebuild(identity)
        logger.info(
            "history_rollup_rebuild state_id=%s dry_run=%s event_count=%s planned_slices=%s "
            "coverage_end=%s",
            plan.get("state_id"),
            not commit,
            plan["event_count"],
            len(plan["planned_l0_slices"]),
            plan["planned_coverage_end"],
        )
        if not commit:
            return plan
        return await self._commit_rebuild(identity, plan)

    async def invalidate(self, identity: ConversationHistoryIdentity) -> dict[str, Any]:
        _require_exact_identity(identity)
        state = await self._repository.get_state(identity)
        if state is None:
            raise HistoryIdentityError("exact session has no history state to invalidate")
        if await self._repository.has_live_lease(state.id):
            raise HistoryJobConflictError(
                "invalidate cannot run while a worker holds a live lease on this state"
            )
        count = await self._repository.invalidate_all_summaries(state.id)
        refreshed = await self._repository.get_state_by_id(state.id)
        logger.info("history_rollup_invalidate state_id=%s summaries=%s", state.id, count)
        return {
            "state_id": state.id,
            "invalidated_summaries": count,
            "coverage_end_event_id": (
                0 if refreshed is None else refreshed.active_frontier_end_event_id
            ),
        }

    async def reconcile(self, identity: ConversationHistoryIdentity) -> dict[str, Any]:
        _require_exact_identity(identity)
        state = await self._repository.reconcile_state_counters(identity)
        logger.info(
            "history_rollup_reconcile state_id=%s last_seen=%s pending_events=%s "
            "pending_characters=%s",
            state.id,
            state.last_seen_event_id,
            state.pending_event_count,
            state.pending_character_count,
        )
        return {"state": _redact_state(state)}

    async def health(self) -> dict[str, Any]:
        findings = await self._scan_health()
        payload = {
            "ok": not findings,
            "findings": [item.model_dump(mode="json") for item in findings],
        }
        logger.info("history_rollup_health ok=%s findings=%s", payload["ok"], len(findings))
        return payload

    async def _plan_rebuild(self, identity: ConversationHistoryIdentity) -> dict[str, Any]:
        state = await self._repository.get_state(identity)
        latest = await self._repository.latest_event_id(identity)
        events = (
            ()
            if latest <= 0
            else await self._repository.load_source_events(
                identity, start_event_id=1, end_event_id=latest
            )
        )
        summaries = () if state is None else await self._repository.list_summaries(state.id)
        active = tuple(item for item in summaries if item.status is HistorySummaryStatus.ACTIVE)
        snapshot = build_source_snapshot(
            state_id=0 if state is None else state.id,
            reset_at=identity.reset_at,
            scope_type=identity.scope_type,
            events=events,
        )
        slices, coverage_end = self._planned_slices(snapshot)
        hot_tail = self._policy.hot_tail_boundary(snapshot)
        return {
            "dry_run": True,
            "writes": False,
            "state_id": None if state is None else state.id,
            "event_count": len(events),
            "existing_summary_count": len(summaries),
            "existing_active_count": len(active),
            "hot_tail_protected_event_count": len(hot_tail.protected_event_ids),
            "planned_l0_slices": slices,
            "planned_coverage_end": coverage_end,
        }

    def _planned_slices(
        self, snapshot: ConversationSourceSnapshot
    ) -> tuple[list[dict[str, Any]], int]:
        slices: list[dict[str, Any]] = []
        coverage_end = 0
        while True:
            candidate = self._policy.select_l0_candidate(
                snapshot,
                coverage_end_event_id=coverage_end,
                dropped_prefix_ids=(),
                must_roll=False,
            )
            if candidate is None:
                break
            slices.append(
                {
                    "start_event_id": candidate.start_event_id,
                    "end_event_id": candidate.end_event_id,
                    "event_count": len(candidate.event_ids),
                    "source_fingerprint": candidate.fingerprint,
                    "output_characters": len(candidate.extractive_text),
                }
            )
            coverage_end = candidate.end_event_id
        return slices, coverage_end

    async def _commit_rebuild(
        self,
        identity: ConversationHistoryIdentity,
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        state = await self._repository.get_or_create_state(identity)
        owner = f"history-rebuild-{uuid.uuid4().hex}"
        lease_seconds = max(self._settings.conversation_history_rollup_lease_seconds, 300)
        job = await self._repository.begin_rebuild_lock(
            state.id,
            lease_owner=owner,
            lease_seconds=lease_seconds,
            source_fingerprint=_REBUILD_FINGERPRINT,
            summarizer_version=_OPS_VERSION,
        )
        try:
            await self._repository.invalidate_all_summaries(state.id)
            await self._repository.reconcile_state_counters(identity)
            latest = await self._repository.latest_event_id(identity)
            events = (
                ()
                if latest <= 0
                else await self._repository.load_source_events(
                    identity, start_event_id=1, end_event_id=latest
                )
            )
            snapshot = build_source_snapshot(
                state_id=state.id,
                reset_at=identity.reset_at,
                scope_type=identity.scope_type,
                events=events,
            )
            created = 0
            coverage_end = 0
            fingerprints: list[str] = []
            while True:
                candidate = self._policy.select_l0_candidate(
                    snapshot,
                    coverage_end_event_id=coverage_end,
                    dropped_prefix_ids=(),
                    must_roll=False,
                )
                if candidate is None:
                    break
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
                await self._repository.commit_l0_summary(
                    state_id=state.id,
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
                created += 1
                fingerprints.append(candidate.fingerprint)
                coverage_end = candidate.end_event_id
            refreshed = await self._repository.reconcile_state_counters(identity)
            await self._repository.complete_job(
                job.id,
                lease_owner=owner,
                outcome=HistoryJobOutcome.NO_CHANGE,
                result_summary_id=None,
            )
        except Exception:
            await self._repository.fail_job(
                job.id,
                lease_owner=owner,
                error_category="rebuild_failed",
            )
            raise
        result = {
            "dry_run": False,
            "writes": True,
            "state_id": refreshed.id,
            "event_count": plan["event_count"],
            "created_l0_summaries": created,
            "source_fingerprints": fingerprints,
            "coverage_end_event_id": refreshed.active_frontier_end_event_id,
            "planned_coverage_end": plan["planned_coverage_end"],
        }
        logger.info(
            "history_rollup_rebuild_commit state_id=%s created=%s coverage_end=%s",
            refreshed.id,
            created,
            refreshed.active_frontier_end_event_id,
        )
        return result

    async def _scan_health(self) -> tuple[HistoryHealthFinding, ...]:
        findings: list[HistoryHealthFinding] = []
        async with self._repository.database.sessions() as session:
            active = (
                await session.scalars(
                    select(ConversationHistorySummaryModel)
                    .where(
                        ConversationHistorySummaryModel.status == HistorySummaryStatus.ACTIVE.value
                    )
                    .order_by(
                        ConversationHistorySummaryModel.state_id,
                        ConversationHistorySummaryModel.start_event_id,
                    )
                )
            ).all()
            previous: ConversationHistorySummaryModel | None = None
            for row in active:
                if previous is not None and previous.state_id == row.state_id:
                    if row.start_event_id <= previous.end_event_id:
                        findings.append(
                            HistoryHealthFinding(
                                kind="overlap",
                                state_id=row.state_id,
                                summary_id=row.id,
                                detail=(
                                    f"active {row.id} {row.start_event_id}-{row.end_event_id} "
                                    f"overlaps {previous.id} "
                                    f"{previous.start_event_id}-{previous.end_event_id}"
                                ),
                            )
                        )
                    elif row.start_event_id != previous.end_event_id + 1:
                        findings.append(
                            HistoryHealthFinding(
                                kind="frontier_gap",
                                state_id=row.state_id,
                                summary_id=row.id,
                                detail=(
                                    f"active {previous.id} ends {previous.end_event_id} "
                                    f"and {row.id} starts {row.start_event_id}"
                                ),
                            )
                        )
                previous = row

            member_rows = (
                await session.execute(
                    select(
                        ConversationHistorySummaryMemberModel,
                        ConversationHistorySummaryModel,
                        ConversationHistoryStateModel,
                        ChatEventModel,
                    )
                    .join(
                        ConversationHistorySummaryModel,
                        ConversationHistorySummaryMemberModel.summary_id
                        == ConversationHistorySummaryModel.id,
                    )
                    .join(
                        ConversationHistoryStateModel,
                        ConversationHistorySummaryModel.state_id
                        == ConversationHistoryStateModel.id,
                    )
                    .outerjoin(
                        ChatEventModel,
                        ConversationHistorySummaryMemberModel.source_event_id == ChatEventModel.id,
                    )
                    .where(
                        ConversationHistorySummaryMemberModel.member_type
                        == HistoryMemberType.EVENT.value,
                        ConversationHistorySummaryModel.status
                        != HistorySummaryStatus.INVALIDATED.value,
                    )
                )
            ).all()
            for member, summary, state, event in member_rows:
                orphan = event is None
                if event is not None:
                    if event.bot_user_id != state.bot_user_id:
                        orphan = True
                    elif event.scope_type != state.scope_type:
                        orphan = True
                    elif (
                        state.scope_type == ScopeType.PRIVATE.value
                        and event.private_peer_user_id != state.private_peer_user_id
                    ):
                        orphan = True
                    elif (
                        state.scope_type == ScopeType.GROUP.value
                        and event.group_id != state.group_id
                    ):
                        orphan = True
                    elif event.id < summary.start_event_id or event.id > summary.end_event_id:
                        orphan = True
                if orphan:
                    findings.append(
                        HistoryHealthFinding(
                            kind="orphan_member",
                            state_id=summary.state_id,
                            summary_id=summary.id,
                            member_id=member.id,
                            detail=f"event member {member.source_event_id} is not in this session",
                        )
                    )

            parent_members = (
                await session.scalars(
                    select(ConversationHistorySummaryMemberModel).where(
                        ConversationHistorySummaryMemberModel.member_type
                        == HistoryMemberType.SUMMARY.value
                    )
                )
            ).all()
            child_ids = tuple(
                item.source_summary_id
                for item in parent_members
                if item.source_summary_id is not None
            )
            children = {}
            if child_ids:
                child_rows = (
                    await session.scalars(
                        select(ConversationHistorySummaryModel).where(
                            ConversationHistorySummaryModel.id.in_(child_ids)
                        )
                    )
                ).all()
                children = {row.id: row for row in child_rows}
            parents = {}
            parent_ids = tuple({item.summary_id for item in parent_members})
            if parent_ids:
                parent_rows = (
                    await session.scalars(
                        select(ConversationHistorySummaryModel).where(
                            ConversationHistorySummaryModel.id.in_(parent_ids)
                        )
                    )
                ).all()
                parents = {row.id: row for row in parent_rows}
            for member in parent_members:
                parent = parents.get(member.summary_id)
                child = children.get(member.source_summary_id or 0)
                if parent is None or parent.status == HistorySummaryStatus.INVALIDATED.value:
                    continue
                if child is None or child.state_id != parent.state_id:
                    findings.append(
                        HistoryHealthFinding(
                            kind="orphan_member",
                            state_id=None if parent is None else parent.state_id,
                            summary_id=member.summary_id,
                            member_id=member.id,
                            detail=f"summary member {member.source_summary_id} is missing",
                        )
                    )

            replaced = (
                await session.scalars(
                    select(ConversationHistorySummaryModel).where(
                        ConversationHistorySummaryModel.replaced_by_summary_id.is_not(None),
                        ConversationHistorySummaryModel.status
                        != HistorySummaryStatus.INVALIDATED.value,
                    )
                )
            ).all()
            replacement_ids = tuple(
                item.replaced_by_summary_id
                for item in replaced
                if item.replaced_by_summary_id is not None
            )
            replacements = {}
            if replacement_ids:
                replacement_rows = (
                    await session.scalars(
                        select(ConversationHistorySummaryModel).where(
                            ConversationHistorySummaryModel.id.in_(replacement_ids)
                        )
                    )
                ).all()
                replacements = {row.id: row for row in replacement_rows}
            for child in replaced:
                parent = replacements.get(child.replaced_by_summary_id or 0)
                bad = parent is None
                if parent is not None:
                    if parent.state_id != child.state_id:
                        bad = True
                    elif child.start_event_id < parent.start_event_id:
                        bad = True
                    elif child.end_event_id > parent.end_event_id:
                        bad = True
                if bad:
                    findings.append(
                        HistoryHealthFinding(
                            kind="bad_replacement",
                            state_id=child.state_id,
                            summary_id=child.id,
                            detail=(
                                f"summary {child.id} replaced_by={child.replaced_by_summary_id} "
                                "does not cover the child range"
                            ),
                        )
                    )

            now = datetime.now(UTC)
            stale = (
                await session.scalars(
                    select(ConversationHistoryRollupJobModel).where(
                        ConversationHistoryRollupJobModel.status
                        == HistoryJobStatus.PROCESSING.value,
                        ConversationHistoryRollupJobModel.lease_until.is_not(None),
                        ConversationHistoryRollupJobModel.lease_until < now,
                    )
                )
            ).all()
            for job in stale:
                findings.append(
                    HistoryHealthFinding(
                        kind="stale_lease",
                        state_id=job.state_id,
                        job_id=job.id,
                        detail=f"job {job.id} lease expired",
                    )
                )
        return tuple(findings)


def parse_history_identity(
    *,
    bot_user_id: str | None,
    scope: str | None,
    user_id: str | None,
    group_id: str | None,
    reset_at: str | None,
) -> ConversationHistoryIdentity:
    if not bot_user_id or not bot_user_id.strip():
        raise HistoryIdentityError("exact identity requires --bot-user-id")
    if not scope or not scope.strip():
        raise HistoryIdentityError("exact identity requires --scope")
    try:
        scope_type = ScopeType(scope.strip())
    except ValueError as error:
        raise HistoryIdentityError("exact identity requires private or group scope") from error
    peer = user_id.strip() if user_id else None
    group = group_id.strip() if group_id else None
    epoch = _parse_reset_at(reset_at)
    if scope_type is ScopeType.PRIVATE:
        if not peer or group is not None:
            raise HistoryIdentityError("private identity requires --user-id and no --group-id")
        return ConversationHistoryIdentity(
            bot_user_id=bot_user_id.strip(),
            scope_type=scope_type,
            private_peer_user_id=peer,
            reset_at=epoch,
        )
    if not group or peer is not None:
        raise HistoryIdentityError("group identity requires --group-id and no --user-id")
    return ConversationHistoryIdentity(
        bot_user_id=bot_user_id.strip(),
        scope_type=scope_type,
        group_id=group,
        reset_at=epoch,
    )


def _require_exact_identity(identity: ConversationHistoryIdentity) -> None:
    ConversationHistoryRepository._validate_identity(identity)
    if not identity.bot_user_id.strip():
        raise HistoryIdentityError("exact identity requires --bot-user-id")


def _compaction_config(settings: Settings) -> HistoryCompactionConfig:
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


def _parse_reset_at(raw: str | None) -> datetime | None:
    if raw is None or not raw.strip():
        return None
    parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _redact_state(state: ConversationHistoryState) -> dict[str, Any]:
    return {
        "id": state.id,
        "bot_user_id": state.bot_user_id,
        "scope_type": state.scope_type,
        "private_peer_user_id": state.private_peer_user_id,
        "group_id": state.group_id,
        "reset_at": None if state.reset_at is None else state.reset_at.isoformat(),
        "last_seen_event_id": state.last_seen_event_id,
        "active_frontier_end_event_id": state.active_frontier_end_event_id,
        "pending_event_count": state.pending_event_count,
        "pending_character_count": state.pending_character_count,
        "revision": state.revision,
    }


def _redact_summary(summary: ConversationHistorySummary) -> dict[str, Any]:
    return {
        "id": summary.id,
        "state_id": summary.state_id,
        "level": summary.level,
        "status": summary.status.value,
        "start_event_id": summary.start_event_id,
        "end_event_id": summary.end_event_id,
        "mode": summary.mode.value,
        "trust": summary.trust.value,
        "summarizer_version": summary.summarizer_version,
        "source_fingerprint": summary.source_fingerprint,
        "replaced_by_summary_id": summary.replaced_by_summary_id,
        "member_count": len(summary.members),
        "member_event_ids": tuple(
            item.source_event_id for item in summary.members if item.source_event_id is not None
        ),
        "member_summary_ids": tuple(
            item.source_summary_id for item in summary.members if item.source_summary_id is not None
        ),
        "rendered_characters": len(summary.rendered_text),
    }


def _redact_job(job: ConversationHistoryJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "state_id": job.state_id,
        "job_kind": job.job_kind.value,
        "status": job.status.value,
        "attempts": job.attempts,
        "outcome": None if job.outcome is None else job.outcome.value,
        "result_summary_id": job.result_summary_id,
        "lease_owner": job.lease_owner,
        "lease_until": None if job.lease_until is None else job.lease_until.isoformat(),
        "error_category": job.error_category,
        "source_fingerprint": job.source_fingerprint,
        "source_start_id": job.source_start_id,
        "source_end_id": job.source_end_id,
    }
