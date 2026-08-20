"""Administrator-facing state machine for controlled historical memory rebuild."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from typing import Any

from qq_ai_bot.config import Settings
from qq_ai_bot.memory.claim_processor import MemoryClaimProcessor, MemoryProcessingContext
from qq_ai_bot.memory.eligibility import MemoryEventEligibilityPolicy
from qq_ai_bot.memory.enums import (
    MemoryProcessingSource,
    MemoryRebuildCommitStatus,
    MemoryRebuildExpiredClaimPolicy,
    MemoryRebuildReviewStatus,
    MemoryRebuildRunStatus,
    MemoryRebuildThirdPartyMode,
    MemorySourceType,
)
from qq_ai_bot.memory.event_extractor import MemoryEventExtractor
from qq_ai_bot.memory.extraction import (
    EXTRACTION_PROMPT_VERSION,
    EXTRACTION_SCHEMA_VERSION,
    SOURCE_ADAPTATION_VERSION,
    MemoryClaim,
    source_event_fingerprint,
)
from qq_ai_bot.memory.rebuild.metrics import MemoryRebuildMetrics
from qq_ai_bot.memory.rebuild.models import (
    MemoryRebuildReviewEntry,
    MemoryRebuildRun,
    MemoryRebuildSelection,
)
from qq_ai_bot.memory.rebuild.repository import MemoryRebuildRepository
from qq_ai_bot.model_runtime.models import ModelTask
from qq_ai_bot.persistence.repositories import EventLedgerRepository

SUBJECT_RESOLVER_VERSION = "1"
CLAIM_VALIDATOR_VERSION = "2"


def canonical_json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def extraction_fingerprint(settings: Settings, *, model_name: str | None = None) -> str:
    payload = {
        "task": ModelTask.MEMORY_EXTRACTION.value,
        "model": model_name or settings.llm_model,
        "prompt": EXTRACTION_PROMPT_VERSION,
        "schema": EXTRACTION_SCHEMA_VERSION,
        "subject_resolver": SUBJECT_RESOLVER_VERSION,
        "claim_validator": CLAIM_VALIDATOR_VERSION,
        "source_adaptation": SOURCE_ADAPTATION_VERSION,
    }
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


class MemoryRebuildService:
    """All command, tool, and worker entry points share this state machine."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: MemoryRebuildRepository,
        ledger: EventLedgerRepository,
        extractor: MemoryEventExtractor,
        processor: MemoryClaimProcessor,
        eligibility: MemoryEventEligibilityPolicy | None = None,
        metrics: MemoryRebuildMetrics | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.ledger = ledger
        self.extractor = extractor
        self.processor = processor
        self.eligibility = eligibility or MemoryEventEligibilityPolicy()
        self.metrics = metrics or MemoryRebuildMetrics()
        self._active_in_flight_calls = 0
        self._in_flight_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._cancelled_runs: set[str] = set()

    @property
    def active_in_flight_calls(self) -> int:
        return self._active_in_flight_calls

    def _authorize(self, actor_user_id: str) -> None:
        if actor_user_id not in self.settings.superusers:
            raise PermissionError("memory rebuild requires the current real superuser")

    def _available(self) -> None:
        if not self.settings.memory_rebuild_enabled:
            raise RuntimeError("MEMORY_REBUILD_ENABLED is false")

    async def plan(
        self, selection: MemoryRebuildSelection, *, actor_user_id: str
    ) -> MemoryRebuildRun:
        self._authorize(actor_user_id)
        self._available()
        configured_max = self.settings.memory_rebuild_max_events_per_run
        if configured_max is not None and (
            selection.maximum_events is None or selection.maximum_events > configured_max
        ):
            raise ValueError(f"selection.maximum_events must be set and <= {configured_max}")
        snapshot = await self.ledger.maximum_event_id()
        statistics = await self.ledger.count_rebuild_candidates(
            selection,
            snapshot_max_event_id=snapshot,
        )
        self.metrics.increment("rebuild_runs_planned")
        self.metrics.increment("rebuild_events_matched", statistics.matched_events)
        self.metrics.increment("rebuild_events_eligible", statistics.eligible_events)
        selection_json = canonical_json(selection)
        return await self.repository.create_run(
            selection=selection,
            selection_json=selection_json,
            selection_hash=hashlib.sha256(selection_json.encode()).hexdigest(),
            snapshot_max_event_id=snapshot,
            fingerprint=extraction_fingerprint(
                self.settings,
                model_name=self.extractor.model_name,
            ),
            statistics=statistics,
            actor_user_id=actor_user_id,
        )

    async def list(self, *, actor_user_id: str) -> tuple[MemoryRebuildRun, ...]:
        self._authorize(actor_user_id)
        return await self.repository.list_runs()

    async def status(self, run_id: str, *, actor_user_id: str) -> dict[str, Any]:
        self._authorize(actor_user_id)
        run = await self._require(run_id)
        return {
            "run": run.model_dump(mode="json"),
            "statistics": await self.repository.statistics(run_id),
            "pending_review": await self.repository.pending_review_count(run_id),
            "pending_commit": await self.repository.remaining_commit_count(run_id),
        }

    async def start(self, run_id: str, *, actor_user_id: str) -> MemoryRebuildRun:
        self._authorize(actor_user_id)
        self._available()
        if await self.repository.executing_count():
            raise RuntimeError("another memory rebuild run is executing")
        changed = await self.repository.transition(
            run_id,
            expected={MemoryRebuildRunStatus.PLANNED},
            status=MemoryRebuildRunStatus.EXTRACTING,
        )
        if not changed:
            raise ValueError("run is not in planned state")
        self.metrics.increment("rebuild_runs_started")
        return await self._require(run_id)

    async def pause(self, run_id: str, *, actor_user_id: str) -> MemoryRebuildRun:
        self._authorize(actor_user_id)
        run = await self._require(run_id)
        target = {
            MemoryRebuildRunStatus.EXTRACTING: MemoryRebuildRunStatus.EXTRACTION_PAUSED,
            MemoryRebuildRunStatus.COMMITTING: MemoryRebuildRunStatus.COMMIT_PAUSED,
        }.get(run.status)
        if target is None:
            raise ValueError("run is not executing")
        if not await self.repository.transition(run_id, expected={run.status}, status=target):
            raise RuntimeError("memory rebuild state changed concurrently")
        return await self._require(run_id)

    async def resume(self, run_id: str, *, actor_user_id: str) -> MemoryRebuildRun:
        self._authorize(actor_user_id)
        self._available()
        if await self.repository.executing_count():
            raise RuntimeError("another memory rebuild run is executing")
        run = await self._require(run_id)
        target = {
            MemoryRebuildRunStatus.EXTRACTION_PAUSED: MemoryRebuildRunStatus.EXTRACTING,
            MemoryRebuildRunStatus.COMMIT_PAUSED: MemoryRebuildRunStatus.COMMITTING,
        }.get(run.status)
        if target is None:
            raise ValueError("run is not paused")
        if target is MemoryRebuildRunStatus.EXTRACTING and (
            run.extraction_fingerprint
            != extraction_fingerprint(
                self.settings,
                model_name=self.extractor.model_name,
            )
        ):
            raise ValueError("extraction_fingerprint_changed; create a new run")
        if not await self.repository.transition(run_id, expected={run.status}, status=target):
            raise RuntimeError("another memory rebuild run is executing")
        return await self._require(run_id)

    async def cancel(self, run_id: str, *, actor_user_id: str) -> MemoryRebuildRun:
        self._authorize(actor_user_id)
        run = await self._require(run_id)
        if run.status in {
            MemoryRebuildRunStatus.COMPLETED,
            MemoryRebuildRunStatus.CANCELLED,
        }:
            return run
        changed = await self.repository.transition(
            run_id,
            expected={run.status},
            status=MemoryRebuildRunStatus.CANCELLED,
        )
        if not changed:
            raise RuntimeError("memory rebuild state changed concurrently")
        self._cancelled_runs.add(run_id)
        for task in tuple(self._in_flight_tasks.get(run_id, ())):
            task.cancel()
        self.metrics.increment("rebuild_runs_cancelled")
        return await self._require(run_id)

    async def review(
        self,
        run_id: str,
        *,
        actor_user_id: str,
        page: int = 1,
    ) -> tuple[MemoryRebuildReviewEntry, ...]:
        self._authorize(actor_user_id)
        if page <= 0:
            raise ValueError("page must be positive")
        await self._require(run_id)
        size = self.settings.memory_rebuild_review_page_size
        rows = await self.repository.review_rows(run_id, offset=(page - 1) * size, limit=size)
        result: list[MemoryRebuildReviewEntry] = []
        for proposal, event in rows:
            claim = MemoryClaim.model_validate_json(proposal.claim_json)
            result.append(
                MemoryRebuildReviewEntry(
                    proposal_id=proposal.id,
                    event_id=event.id,
                    event_time=event.occurred_at,
                    sender_user_id=event.sender_user_id,
                    group_id=event.group_id,
                    subject_user_id=proposal.subject_user_id,
                    scope_type=proposal.scope_type,
                    operation=proposal.operation,
                    kind=proposal.kind,
                    memory_key=claim.memory_key,
                    content=claim.content,
                    confidence=proposal.confidence,
                    authority=proposal.authority,
                    valid_from=claim.valid_from,
                    valid_until=claim.valid_until,
                    source_excerpt=claim.evidence_quote[
                        : self.settings.memory_rebuild_source_excerpt_characters
                    ],
                    review_status=proposal.review_status,
                )
            )
        return tuple(result)

    async def set_review(
        self,
        run_id: str,
        selector: str,
        *,
        approved: bool,
        actor_user_id: str,
    ) -> int:
        self._authorize(actor_user_id)
        run = await self._require(run_id)
        if run.status is not MemoryRebuildRunStatus.REVIEW:
            raise ValueError("run is not ready for review")
        proposal_ids: tuple[int, ...] | None
        if selector.casefold() == "all":
            proposal_ids = None
        elif selector.lstrip().startswith("{"):
            filters = json.loads(selector)
            if not isinstance(filters, dict):
                raise ValueError("review filter must be an object")
            proposal_ids = await self.repository.proposal_ids_for_filter(run_id, filters)
        else:
            try:
                proposal_ids = tuple(int(item) for item in selector.split(",") if item.strip())
            except ValueError as exc:
                raise ValueError("proposal ids must be comma-separated integers") from exc
            if not proposal_ids:
                raise ValueError("proposal selector is empty")
        changed = await self.repository.set_review(
            run_id,
            proposal_ids=proposal_ids,
            status=(
                MemoryRebuildReviewStatus.APPROVED
                if approved
                else MemoryRebuildReviewStatus.REJECTED
            ),
            actor_user_id=actor_user_id,
        )
        self.metrics.increment(
            "rebuild_proposals_approved" if approved else "rebuild_proposals_rejected",
            changed,
        )
        return changed

    async def commit(self, run_id: str, *, actor_user_id: str) -> MemoryRebuildRun:
        self._authorize(actor_user_id)
        self._available()
        run = await self._require(run_id)
        if run.status is not MemoryRebuildRunStatus.REVIEW:
            raise ValueError("run is not in review")
        if await self.repository.pending_review_count(run_id):
            raise ValueError("all proposals must be approved or rejected before commit")
        if await self.repository.executing_count():
            raise RuntimeError("another memory rebuild run is executing")
        changed = await self.repository.transition(
            run_id,
            expected={MemoryRebuildRunStatus.REVIEW},
            status=MemoryRebuildRunStatus.COMMITTING,
        )
        if not changed:
            raise RuntimeError("another memory rebuild run is executing")
        return await self._require(run_id)

    async def retry(self, run_id: str, *, actor_user_id: str) -> MemoryRebuildRun:
        self._authorize(actor_user_id)
        run = await self._require(run_id)
        if run.status is not MemoryRebuildRunStatus.FAILED:
            raise ValueError("only failed runs can be retried")
        target = await self.repository.reset_failed(run_id)
        changed = await self.repository.transition(
            run_id,
            expected={MemoryRebuildRunStatus.FAILED},
            status=target,
        )
        if not changed:
            raise RuntimeError("memory rebuild state changed concurrently")
        return await self._require(run_id)

    async def purge(self, run_id: str, *, actor_user_id: str) -> bool:
        self._authorize(actor_user_id)
        return await self.repository.purge(run_id)

    async def forget_person(self, user_id: str) -> int:
        return await self.repository.forget_person(user_id)

    async def process_extraction_once(self, run: MemoryRebuildRun) -> int:
        if run.status is not MemoryRebuildRunStatus.EXTRACTING:
            return 0
        if run.extraction_fingerprint != extraction_fingerprint(
            self.settings,
            model_name=self.extractor.model_name,
        ):
            await self.repository.transition(
                run.public_id,
                expected={MemoryRebuildRunStatus.EXTRACTING},
                status=MemoryRebuildRunStatus.EXTRACTION_PAUSED,
                error_category="extraction_fingerprint_changed",
            )
            return 0
        scanned = await self.repository.item_count(run.public_id)
        if run.selection.maximum_events is not None and scanned >= run.selection.maximum_events:
            await self.repository.transition(
                run.public_id,
                expected={MemoryRebuildRunStatus.EXTRACTING},
                status=MemoryRebuildRunStatus.REVIEW,
            )
            return 0
        page_limit = min(
            self.settings.memory_rebuild_scan_batch_size,
            (run.selection.maximum_events - scanned)
            if run.selection.maximum_events is not None
            else self.settings.memory_rebuild_scan_batch_size,
        )
        checkpoint_time, checkpoint_event_id = await self.repository.scan_checkpoint(run.public_id)
        rows = await self.ledger.list_rebuild_candidates(
            run.selection,
            snapshot_max_event_id=run.snapshot_max_event_id,
            after_occurred_at=checkpoint_time,
            after_event_id=checkpoint_event_id,
            limit=page_limit,
        )
        if not rows:
            await self.repository.transition(
                run.public_id,
                expected={MemoryRebuildRunStatus.EXTRACTING},
                status=MemoryRebuildRunStatus.REVIEW,
            )
            return 0
        semaphore = asyncio.Semaphore(self.settings.memory_rebuild_extraction_concurrency)
        results = await asyncio.gather(
            *(self._extract_one(run, source_event, semaphore) for source_event in rows)
        )
        for source_event, state in zip(rows, results, strict=True):
            if state not in {"processed", "complete"}:
                break
            await self.repository.update_scan_checkpoint(run.public_id, source_event)
        return sum(state == "processed" for state in results)

    async def _extract_one(
        self,
        run: MemoryRebuildRun,
        source_event: Any,
        semaphore: asyncio.Semaphore,
    ) -> str:
        async with semaphore:
            current = await self._require(run.public_id)
            if current.status is not MemoryRebuildRunStatus.EXTRACTING:
                return "deferred"
            event = await self._adapt_event(source_event, run.selection.third_party_mode)
            event_hash = source_event_fingerprint(event)
            item_id, acquired, item_status = await self.repository.ensure_item(
                run.public_id,
                event_id=source_event.id,
                source_event_hash=event_hash,
            )
            if not acquired:
                if item_status in {"staged", "no_claims", "skipped", "committed"}:
                    return "complete"
                return "deferred"
            current = await self._require(run.public_id)
            if current.status is not MemoryRebuildRunStatus.EXTRACTING:
                await self.repository.defer_item(item_id, category="run_not_extracting")
                return "deferred"
            try:
                context = await self.ledger.list_scope_before(
                    source_event.scope,
                    before_event_id=source_event.id,
                    limit=self.settings.memory_rebuild_context_event_limit,
                )
                task = asyncio.current_task()
                if task is not None:
                    self._in_flight_tasks.setdefault(run.public_id, set()).add(task)
                self._active_in_flight_calls += 1
                try:
                    extracted = await self.extractor.extract(event, context=context)
                finally:
                    self._active_in_flight_calls -= 1
                    if task is not None:
                        tasks = self._in_flight_tasks.get(run.public_id)
                        if tasks is not None:
                            tasks.discard(task)
                            if not tasks:
                                self._in_flight_tasks.pop(run.public_id, None)
                self.metrics.increment("rebuild_extraction_requests")
                if extracted.input_tokens is not None:
                    self.metrics.increment("rebuild_input_tokens", extracted.input_tokens)
                if extracted.output_tokens is not None:
                    self.metrics.increment("rebuild_output_tokens", extracted.output_tokens)
                self.metrics.increment(
                    "rebuild_latency", max(0, round(extracted.latency_seconds * 1000))
                )
                await self.repository.record_model_usage(
                    run.public_id,
                    extraction_requests=1,
                    input_tokens=extracted.input_tokens,
                    output_tokens=extracted.output_tokens,
                    latency_seconds=extracted.latency_seconds,
                )
                staged: list[tuple[MemoryClaim, Any, str]] = []
                for raw_claim in extracted.output.claims:
                    claim = raw_claim
                    if claim.source_type is not MemorySourceType.EXPLICIT:
                        claim = claim.model_copy(update={"source_type": MemorySourceType.REBUILD})
                    validated = self.processor.validate(
                        claim,
                        event,
                        subject_context=extracted.subject_context,
                    )
                    if validated is None:
                        continue
                    claim_json = canonical_json(claim)
                    claim_hash = hashlib.sha256(f"{event_hash}:{claim_json}".encode()).hexdigest()
                    staged.append((claim, validated, claim_hash))
                await self.repository.stage_claims(
                    run.public_id,
                    item_id=item_id,
                    event_id=source_event.id,
                    claims=tuple(staged),
                )
            except asyncio.CancelledError:
                await self.repository.defer_item(item_id, category="cancelled")
                if run.public_id in self._cancelled_runs:
                    return "deferred"
                raise
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                exhausted = await self.repository.fail_item(
                    item_id,
                    type(exc).__name__,
                    max_attempts=self.settings.memory_rebuild_retry_attempts,
                    retry_initial_seconds=self.settings.memory_rebuild_retry_initial_seconds,
                )
                self.metrics.increment("rebuild_events_failed")
                if exhausted:
                    await self.repository.transition(
                        run.public_id,
                        expected={MemoryRebuildRunStatus.EXTRACTING},
                        status=MemoryRebuildRunStatus.FAILED,
                        error_category=type(exc).__name__,
                    )
                    return "failed"
                return "deferred"
            self.metrics.increment("rebuild_events_scanned")
            self.metrics.increment("rebuild_proposals_staged", len(staged))
            if not staged:
                self.metrics.increment("rebuild_events_no_claims")
            return "processed"

    async def process_commit_once(self, run: MemoryRebuildRun) -> int:
        if run.status is not MemoryRebuildRunStatus.COMMITTING:
            return 0
        rows = await self.repository.next_commit_rows(
            run.public_id,
            limit=self.settings.memory_rebuild_commit_batch_size,
        )
        processed = 0
        for proposal, item, stored_event in rows:
            current = await self._require(run.public_id)
            if current.status is not MemoryRebuildRunStatus.COMMITTING:
                break
            event = await self.repository.get_event(stored_event.id)
            if event is not None:
                event = await self._adapt_event(event, run.selection.third_party_mode)
            if event is None or source_event_fingerprint(event) != item.source_event_hash:
                await self.repository.finish_proposal(
                    proposal.id,
                    status=MemoryRebuildCommitStatus.SKIPPED,
                    fact_id=None,
                    action="noop",
                    reason_code="source_event_changed",
                )
                processed += 1
                continue
            receipt = await self.repository.receipt_status(event.id)
            if receipt in {"done", "pending", "processing"} or (
                receipt == "failed" and not run.selection.include_failed_live_jobs
            ):
                reason = (
                    "already_processed"
                    if receipt == "done"
                    else (
                        "live_job_active"
                        if receipt in {"pending", "processing"}
                        else "failed_live_job_not_selected"
                    )
                )
                await self.repository.finish_proposal(
                    proposal.id,
                    status=MemoryRebuildCommitStatus.SKIPPED,
                    fact_id=None,
                    action="noop",
                    reason_code=reason,
                )
                self.metrics.increment("rebuild_events_skipped_processed")
                processed += 1
                continue
            if not self.eligibility.is_eligible(
                event,
                sender_is_bot=await self.ledger.sender_is_bot(event.sender_user_id),
            ):
                await self.repository.finish_proposal(
                    proposal.id,
                    status=MemoryRebuildCommitStatus.SKIPPED,
                    fact_id=None,
                    action="noop",
                    reason_code="event_ineligible_at_commit",
                )
                processed += 1
                continue
            claim = MemoryClaim.model_validate_json(proposal.claim_json)
            subject_context = await self.extractor.subject_context(event)
            validated = self.processor.validate(
                claim,
                event,
                subject_context=subject_context,
            )
            if (
                validated is None
                or validated.fact.subject_user_id != proposal.subject_user_id
                or validated.fact.group_id != proposal.group_id
                or validated.fact.scope_type.value != proposal.scope_type
            ):
                await self.repository.finish_proposal(
                    proposal.id,
                    status=MemoryRebuildCommitStatus.SKIPPED,
                    fact_id=None,
                    action="noop",
                    reason_code="commit_revalidation_failed",
                )
                processed += 1
                continue
            expired = bool(
                validated.fact.valid_until is not None
                and validated.fact.valid_until <= run.snapshot_created_at
            )
            if expired and (
                run.selection.expired_claim_policy is MemoryRebuildExpiredClaimPolicy.SKIP
            ):
                await self.repository.finish_proposal(
                    proposal.id,
                    status=MemoryRebuildCommitStatus.SKIPPED,
                    fact_id=None,
                    action="noop",
                    reason_code="historical_claim_expired",
                )
                processed += 1
                continue
            try:
                result = await self.processor.process(
                    validated,
                    MemoryProcessingContext(
                        source=MemoryProcessingSource.REBUILD,
                        event=event,
                        rebuild_run_id=run.public_id,
                        proposal_id=proposal.id,
                        preserve_capacity=True,
                        force_expired_invalidated=expired,
                    ),
                )
                if result.model_requests:
                    self.metrics.increment("rebuild_consolidation_requests", result.model_requests)
                    if result.input_tokens is not None:
                        self.metrics.increment("rebuild_input_tokens", result.input_tokens)
                    if result.output_tokens is not None:
                        self.metrics.increment("rebuild_output_tokens", result.output_tokens)
                    self.metrics.increment(
                        "rebuild_latency", max(0, round(result.latency_seconds * 1000))
                    )
                    await self.repository.record_model_usage(
                        run.public_id,
                        consolidation_requests=result.model_requests,
                        input_tokens=result.input_tokens,
                        output_tokens=result.output_tokens,
                        latency_seconds=result.latency_seconds,
                    )
            except asyncio.CancelledError:
                raise
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                exhausted = await self.repository.fail_proposal(
                    proposal.id,
                    type(exc).__name__,
                    max_attempts=self.settings.memory_rebuild_retry_attempts,
                    retry_initial_seconds=self.settings.memory_rebuild_retry_initial_seconds,
                )
                self.metrics.increment("rebuild_proposals_failed")
                if exhausted:
                    await self.repository.transition(
                        run.public_id,
                        expected={MemoryRebuildRunStatus.COMMITTING},
                        status=MemoryRebuildRunStatus.FAILED,
                        error_category=type(exc).__name__,
                    )
                    break
                continue
            await self.repository.finish_proposal(
                proposal.id,
                status=MemoryRebuildCommitStatus.COMMITTED,
                fact_id=result.fact_id,
                action=result.action.value,
                reason_code=result.reason_code,
            )
            self.metrics.increment("rebuild_proposals_committed")
            if result.action.value == "create":
                self.metrics.increment("rebuild_facts_created")
            elif result.action.value == "merge_evidence":
                self.metrics.increment("rebuild_evidence_merged")
            elif result.action.value == "supersede":
                self.metrics.increment("rebuild_facts_superseded")
            elif result.action.value == "contest":
                self.metrics.increment("rebuild_facts_contested")
            elif result.action.value == "invalidate":
                self.metrics.increment("rebuild_facts_invalidated")
            elif result.action.value == "noop":
                self.metrics.increment("rebuild_noops")
            processed += 1
        await self.repository.complete_item_receipts(
            run.public_id,
            include_failed_live_jobs=run.selection.include_failed_live_jobs,
        )
        if not await self.repository.remaining_commit_count(
            run.public_id
        ) and not await self.repository.failed_commit_count(run.public_id):
            await self.repository.transition(
                run.public_id,
                expected={MemoryRebuildRunStatus.COMMITTING},
                status=MemoryRebuildRunStatus.COMPLETED,
            )
            self.metrics.increment("rebuild_runs_completed")
        return processed

    async def _adapt_event(self, event: Any, mode: MemoryRebuildThirdPartyMode) -> Any:
        if mode is MemoryRebuildThirdPartyMode.DISABLED:
            return replace(event, mentioned_user_ids=(), reply_sender_user_id=None)
        return await self.ledger.hydrate_rebuild_subjects(event)

    async def _require(self, run_id: str) -> MemoryRebuildRun:
        run = await self.repository.get_run(run_id)
        if run is None:
            raise ValueError("memory rebuild run not found")
        return run
