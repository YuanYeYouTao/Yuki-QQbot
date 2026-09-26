"""Shared preparation and execution boundary for composed Yuki turns."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, replace

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.conversation.projections import (
    ProjectionCapacityError,
    ProjectionConflict,
    PromptProjectionRepository,
)
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, InboundMessage
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
from qq_ai_bot.llm.openai_responses import (
    OpenAICompatibleResponsesProvider,
    OpenAIResponsesProvider,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.services.agent_runner import (
    AgentRunner,
    AgentRunResult,
    AgentRuntime,
    AgentToolBackend,
)
from qq_ai_bot.services.context_assembler import AssembledContext
from qq_ai_bot.services.history_projection import prepare_history
from qq_ai_bot.services.prompt_composer import PromptComposer, PromptComposition
from qq_ai_bot.services.turn_transcript import dispatch_request
from qq_ai_bot.vision.models import VisualObservation


class MainAgentTurnService:
    """Capture turn state before compilation; never refresh a submitted input."""

    def __init__(
        self, composer: PromptComposer, runner: AgentRunner, database: Database | None = None
    ) -> None:
        self._composer = composer
        self._runner = runner
        self._projections = (
            PromptProjectionRepository(
                database,
                max_context_characters=composer._settings.max_context_characters,
                reclaim=True,
            )
            if database is not None
            else None
        )

    async def compose(
        self,
        *,
        inbound: InboundMessage | None,
        context: AssembledContext,
        runtime: RuntimeConfigSnapshot,
        visual_observation: VisualObservation | None,
        visual_failure: bool,
        scope_type: ScopeType | None = None,
        include_plugin_context: bool = True,
        memory_exclusive_write: bool = False,
    ) -> PromptComposition:
        contract = self._runner.main_contract
        state = await asyncio.to_thread(contract.state.snapshot) if contract else None
        composition = self._composer.compose(
            inbound=inbound,
            context=context,
            runtime=runtime,
            visual_observation=visual_observation,
            visual_failure=visual_failure,
            scope_type=scope_type,
            include_plugin_context=include_plugin_context,
            short_state=state,
            memory_exclusive_write=memory_exclusive_write,
        )
        if (
            self._projections is None
            or contract is None
            or (context.current_event_id is None and not context.projection_scope)
            or context.read_version is None
            or context.read_version.conversation_id is None
        ):
            return composition
        await contract.definitions()
        version = context.read_version
        # Separate per-actor selected memory views. Actorless wakeups cannot inherit
        # the private dynamic context assembled for a preceding human turn.
        view_key = _hash(
            [
                "main-history-v1",
                version.conversation_id,
                context.projection_scope,
                inbound.sender.user_id if inbound is not None else "actorless",
                include_plugin_context,
                memory_exclusive_write,
            ]
        )
        if context.current_message.images:
            # Inline image bytes are deliberately ephemeral. Retire the old
            # representation before dispatch instead of silently resuming it on
            # the next text turn as if this image request had never happened.
            repository = self._projections
            retired = False

            async def retire_image_projection() -> None:
                nonlocal retired
                if not retired:
                    await repository.invalidate_view(view_key, reason="protocol_changed")
                    retired = True

            return replace(composition, commit_projection=retire_image_projection)
        profile_revision = getattr(self._runner._models, "profile_revision", None)
        contract_revision = _hash(
            [
                composition.metrics.stable_prefix_hash,
                contract.revision,
                self._runner._models.model_name(self._runner._task),
                self._runner._models.protocol(self._runner._task).value,
                profile_revision(self._runner._task) if callable(profile_revision) else "legacy",
                asdict(runtime.llm),
                asdict(runtime.web),
            ]
        )
        prepared = await prepare_history(
            self._projections,
            context,
            view_key=view_key,
            context_key=_hash([version.generation, context.rollup_text]),
            contract_revision=contract_revision,
            max_history_characters=max(
                0,
                self._composer._settings.max_context_characters
                - len(composition.messages[-1].content or ""),
            ),
        )
        composition = self._composer.compose(
            inbound=inbound,
            context=prepared.context,
            runtime=runtime,
            visual_observation=visual_observation,
            visual_failure=visual_failure,
            scope_type=scope_type,
            include_plugin_context=include_plugin_context,
            short_state=state,
            memory_exclusive_write=memory_exclusive_write,
        )
        fragments = prepared.fragments.append_current(
            context.current_event_id, composition.messages[-1]
        )
        representation_retired = False

        async def commit_projection() -> None:
            nonlocal representation_retired
            sequence = dispatch_request()
            if sequence is None or representation_retired:
                return
            if sequence.messages[: len(composition.messages)] != composition.messages:
                await prepared.repository.invalidate_view(view_key, reason="protocol_changed")
                representation_retired = True
                return
            try:
                submitted = fragments.append_protocol(
                    sequence.messages[len(composition.messages) :]
                )
                if sequence.continuation is not None:
                    if sequence.continuation.protocol != "responses":
                        # Signed native reasoning stays in the private Work journal.
                        # A new conversation turn establishes its own projection boundary.
                        raise ProjectionConflict("opaque native checkpoint requires a boundary")
                    provider = {
                        "deepseek": DeepSeekResponsesProvider,
                        "openai": OpenAIResponsesProvider,
                        "openai_compatible": OpenAICompatibleResponsesProvider,
                    }.get(sequence.continuation.provider)
                    if provider is None:
                        raise ProjectionConflict("unknown Responses replay provider")
                    continuation = provider._request_continuation(
                        ChatRequest(
                            messages=(),
                            model="",
                            continuation=sequence.continuation,
                            continuation_items=sequence.items,
                        )
                    )
                    if continuation is None:
                        raise ProjectionConflict("missing Responses replay")
                    submitted = submitted.append_responses(continuation)
            except ProjectionConflict:
                # Hidden reasoning and opaque continuation stay turn-local. The
                # next turn must not claim this discarded sequence's epoch.
                await prepared.repository.invalidate_view(view_key, reason="protocol_changed")
                representation_retired = True
                return
            try:
                await prepared.commit(submitted)
            except ProjectionCapacityError:
                await prepared.repository.invalidate_view(view_key, reason="capacity")
                representation_retired = True

        return replace(composition, commit_projection=commit_projection)

    async def run(
        self,
        messages: tuple[ChatMessage, ...],
        runtime: AgentRuntime,
        backend: AgentToolBackend | None,
    ) -> AgentRunResult:
        # The compiler places the current task after history. Capture that exact
        # message before the separate work-status input is appended below.
        if runtime.compaction_brief is None and messages:
            if messages[-1].role != "user":
                raise ValueError("main_agent_current_task_message_required")
            runtime = replace(runtime, compaction_brief=messages[-1])
        control = runtime.work_control or current_work_control.get()
        if (
            control is None
            and self._composer._settings.runtime_work_enabled
            and self._projections is not None
            and runtime.canonical_conversation_id
        ):
            from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
            from qq_ai_bot.runtime.work_activation import activate_work
            from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository

            database = self._projections.database
            async with database.sessions() as session:
                conversation = await session.get(
                    CanonicalConversationModel, runtime.canonical_conversation_id
                )
                if conversation is None:
                    raise WorkConflict("work_conversation_unavailable")
                generation = conversation.generation
            if not runtime.execution_id:
                raise WorkConflict("invocation_execution_id_required")
            boundary = invocation_boundary(runtime)

            async def validate() -> None:
                if runtime.before_model_request is not None:
                    await runtime.before_model_request()

            repository = WorkRepository(database)
            previous = await repository.by_source(f"invocation:{boundary}")
            if previous is not None:
                await validate()
                prior_source = json.loads(previous["source_json"])
                requested_source = runtime.invocation_source or {}
                if prior_source.get("owner") == "plugin_invocation" and (
                    prior_source.get("approval_revision")
                    != requested_source.get("approval_revision")
                    or prior_source.get("plugin_id") != requested_source.get("plugin_id")
                ):
                    raise WorkConflict("plugin_work_authority_changed")
                from sqlalchemy import select

                from qq_ai_bot.runtime.work_recovery_schema import recovery

                async with database.sessions() as session:
                    wait_until = await session.scalar(
                        select(recovery.c.not_before).where(recovery.c.work_id == previous["id"])
                    )
                import time

                if (
                    previous["generation"] != generation
                    or previous["state"]
                    in {"suspended", "failed", "cancelled", "waiting_user", "waiting_external"}
                    or (wait_until and wait_until > time.time())
                ):
                    return AgentRunResult(
                        text="",
                        tool_calls_used=0,
                        model_requests=0,
                        web_was_used=False,
                        suppress_delivery=True,
                        work_state="cancelled"
                        if previous["generation"] != generation
                        else previous["state"],
                        work_id=previous["id"],
                    )
            if (
                previous is not None
                and previous["generation"] == generation
                and previous["state"] == "completed"
            ):
                await validate()
                saved = json.loads(previous["checkpoint_json"])
                if saved.get("archived"):
                    return AgentRunResult(
                        text="",
                        tool_calls_used=0,
                        model_requests=0,
                        web_was_used=False,
                        work_id=previous["id"],
                        work_state="archived",
                        suppress_delivery=True,
                    )
                if isinstance(saved.get("sync_result"), str):
                    return AgentRunResult(
                        text=saved["sync_result"],
                        suppress_delivery=bool(saved.get("sync_suppress_delivery", False)),
                        tool_calls_used=0,
                        model_requests=0,
                        web_was_used=False,
                        work_state="completed",
                        work_id=previous["id"],
                    )

            # Synchronous plugin/automation calls return to their owning step.
            async with activate_work(
                repository,
                runtime.canonical_conversation_id,
                generation,
                f"invocation:{boundary}",
                {
                    **(runtime.invocation_source or {}),
                    "origin": runtime.origin.value,
                    "actor_user_id": runtime.actor_user_id,
                    "execution_boundary": boundary,
                    "parent_execution_id": runtime.execution_id,
                    "delivery_contract": "return_to_caller",
                },
                validate,
            ) as bounded:
                if bounded.current is None and runtime.invocation_goal:
                    await bounded.execute(
                        "task_control",
                        {
                            "action": "accept",
                            "goal": runtime.invocation_goal,
                            "output_kind": "answer",
                            "deliver_artifacts": False,
                        },
                        "host-invocation-admission",
                    )
                result = await self.run(messages, replace(runtime, work_control=bounded), backend)
                bounded.final_delivery = bounded.ending == "completed"
                if bounded.current is not None:
                    if result.suppress_delivery:
                        stored = json.loads(bounded.current["checkpoint_json"])
                        if isinstance(stored.get("sync_result"), str):
                            result = replace(
                                result, text=stored["sync_result"], suppress_delivery=False
                            )
                    if bounded.ending == "completed" and bounded.final_delivery:
                        await repository.checkpoint(
                            bounded.lease,
                            bounded.current["id"],
                            {
                                "sync_result": result.text,
                                "sync_suppress_delivery": result.suppress_delivery,
                            },
                        )
                        if bounded.session is not None:
                            await bounded.session.save("delivered")
                    result = replace(
                        result,
                        work_state=bounded.ending or "running",
                        work_id=bounded.current["id"],
                    )
            return replace(result, outcome=bounded.outcome)
        if control is not None:
            control.current_message = messages[-1] if messages else None
            active = control.current
            messages = (
                *messages,
                ChatMessage(
                    role="user",
                    content=(
                        "[运行状态资料，不增加任何权限] "
                        + json.dumps(
                            {
                                "work_id": active["id"] if active else None,
                                "goal": active["goal"] if active else None,
                                "state": active["state"] if active else "no_active_work",
                                "available_work": await control.available_work()
                                if active is None
                                else [],
                            },
                            ensure_ascii=False,
                        )
                    ),
                ),
            )
        return await self._runner.run(
            messages,
            replace(
                runtime,
                dynamic_context_prepared=True,
                work_control=control,
            ),
            backend,
        )


def invocation_boundary(runtime: AgentRuntime) -> str:
    return _hash(
        [
            runtime.origin.value,
            runtime.canonical_conversation_id,
            runtime.execution_id,
            sorted(runtime.allowed_capabilities),
        ]
    )


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
