"""Live Memory V2 worker composed from the shared extraction and claim pipelines."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.config import Settings
from qq_ai_bot.memory.candidates import MemoryConflictCandidateResolver
from qq_ai_bot.memory.claim_candidates import MemoryClaimCandidateRepository
from qq_ai_bot.memory.claim_processor import MemoryClaimProcessor, MemoryProcessingContext
from qq_ai_bot.memory.classifier import MemoryRelationClassifier
from qq_ai_bot.memory.enums import MemoryProcessingSource, MemoryRebuildJobOutcome
from qq_ai_bot.memory.event_extractor import MemoryEventExtractor
from qq_ai_bot.memory.extraction import MemoryClaim
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.models import MemoryJob
from qq_ai_bot.memory.mutation.service import MemoryMutationService
from qq_ai_bot.memory.repository import MemoryJobRepository
from qq_ai_bot.memory.resolution import MemoryResolutionPolicy
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.subjects import SubjectResolutionContext
from qq_ai_bot.memory.validation import MemoryClaimValidator
from qq_ai_bot.model_runtime.executor import ModelCompleter, ModelExecutor, require_model_executor
from qq_ai_bot.persistence.people_repository import PeopleRepository
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.services.concurrency import ConcurrencyManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _MemoryJobResult:
    outcome: MemoryRebuildJobOutcome
    extracted: int = 0
    validated: int = 0
    applied: int = 0
    candidates: int = 0
    rejection_reasons: tuple[tuple[str, int], ...] = ()

    @property
    def result_category(self) -> str | None:
        if self.outcome is not MemoryRebuildJobOutcome.ALL_REJECTED:
            return None
        if not self.rejection_reasons:
            return "all_rejected"
        return f"all_rejected:{self.rejection_reasons[0][0]}"


class MemoryWorker:
    """Extract one conversation micro-batch while committing every event independently."""

    def __init__(
        self,
        *,
        settings: Settings,
        jobs: MemoryJobRepository,
        facts: MemoryFactService,
        ledger: EventLedgerRepository,
        people: PeopleRepository | None = None,
        provider: ModelCompleter | None = None,
        model_executor: ModelExecutor | None = None,
        concurrency: ConcurrencyManager,
        validator: MemoryClaimValidator | None = None,
        runtime_config: RuntimeConfigService | None = None,
        candidate_resolver: MemoryConflictCandidateResolver | None = None,
        relation_classifier: MemoryRelationClassifier | None = None,
        resolution_policy: MemoryResolutionPolicy | None = None,
        metrics: MemoryLifecycleMetrics | None = None,
        extractor: MemoryEventExtractor | None = None,
        processor: MemoryClaimProcessor | None = None,
        mutations: MemoryMutationService | None = None,
        claim_candidates: MemoryClaimCandidateRepository | None = None,
    ) -> None:
        self._settings = settings
        self._jobs = jobs
        self._facts = facts
        self._ledger = ledger
        models = require_model_executor(
            model_executor,
            provider=provider,
            model=settings.llm_model or "fake",
        )
        self._concurrency = concurrency
        self.metrics = metrics or MemoryLifecycleMetrics()
        candidates = candidate_resolver or MemoryConflictCandidateResolver(
            facts.repository,
            limit=settings.memory_consolidation_candidate_limit,
        )
        self.extractor = extractor or MemoryEventExtractor(
            models,
            concurrency,
            people=people,
            bot_aliases=settings.bot_aliases,
            bot_display_name=settings.bot_display_name,
            timezone=settings.default_timezone,
        )
        self.processor = processor or MemoryClaimProcessor(
            settings=settings,
            facts=facts,
            candidate_resolver=candidates,
            relation_classifier=relation_classifier
            or MemoryRelationClassifier(
                model_executor=models,
                concurrency=concurrency,
                max_output_tokens=settings.memory_consolidation_max_output_tokens,
            ),
            resolution_policy=resolution_policy or MemoryResolutionPolicy(),
            validator=validator,
            runtime_config=runtime_config,
            metrics=self.metrics,
        )
        self.mutations = mutations or MemoryMutationService(
            settings=settings,
            facts=facts,
            processor=self.processor,
            ledger=ledger,
        )
        self.claim_candidates = claim_candidates or MemoryClaimCandidateRepository(
            facts.repository.database
        )
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._queued_by_conversation: dict[str, tuple[int, int]] = {}

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="memory-v2-worker")

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            await self._task

    async def enqueue(
        self,
        event_id: int,
        conversation_key: str,
        *,
        content_characters: int = 0,
    ) -> bool:
        created = await self._jobs.enqueue(event_id, conversation_key)
        if created:
            count, characters = self._queued_by_conversation.get(conversation_key, (0, 0))
            pending = (count + 1, characters + max(0, content_characters))
            self._queued_by_conversation[conversation_key] = pending
            if (
                pending[0] >= self._settings.memory_batch_trigger_count
                or pending[1] >= self._settings.memory_batch_max_characters
            ):
                self._queued_by_conversation.pop(conversation_key, None)
                self._wake.set()
        return created

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self._settings.memory_batch_seconds
                )
            except TimeoutError:
                pass
            self._wake.clear()
            if not self._stop.is_set():
                try:
                    while await self.process_once():
                        if self._stop.is_set():
                            break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception(
                        "memory_v2_worker_iteration_failed exception_category=%s",
                        type(exc).__name__,
                    )

    async def process_once(self) -> int:
        jobs = await self._jobs.claim_ready_batch(
            limit=min(self._settings.memory_batch_max_events, 12),
            trigger_count=self._settings.memory_batch_trigger_count,
            max_characters=self._settings.memory_batch_max_characters,
            max_wait_seconds=self._settings.memory_batch_max_wait_seconds,
        )
        if not jobs:
            return 0
        try:
            first_event = jobs[0].event
            context = await self._ledger.list_scope_before(
                first_event.scope,
                before_event_id=first_event.id,
                limit=8,
            )
            extracted = await self.extractor.extract_batch(
                tuple(job.event for job in jobs),
                context=context,
                max_output_tokens=self._settings.memory_batch_max_output_tokens,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "memory_v2_batch_failed first_job_id=%d job_count=%d exception_category=%s",
                jobs[0].id,
                len(jobs),
                type(exc).__name__,
            )
            for job in jobs:
                await self._fail_job(job, exc)
            return 0

        known_event_ids = {job.event_id for job in jobs}
        subject_contexts = dict(extracted.subject_contexts)
        claims_by_event: defaultdict[int, list[MemoryClaim]] = defaultdict(list)
        unknown_claims = 0
        for anchored in extracted.output.claims:
            if anchored.source_event_id not in known_event_ids:
                unknown_claims += 1
                self.metrics.increment("claims_rejected_unknown_source_event")
                continue
            claims_by_event[anchored.source_event_id].append(anchored.claim)
        if unknown_claims:
            logger.warning(
                "memory_v2_batch_unknown_source_events first_job_id=%d "
                "job_count=%d rejected_claims=%d",
                jobs[0].id,
                len(jobs),
                unknown_claims,
            )

        completed = 0
        for job in jobs:
            try:
                result = await self._process_claims(
                    job,
                    tuple(claims_by_event.get(job.event_id, ())),
                    subject_context=subject_contexts.get(job.event_id),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "memory_v2_job_failed job_id=%d event_id=%d exception_category=%s",
                    job.id,
                    job.event_id,
                    type(exc).__name__,
                )
                await self._fail_job(job, exc)
                continue
            if result.outcome is MemoryRebuildJobOutcome.ALL_REJECTED:
                logger.warning(
                    "memory_v2_job_all_rejected job_id=%d event_id=%d extracted=%d "
                    "validated=%d applied=%d rejection_reasons=%s",
                    job.id,
                    job.event_id,
                    result.extracted,
                    result.validated,
                    result.applied,
                    dict(result.rejection_reasons),
                )
            try:
                await self._jobs.complete(
                    job.id,
                    outcome=result.outcome,
                    result_category=result.result_category,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "memory_v2_job_completion_failed job_id=%d event_id=%d exception_category=%s",
                    job.id,
                    job.event_id,
                    type(exc).__name__,
                )
                await self._fail_job(job, exc)
                continue
            completed += 1
        return completed

    async def _process_claims(
        self,
        job: MemoryJob,
        claims: tuple[MemoryClaim, ...],
        *,
        subject_context: SubjectResolutionContext | None = None,
    ) -> _MemoryJobResult:
        extracted_count = len(claims)
        if not extracted_count:
            return _MemoryJobResult(MemoryRebuildJobOutcome.NO_CLAIMS)
        applied = 0
        candidates = 0
        validated_count = 0
        rejection_reasons: Counter[str] = Counter()
        for claim in claims:
            self.metrics.increment("claims_extracted")
            validation = self.processor.validate_result(
                claim,
                job.event,
                subject_context=subject_context,
            )
            validated = validation.claim
            staged_candidate_id: int | None = None
            if validated is None:
                if validation.candidate_type is not None:
                    staged = await self.claim_candidates.stage(
                        claim,
                        job.event,
                        candidate_type=validation.candidate_type,
                        subject_context=subject_context,
                    )
                    candidates += 1
                    staged_candidate_id = staged.id
                    self.metrics.increment("claims_candidate")
                    if (
                        staged.ready_for_promotion
                        and validation.reason_code == "low_confidence_candidate"
                    ):
                        promoted = claim.model_copy(
                            update={"confidence": max(0.75, claim.confidence)}
                        )
                        validation = self.processor.validate_result(
                            promoted,
                            job.event,
                            subject_context=subject_context,
                        )
                        validated = validation.claim
                    if validated is None:
                        continue
                else:
                    rejection_reasons[validation.reason_code] += 1
                    self.metrics.increment(f"claims_rejected_{validation.reason_code}")
                    continue
            validated_count += 1
            self.metrics.increment(f"claims_{claim.operation.value}ed")
            if validated.fact.authority.value == "third_party":
                self.metrics.increment("claims_third_party")
            result = await self.mutations.mutate_validated_claim(
                validated,
                MemoryProcessingContext(
                    source=MemoryProcessingSource.LIVE,
                    event=job.event,
                ),
                conversation_key=job.conversation_key,
            )
            if result.ok and (result.new_fact_id is not None or result.old_fact_id is not None):
                applied += 1
                if staged_candidate_id is not None:
                    await self.claim_candidates.set_status(
                        staged_candidate_id,
                        "accepted",
                    )
                continue
            reason_code = result.reason_code or result.outcome.value
            rejection_reasons[reason_code] += 1
            self.metrics.increment(f"claims_rejected_{reason_code}")
        outcome = (
            MemoryRebuildJobOutcome.CLAIMS_APPLIED
            if applied
            else MemoryRebuildJobOutcome.CANDIDATES_STAGED
            if candidates
            else MemoryRebuildJobOutcome.ALL_REJECTED
        )
        return _MemoryJobResult(
            outcome=outcome,
            extracted=extracted_count,
            validated=validated_count,
            applied=applied,
            candidates=candidates,
            rejection_reasons=tuple(
                sorted(rejection_reasons.items(), key=lambda item: (-item[1], item[0]))
            ),
        )

    async def _fail_job(self, job: MemoryJob, exc: Exception) -> None:
        try:
            await self._jobs.fail(job.id, type(exc).__name__)
        except asyncio.CancelledError:
            raise
        except Exception as fail_exc:
            logger.exception(
                "memory_v2_job_failure_record_failed job_id=%d exception_category=%s",
                job.id,
                type(fail_exc).__name__,
            )
