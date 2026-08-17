"""Concrete per-turn memory session (R2 C5)."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.domain.conversations import ConversationIdentity
from qq_ai_bot.domain.messages import AttachmentKind, InboundMessage
from qq_ai_bot.memory.attribution import (
    MemoryAttributionJob,
    MemoryAttributionWorker,
    MemoryExposure,
)
from qq_ai_bot.memory.context import MemoryContextService
from qq_ai_bot.memory.enums import MemoryContextMode, MemoryRetrievalMode
from qq_ai_bot.memory.models import MemoryQueryIntent, MemoryRetrievalResult
from qq_ai_bot.memory.mutation.models import MemoryMutationResult
from qq_ai_bot.memory.runtime.capability_view import build_capability_view
from qq_ai_bot.memory.runtime.command_plane import mutation_state_for_result
from qq_ai_bot.memory.runtime.contract import (
    MemoryAvailability,
    MemoryContextPolicy,
    MemoryFinalizationPolicy,
    MemoryTurnContract,
    MemoryWritePolicy,
    MemoryWriteTransition,
)
from qq_ai_bot.memory.runtime.finalizer import (
    MutationFinalizationInput,
    finalize_mutation_text,
    mutation_view_from_tool_result,
)
from qq_ai_bot.memory.runtime.query_plane import MemoryQueryPlane, MemoryReadConsumer
from qq_ai_bot.memory.runtime.resolver import (
    MemoryAccessDecision,
    MemoryAccessReason,
    MemoryStructuredCommand,
    resolve_memory_access,
    resolve_scope_from_scene,
)
from qq_ai_bot.memory.runtime.state import (
    AccessPhase,
    LocatorStatus,
    MemorySessionState,
    MutationState,
    RecallHandle,
)
from qq_ai_bot.runtime.authority import TurnAuthority, TurnSceneFacts
from qq_ai_bot.runtime.contracts import DeliverySummary, MemoryCapabilityView, MemoryReceiptHandle
from qq_ai_bot.runtime.delivery import DeliveryStatus
from qq_ai_bot.runtime.keys import ResolvedMemoryScope
from qq_ai_bot.runtime.origin import TurnOrigin

_TERMINAL_MUTATIONS = frozenset(
    {
        MutationState.COMMITTED,
        MutationState.COMMITTED_AS_CONTESTED,
        MutationState.DEDUPLICATED,
        MutationState.NO_CHANGE,
        MutationState.REJECTED,
    }
)
_MEMORY_WRITE_TOOLS = frozenset({"memory_change"})
_MEMORY_READ_TOOLS = frozenset(
    {
        "get_person_memories",
        "get_group_memories",
        "get_self_memories",
        "get_memory_fact",
        "get_memory_evidence",
    }
)


def scene_from_inbound(
    inbound: InboundMessage, *, image_present: bool | None = None
) -> TurnSceneFacts:
    """Trusted scene facts for the resolver.  Never derived from model text."""

    attachments = (*inbound.attachments, *inbound.reply_attachments)
    images = image_present
    if images is None:
        images = any(item.kind is AttachmentKind.IMAGE for item in attachments)
    return TurnSceneFacts(
        scope_type=inbound.scope_type,
        group_id=inbound.group_id,
        image_present=images,
        mentions_bot=inbound.mentions_bot,
        reply_present=bool(inbound.reply_text or inbound.reply_sender_user_id),
    )


def apply_memory_tool_groups(
    view: MemoryCapabilityView, planner_groups: frozenset[str]
) -> frozenset[str]:
    """Capability view owns first-round Memory scope; Planner scopes cannot add it."""

    stripped = frozenset(
        scope for scope in planner_groups if scope != "memory" and not scope.startswith("memory.")
    )
    if any(namespace.startswith("memory.") for namespace in view.eager_namespaces):
        return frozenset((*stripped, "memory"))
    return stripped


class TurnMemorySession:
    """I/O session that Chat may call.  It never holds ChatService."""

    def __init__(
        self,
        *,
        decision: MemoryAccessDecision,
        scope: ResolvedMemoryScope,
        inbound: InboundMessage,
        identity: ConversationIdentity,
        runtime: RuntimeConfigSnapshot,
        memory_context: MemoryContextService,
        origin: TurnOrigin,
        user_question: str,
        runtime_turn_id: str,
        attribution: MemoryAttributionWorker | None = None,
    ) -> None:
        self._decision = decision
        self._state = MemorySessionState(decision.contract, scope)
        self._inbound = inbound
        self._identity = identity
        self._runtime = runtime
        self._memory_context = memory_context
        self._query = MemoryQueryPlane(memory_context)
        self._origin = origin
        self._user_question = user_question
        self._runtime_turn_id = runtime_turn_id
        self._attribution = attribution
        self._prefetch_token: str | None = None
        self._prefetch_result: MemoryRetrievalResult | None = None
        self._prefetch_intent: MemoryQueryIntent | None = None
        self._staged_fact_ids: tuple[int, ...] = ()
        self._staged_exposures: tuple[MemoryExposure, ...] = ()
        self._pending_tool_exposures: tuple[MemoryExposure, ...] = ()
        self._confirmed_exposures: list[MemoryExposure] = []
        self._prefetch_confirmed = False

    @classmethod
    def open(
        cls,
        *,
        inbound: InboundMessage,
        identity: ConversationIdentity,
        runtime: RuntimeConfigSnapshot,
        memory_context: MemoryContextService,
        origin: TurnOrigin,
        user_question: str,
        authority: TurnAuthority,
        structured_command: MemoryStructuredCommand = MemoryStructuredCommand.NONE,
        image_present: bool | None = None,
        runtime_turn_id: str | None = None,
        attribution: MemoryAttributionWorker | None = None,
        memory_available: bool = True,
    ) -> TurnMemorySession:
        scene = scene_from_inbound(inbound, image_present=image_present)
        decision = resolve_memory_access(
            authority=authority,
            scene=scene,
            structured_command=structured_command,
            memory_available=memory_available,
            retrieval_enabled=runtime.memory.retrieval_enabled,
        )
        return cls(
            decision=decision,
            scope=resolve_scope_from_scene(authority=authority, scene=scene),
            inbound=inbound,
            identity=identity,
            runtime=runtime,
            memory_context=memory_context,
            origin=origin,
            user_question=user_question,
            runtime_turn_id=runtime_turn_id or str(uuid.uuid4()),
            attribution=attribution,
        )

    @property
    def contract(self) -> MemoryTurnContract:
        return self._state.contract

    @property
    def scope(self) -> ResolvedMemoryScope:
        return self._state.scope

    @property
    def reason(self) -> MemoryAccessReason:
        return self._decision.reason

    @property
    def retrieval_degraded(self) -> bool:
        return self._decision.retrieval_degraded

    @property
    def prefetch_intent(self) -> MemoryQueryIntent | None:
        return self._prefetch_intent

    @property
    def prefetch_result(self) -> MemoryRetrievalResult | None:
        return self._prefetch_result

    @property
    def staged_exposures(self) -> tuple[MemoryExposure, ...]:
        return self._staged_exposures

    @property
    def mutation_terminal(self) -> bool:
        return self._state.mutation_state in _TERMINAL_MUTATIONS

    @property
    def exclusive_write(self) -> bool:
        return self._state.contract.write_policy is MemoryWritePolicy.EXCLUSIVE

    @property
    def locator_open(self) -> bool:
        return self._state.locator_status is LocatorStatus.OPEN

    @property
    def receipt_gated(self) -> bool:
        return self._state.contract.finalization_policy is MemoryFinalizationPolicy.RECEIPT_GATED

    async def prefetch(self) -> MemoryRetrievalResult | None:
        if self._state.contract.availability is MemoryAvailability.FORBIDDEN:
            return None
        if self._state.contract.context_policy is MemoryContextPolicy.NONE:
            return None
        self._state.start_prefetch()
        intent = MemoryQueryIntent(
            mode=MemoryContextMode.HYBRID,
            purpose=self._state.contract.default_purpose,
        )
        result = await self._memory_context.retrieve_for_turn(
            inbound=self._inbound,
            content=self._user_question,
            runtime=self._runtime,
            memory_mode=MemoryContextMode.HYBRID,
            memory_intent=intent,
        )
        self._prefetch_token = str(uuid.uuid4())
        self._prefetch_result = result
        self._prefetch_intent = intent
        self._state.complete_prefetch()
        return result

    def capability_view(self) -> MemoryCapabilityView:
        return build_capability_view(
            self._state.contract,
            transition_revision=self._state.transition_revision,
        )

    def stage_prompt_selection(
        self,
        fact_ids: tuple[int, ...],
        exposures: tuple[MemoryExposure, ...],
    ) -> None:
        """Record which prefetch facts actually entered the composed prompt."""

        self._staged_fact_ids = fact_ids
        self._staged_exposures = exposures

    async def confirm_prompt_exposure(self, token: str | None = None) -> MemoryReceiptHandle | None:
        del token
        handle: MemoryReceiptHandle | None = None
        if (
            not self._prefetch_confirmed
            and self._prefetch_result is not None
            and self._prefetch_intent is not None
            and self._staged_fact_ids
        ):
            recall = await self._query.publish_exposure(
                MemoryReadConsumer.AUTOMATIC_CONTEXT,
                conversation_key=self._identity.key,
                trigger_message_id=self._inbound.message_id,
                origin=self._origin.value,
                intent=self._prefetch_intent,
                result=self._prefetch_result,
                injected_fact_ids=self._staged_fact_ids,
                runtime=self._runtime,
            )
            if recall is not None:
                self._state.record_recall(
                    RecallHandle(
                        runtime_turn_id=self._runtime_turn_id,
                        receipt_turn_id=recall.turn_id,
                        purpose=self._prefetch_intent.purpose,
                        injected_fact_ids=self._staged_fact_ids,
                    )
                )
                handle = MemoryReceiptHandle(
                    receipt_turn_id=recall.turn_id,
                    injected_fact_ids=self._staged_fact_ids,
                )
            self._confirmed_exposures.extend(self._staged_exposures)
            self._prefetch_confirmed = True
        if self._pending_tool_exposures:
            self._confirmed_exposures.extend(self._pending_tool_exposures)
            self._pending_tool_exposures = ()
        return handle

    async def observe_tool_result(self, capability_id: str, result_json: str) -> None:
        if capability_id in _MEMORY_WRITE_TOOLS:
            self._observe_write(result_json)
            return
        if capability_id in _MEMORY_READ_TOOLS:
            self._observe_read(result_json)

    def request_exclusive_write(self) -> None:
        if self._state.contract.write_transition is MemoryWriteTransition.REQUESTABLE:
            self._state.enter_exclusive_write()

    def finalize_text(self) -> str | None:
        if not self.receipt_gated:
            return None
        view = self._state.last_mutation_view
        if view is None:
            return finalize_mutation_text(MutationFinalizationInput(attempted=False))
        return finalize_mutation_text(view)

    async def on_delivery_confirmed(self, summary: DeliverySummary) -> None:
        if self._state.closed:
            return
        if summary.status in {DeliveryStatus.CANCELLED, DeliveryStatus.FAILED}:
            self._state.skip_attribution()
            return
        if (
            self._attribution is None
            or not self._runtime.memory.usage_attribution_enabled
            or self._origin not in {TurnOrigin.USER_MESSAGE, TurnOrigin.AUTONOMOUS_GROUP}
            or not summary.delivered_text.strip()
            or not self._confirmed_exposures
        ):
            self._state.skip_attribution()
            return
        self._state.freeze_exposures()
        exposures = tuple(self._confirmed_exposures)
        handles = self._state.recall_handles()
        if not handles and self._prefetch_intent is not None:
            self._enqueue_job(self._runtime_turn_id, self._prefetch_intent, exposures, summary)
        for handle in handles:
            matched = tuple(item for item in exposures if item.fact_id in handle.injected_fact_ids)
            if not matched:
                continue
            self._enqueue_job(
                handle.receipt_turn_id,
                MemoryQueryIntent(purpose=handle.purpose),
                matched,
                summary,
            )
        self._state.queue_attribution()

    async def close(self) -> None:
        self._state.close()

    def _observe_write(self, result_json: str) -> None:
        if self._state.contract.write_transition is MemoryWriteTransition.REQUESTABLE:
            self._state.enter_exclusive_write()
        if self._state.access_phase is AccessPhase.LOCATOR_READ_DONE:
            self._state.return_to_exclusive_write()
        decoded = _decode_json(result_json)
        view = mutation_view_from_tool_result(decoded, attempted=True)
        self._state.remember_mutation_view(view)
        if self._state.access_phase is not AccessPhase.MUTATION_EXCLUSIVE:
            return
        self._state.mark_mutation_attempted()
        fake_result = _result_from_tool_json(decoded)
        outcome = mutation_state_for_result(fake_result)
        data = decoded.get("data")
        payload = data if isinstance(data, dict) else {}
        receipt_id = payload.get("mutation_id")
        if outcome in {MutationState.AMBIGUOUS, MutationState.NOT_FOUND}:
            if self._state.locator_status is not LocatorStatus.UNUSED:
                self._state.resolve_mutation(MutationState.REJECTED)
                return
            self._state.resolve_mutation(outcome)
            self._state.open_locator_read()
            return
        self._state.resolve_mutation(
            outcome,
            receipt_id=str(receipt_id) if receipt_id else None,
        )

    def _observe_read(self, result_json: str) -> None:
        decoded = _decode_json(result_json)
        data = decoded.get("data")
        if not isinstance(data, dict):
            return
        registry_payload = data.get("memories") or data.get("memory") or data
        from qq_ai_bot.memory.attribution import MemoryExposureRegistry

        registry = MemoryExposureRegistry()
        fact_ids = registry.register_tool_payload(registry_payload)
        snapshot = registry.snapshot()
        if snapshot:
            self._pending_tool_exposures = (*self._pending_tool_exposures, *snapshot)
        if fact_ids and self._state.access_phase is AccessPhase.LOCATOR_READ_ENABLED:
            self._state.complete_locator_read()
        del fact_ids

    def _enqueue_job(
        self,
        turn_id: str,
        intent: MemoryQueryIntent,
        exposures: tuple[MemoryExposure, ...],
        summary: DeliverySummary,
    ) -> None:
        if self._attribution is None or not turn_id:
            return
        self._attribution.enqueue(
            MemoryAttributionJob(
                turn_id=turn_id,
                user_id=self._inbound.sender.user_id,
                group_id=self._inbound.group_id,
                user_question=self._user_question,
                final_response=summary.delivered_text,
                intent=intent,
                exposures=exposures,
                runtime=self._runtime,
                enqueued_at=datetime.now(UTC),
            )
        )


def _decode_json(raw: str) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _result_from_tool_json(decoded: dict[str, object]) -> MemoryMutationResult:
    from qq_ai_bot.memory.mutation.models import (
        MemoryMutationAppliedOperation,
        MemoryMutationOperation,
        MemoryMutationOutcome,
    )

    data = decoded.get("data")
    payload = data if isinstance(data, dict) else {}
    applied = str(payload.get("applied_operation") or "noop")
    outcome = str(payload.get("outcome") or "rejected")
    try:
        applied_op = MemoryMutationAppliedOperation(applied)
    except ValueError:
        applied_op = MemoryMutationAppliedOperation.NOOP
    try:
        outcome_op = MemoryMutationOutcome(outcome)
    except ValueError:
        outcome_op = MemoryMutationOutcome.REJECTED
    return MemoryMutationResult(
        ok=bool(decoded.get("ok")),
        mutation_id=str(payload.get("mutation_id") or "") or None,
        requested_operation=MemoryMutationOperation.CREATE,
        applied_operation=applied_op,
        outcome=outcome_op,
        reason_code=str(
            payload.get("reason_code") or decoded.get("error") or decoded.get("error_code") or ""
        ),
    )


def empty_retrieval() -> MemoryRetrievalResult:
    return MemoryRetrievalResult(
        blocks=(),
        hits=(),
        candidate_count=0,
        selected_count=0,
        query_hash=hashlib.sha256(b"").hexdigest(),
        mode=MemoryRetrievalMode.RELEVANT,
        semantic_status="session_skipped",
    )
