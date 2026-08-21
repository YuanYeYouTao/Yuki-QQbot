"""Person-centric context assembly, bounded Agent loop, sending, and ledger writes."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol, TypeVar, cast

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.admin.permission_catalog import contains_internal_capability_payload
from qq_ai_bot.automation.intent import enforce_creation_claim, is_scheduled_automation_request
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities import (
    AuthorityContext,
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityPolicyContext,
    CapabilityRisk,
    CapabilityTrustSource,
    InProcessToolProvider,
    ToolArtifactWriter,
    ToolExecutionResult,
    ToolInvocationContext,
    ToolKernelMetrics,
    ToolProvider,
    ToolProviderRegistry,
    ToolResultBudgeter,
    UnifiedToolCatalog,
    resolve_mutation_commit,
)
from qq_ai_bot.capabilities.catalog import DescriptorRegistrySnapshot
from qq_ai_bot.capabilities.exposure import NO_LONGER_AUTHORIZED
from qq_ai_bot.capabilities.request import REQUEST_TOOLS_NAME
from qq_ai_bot.capabilities.runtime import (
    CapabilityIndexCache,
    CapabilityQuery,
    CapabilitySearchReport,
    TurnCapabilityRuntime,
)
from qq_ai_bot.capabilities.validation import UNDECLARED_TOOL
from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.cadence import ReplyEffectRepository
from qq_ai_bot.conversation.delivery import ReplyControlState, default_reply_spec
from qq_ai_bot.conversation.reply import ReplyEffect
from qq_ai_bot.conversation.rollup.errors import ConversationCoverageError
from qq_ai_bot.conversation.rollup.repository import (
    ConversationRollupRepository,
    ConversationScopeRepository,
)
from qq_ai_bot.conversation.rollup.service import ConversationRollupService
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import (
    AttachmentKind,
    ChatMessage,
    ChatTool,
    InboundMessage,
    OutboundMedia,
    OutboundMessage,
    OutboundSendReceipt,
    PromptRequestDiagnostics,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.emoji.effects import EmojiReplyEffectService
from qq_ai_bot.emoji.models import (
    EmojiPlacement,
    EmojiPreparationResult,
    EmojiPreparationStatus,
    EmojiReplyMode,
    PendingReplyEffect,
)
from qq_ai_bot.llm.base import LLMEmptyResponseError
from qq_ai_bot.memory.attribution import (
    MemoryAttributionWorker,
    MemoryExposure,
    MemoryExposureRegistry,
)
from qq_ai_bot.memory.context import MemoryContextService
from qq_ai_bot.memory.enums import MemoryContextMode
from qq_ai_bot.memory.fts import SQLiteMemoryFTSIndex
from qq_ai_bot.memory.models import MemoryQueryIntent
from qq_ai_bot.memory.query import MemoryQueryBuilder
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.retrieval import MemoryRetriever
from qq_ai_bot.memory.runtime.contract import MemoryReadPolicy
from qq_ai_bot.memory.runtime.resolver import MemoryStructuredCommand
from qq_ai_bot.memory.runtime.turn_session import (
    TurnMemorySession,
    empty_retrieval,
)
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.targets import MemoryTargetResolver
from qq_ai_bot.model_runtime.executor import ModelCompleter, ModelExecutor, require_model_executor
from qq_ai_bot.model_runtime.models import ModelProtocol, ModelTask
from qq_ai_bot.persistence.repositories import (
    EventLedgerRepository,
    PeopleRepository,
    RelationshipRepository,
    WebSearchSourceRepository,
)
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.runtime.authority import TurnAuthority
from qq_ai_bot.runtime.contracts import DeliverySummary
from qq_ai_bot.runtime.delivery import DeliveryStatus
from qq_ai_bot.runtime.observability import identifier_hash
from qq_ai_bot.runtime.origin import TurnOrigin as RuntimeTurnOrigin
from qq_ai_bot.services.agent_runner import (
    AgentRunner,
    AgentRunResult,
    AgentRuntime,
    AgentToolBackend,
)
from qq_ai_bot.services.agent_tools import AgentToolService, OneBotToolGateway, ToolRuntime
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.context_assembler import ContextAssembler
from qq_ai_bot.services.effect_gate import (
    ConversationEffectGate,
    EffectGateTimeoutError,
    EffectPermitRejectedError,
)
from qq_ai_bot.services.plugin_events import (
    LifecycleEventPublisher,
    publish_notification,
)
from qq_ai_bot.services.policies import replies_to_bot
from qq_ai_bot.services.prompt_composer import PromptComposer
from qq_ai_bot.services.renderer import clean_model_output, split_qq_message
from qq_ai_bot.services.reply_sequence import (
    DeliveryFailureRecovery,
    ReplySequenceManager,
)
from qq_ai_bot.services.reply_target import ReplyTargetControl, ReplyTargetResolver
from qq_ai_bot.services.source_policy import SourceDisplayPolicy
from qq_ai_bot.services.source_renderer import SourceRenderer
from qq_ai_bot.services.turn_coordinator import (
    ConversationTurnCoordinator,
    TurnSupersededError,
    TurnToken,
)
from qq_ai_bot.speech.models import VoiceMode, VoicePreferenceMode
from qq_ai_bot.speech.preference_service import VoicePreferenceService
from qq_ai_bot.speech.reply_effect import (
    PendingVoiceReplyEffect,
    PreparedVoiceReply,
    VoiceReplyEffectService,
)
from qq_ai_bot.time.service import TimeContextService
from qq_ai_bot.vision.models import VisualObservation
from qq_ai_bot.web.models import WebProvider, WebRouteReason
from qq_ai_bot.web.native_sources import recover_native_web_response
from qq_ai_bot.web.router import WebProviderRouter
from yuki_plugin_sdk.events import EventName

logger = logging.getLogger(__name__)

_EffectResult = TypeVar("_EffectResult")

_ARTIFACT_PROVIDER_ID = "artifacts"
_ARTIFACT_READER_NAME = "read_tool_artifact"
_SET_REPLY_TARGET_NAME = "set_reply_target"
_MEMORY_MUTATION_EXECUTION_CONTRACT = (
    "本轮是后端授权的长期记忆变更终端轮次。必须先调用当前唯一暴露的长期记忆写能力，"
    "并严格以真实工具回执为准；不得直接用正文确认、模拟或承诺变更。定位失败时也必须"
    "保留真实失败回执，不得改用管理员能力。本轮不继续处理其他问答。"
)
_SET_REPLY_TARGET_TOOL = ChatTool(
    name=_SET_REPLY_TARGET_NAME,
    description=(
        "控制本轮最终 QQ 引用回复目标。仅在多人混聊或需要明确回应某条较早消息时调用。"
        "event_id 必须来自当前上下文消息行的 #EventRecord.id；省略 event_id 表示取消引用。"
        "该函数只设置本轮回复样式，不发送消息。每轮最多成功设置一次。"
    ),
    parameters={
        "type": "object",
        "properties": {"event_id": {"type": "integer", "minimum": 1}},
        "additionalProperties": False,
    },
)


def _with_memory_mutation_contract(
    messages: tuple[ChatMessage, ...],
    exclusive_write: bool,
) -> tuple[ChatMessage, ...]:
    if not exclusive_write:
        return messages
    if not messages:
        return (ChatMessage(role="system", content=_MEMORY_MUTATION_EXECUTION_CONTRACT),)
    return (
        *messages[:-1],
        ChatMessage(role="system", content=_MEMORY_MUTATION_EXECUTION_CONTRACT),
        messages[-1],
    )


_ADMIN_RETRYABLE_ERRORS = frozenset(
    {
        "invalid_json",
        "invalid_arguments",
        "validation_error",
        "unknown_capability",
        "ValueError",
    }
)


def _core_result_character_budget(runtime: RuntimeConfigSnapshot | None) -> int:
    if runtime is None:
        return 8000
    tooling = runtime.tooling
    if tooling is not None and tooling.result_token_budget is not None:
        return tooling.result_token_budget * 4
    return runtime.agent.tool_result_max_characters


def _fit_artifact_page_result(
    page: dict[str, object],
    *,
    max_characters: int,
) -> ToolExecutionResult:
    """Fit one artifact page into the model result budget without changing its handle."""

    def outcome(candidate: dict[str, object]) -> ToolExecutionResult:
        return ToolExecutionResult(
            ok=True,
            data=candidate,
            provider_id=_ARTIFACT_PROVIDER_ID,
            tool_name=_ARTIFACT_READER_NAME,
        )

    def rendered_size(candidate: dict[str, object]) -> int:
        return len(
            json.dumps(
                outcome(candidate).model_payload(),
                ensure_ascii=False,
                default=str,
            )
        )

    if rendered_size(page) <= max_characters:
        return outcome(page)
    content = page.get("content")
    offset = page.get("offset")
    total = page.get("total_characters")
    if not isinstance(content, str) or not isinstance(offset, int) or not isinstance(total, int):
        return ToolExecutionResult(
            ok=False,
            error_code="artifact_page_budget_exceeded",
            public_message="Artifact 页面无法放入当前工具结果预算",
            provider_id=_ARTIFACT_PROVIDER_ID,
            tool_name=_ARTIFACT_READER_NAME,
        )

    best: dict[str, object] | None = None
    low = 0
    high = len(content)
    while low <= high:
        length = (low + high) // 2
        next_offset = offset + length
        candidate = {
            **page,
            "content": content[:length],
            "next_offset": next_offset if next_offset < total else None,
        }
        if rendered_size(candidate) <= max_characters:
            best = candidate
            low = length + 1
        else:
            high = length - 1
    if best is None or (content and not best.get("content")):
        return ToolExecutionResult(
            ok=False,
            error_code="artifact_page_budget_exceeded",
            public_message="Artifact 页面预算过小，无法返回有效内容",
            provider_id=_ARTIFACT_PROVIDER_ID,
            tool_name=_ARTIFACT_READER_NAME,
        )
    return outcome(best)


class OutboundSender(Protocol):
    """Adapter-provided sender used by the business layer."""

    async def send(self, message: OutboundMessage) -> OutboundSendReceipt:
        """Send one normal message and return proof of platform acceptance."""


class AdminToolService(Protocol):
    """Backend-verified administrator tools used by the single chat Agent."""

    def definitions(self) -> tuple[ChatTool, ...]:
        """Return reviewed administrator tool schemas."""

    def is_mutating_call(self, name: str, arguments_json: str) -> bool:
        """Return whether this exact registered operation changes backend state."""

    async def execute(
        self,
        name: str,
        arguments_json: str,
        runtime: ToolRuntime,
    ) -> str:
        """Execute against authority derived from the current real event."""


class AutomationToolProvider(Protocol):
    """Owner-scoped automation tools available to every real direct user turn."""

    def definitions(self) -> tuple[ChatTool, ...]: ...

    def owns(self, name: str) -> bool: ...

    async def execute(self, name: str, arguments_json: str, runtime: ToolRuntime) -> str: ...


class PluginToolProvider(Protocol):
    """Approved Plugin API tools merged into the existing Yuki Agent loop."""

    def definitions(
        self,
        runtime: ToolRuntime,
        *,
        web_was_used: bool,
    ) -> tuple[ChatTool, ...]: ...

    def owns(self, name: str) -> bool: ...

    def is_mutating(self, name: str) -> bool: ...

    def is_read_only(self, name: str) -> bool: ...

    async def execute(
        self,
        name: str,
        arguments_json: str,
        runtime: ToolRuntime,
        *,
        web_was_used: bool,
    ) -> str: ...


class ToolInvocationRecorder(Protocol):
    async def record_invocation(
        self,
        *,
        conversation_key: str,
        provider_id: str,
        tool_name: str,
        success: bool,
        latency_seconds: float,
        result_size: int,
        artifact_created: bool,
        error_category: str | None,
        trigger_message_id: str,
        bot_user_id: str,
        result_excerpt: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _CompletedAgentRun:
    result: AgentRunResult
    memory_exposures: tuple[MemoryExposure, ...]


class _ChatAgentBackend(AgentToolBackend):
    """Preserve event-bound chat policies behind the shared model tool loop."""

    def __init__(self, service: ChatService, runtime: ToolRuntime) -> None:
        self._service = service
        self._runtime = runtime
        self._memory_session = getattr(runtime, "memory_session", None)
        self._tools_closed = False
        self._web_was_used = False
        self._web_calls_used = 0
        self._capability_was_used = False
        self._search_event_tasks: list[asyncio.Task[None]] = []
        self._admin_retry_constraint: tuple[str, str] | None = None
        self._admin_terminal_failure: dict[str, object] | None = None
        self._completed_admin_mutations: set[tuple[str, str]] = set()
        self._committed_mutation_messages: list[str] = []
        self._mutation_committed = False
        self._automation_persisted = False
        self._batch: list[ToolCall] = []
        self._batch_lock = asyncio.Lock()
        self._catalog: UnifiedToolCatalog | None = None
        self._requestable_catalog: UnifiedToolCatalog | None = None
        self._provider_registry: ToolProviderRegistry | None = None
        self._capability_runtime: TurnCapabilityRuntime | None = None
        self._requested_tool_names: set[str] = set()
        self._callable_tool_names: set[str] = set()
        self._tool_turn_recorded = False
        self._request_tools_called = False
        self._first_real_tool_recorded = False
        self._native_web_fallback = runtime.native_web_fallback
        self._batch_rejected: str = ""

    def enable_native_web_fallback(self) -> None:
        """Allow Tavily tools only after the Runner verifies a fallback condition."""

        if self._native_web_fallback:
            return
        self._native_web_fallback = True
        # Catalog providers depend on this flag; rebuild before the first request.
        self._capability_runtime = None
        self._catalog = None
        self._requestable_catalog = None

    async def prepare(self, runtime: AgentRuntime | None = None) -> None:
        """Hydrate lazy MCP metadata before the first model request."""

        del runtime
        if self._capability_runtime is not None:
            return
        capability_runtime = self._install_capability_runtime()
        await capability_runtime.prepare_initial_exposure(self._capability_query())
        self._catalog = capability_runtime.authorized_catalog
        self._requestable_catalog = self._catalog

    def _memory(self) -> Any:
        return self._memory_session

    def _exclusive_write(self) -> bool:
        session = self._memory()
        return session is not None and session.exclusive_write

    def _eager_memory_read(self) -> bool:
        session = self._memory()
        if session is None:
            return False
        return session.contract.read_policy in {
            MemoryReadPolicy.EAGER,
            MemoryReadPolicy.LOCATOR_ONLY,
        }

    def _locator_open(self) -> bool:
        session = self._memory()
        return session is not None and session.locator_open

    async def confirm_memory_prompt_exposure(self) -> None:
        session = self._memory()
        if session is not None:
            await session.confirm_prompt_exposure()

    def terminal_memory_reply(self) -> str | None:
        session = self._memory()
        if session is None or not session.mutation_terminal:
            return None
        text = session.finalize_text()
        return text if isinstance(text, str) else None

    def mark_native_web_used(self) -> None:
        """Apply post-Web isolation before same-response local calls execute."""

        self._web_was_used = True

    def consume_provider_chain_restart(self) -> bool:
        """Drop Responses continuation after a no-side-effect schema rebuild."""

        runtime = self._capability_runtime
        if runtime is None:
            return False
        return runtime.consume_provider_chain_restart()

    def _prompt_tools_closed(self) -> bool:
        if self._tools_closed:
            return True
        return self._runtime.tools_closed and not self._runtime.align_conversation_prefix_tools

    def _prefix_policy_origin(self) -> TurnOrigin:
        if self._runtime.align_conversation_prefix_tools:
            return TurnOrigin.USER_MESSAGE
        return self._runtime.origin

    def definitions(self, runtime: AgentRuntime, *, web_was_used: bool) -> tuple[ChatTool, ...]:
        del runtime
        self._web_was_used = self._web_was_used or web_was_used
        response_controls = () if self._exclusive_write() else self._response_control_definitions()
        if self._prompt_tools_closed():
            self._callable_tool_names = {tool.name for tool in response_controls}
            self._log_tool_exposure(response_controls, reason="business_tools_closed")
            return response_controls
        capability_runtime = self._ensure_capability_runtime()
        session = self._memory()
        if session is not None:
            capability_runtime.sync_memory_view(session.capability_view())
        definitions = capability_runtime.definitions()
        if response_controls:
            definitions = (
                *(tool for tool in definitions if tool.name != _SET_REPLY_TARGET_NAME),
                *response_controls,
            )
        if self._admin_retry_constraint is not None:
            definitions = tuple(
                tool for tool in definitions if tool.name == self._admin_retry_constraint[0]
            )
        definitions = tuple(sorted(definitions, key=lambda tool: tool.name))
        self._callable_tool_names = set(capability_runtime.callable_capability_ids()) | {
            tool.name for tool in response_controls
        }
        self._callable_tool_names.update(tool.name for tool in definitions)
        if not self._tool_turn_recorded and definitions:
            self._service._tool_metrics.record_tool_enabled_turn()
            self._tool_turn_recorded = True
        self._log_tool_exposure(definitions, reason="ready")
        return definitions

    def _ensure_capability_runtime(self) -> TurnCapabilityRuntime:
        if self._capability_runtime is not None:
            self._catalog = self._capability_runtime.authorized_catalog
            self._requestable_catalog = self._catalog
            return self._capability_runtime
        capability_runtime = self._install_capability_runtime()
        capability_runtime.initial_exposure(self._capability_query())
        self._catalog = capability_runtime.authorized_catalog
        self._requestable_catalog = self._catalog
        return capability_runtime

    def _capability_query(self) -> CapabilityQuery:
        return CapabilityQuery(
            text=self._runtime.selection_query,
            origin=RuntimeTurnOrigin(self._runtime.origin.value),
            limit=8,
            reply_excerpt=(self._runtime.inbound.reply_text or "")[:500],
            priority_capability_ids=self._host_priority_capability_ids(),
        )

    def _refresh_capability_registry(
        self,
    ) -> tuple[DescriptorRegistrySnapshot, Any]:
        request_runtime = self._request_runtime()
        self._provider_registry = self._service._build_tool_registry(
            request_runtime,
            web_was_used=self._web_was_used,
        )
        catalog = self._provider_registry.catalog(request_runtime)
        snapshot = DescriptorRegistrySnapshot(catalog)
        index = self._service._capability_index.index_for(snapshot)
        return snapshot, index

    def _install_capability_runtime(self) -> TurnCapabilityRuntime:
        snapshot, index = self._refresh_capability_registry()
        session = self._memory()
        memory_view = session.capability_view() if session is not None else None
        reply_target = self._runtime.reply_target_control
        align_prefix = self._runtime.align_conversation_prefix_tools
        policy_context = CapabilityPolicyContext(
            authority=AuthorityContext(
                actor_user_id=self._runtime.actor_user_id,
                is_superuser=self._runtime.actor_is_superuser,
            ),
            origin=self._prefix_policy_origin(),
            contains_images=bool(
                self._runtime.inbound.attachments or self._runtime.inbound.reply_attachments
            ),
            web_was_used=self._web_was_used,
            tools_closed=False if align_prefix else self._runtime.tools_closed,
            read_only=False if align_prefix else self._runtime.read_only,
            memory_view=memory_view,
            artifact_available=self._service._tool_artifacts is not None,
            reply_target_available=bool(
                reply_target is not None and reply_target.visible_event_ids
            ),
        )
        mcp = self._runtime.runtime_config.mcp if self._runtime.runtime_config else None
        tooling = self._runtime.runtime_config.tooling if self._runtime.runtime_config else None
        request_runtime = self._request_runtime()

        async def ensure_metadata(server_id: str) -> None:
            provider = self._provider_registry.provider("mcp") if self._provider_registry else None
            prepare = getattr(provider, "ensure_server_metadata", None)
            if callable(prepare):
                await prepare(server_id, request_runtime)

        authority = TurnAuthority(
            actor_user_id=self._runtime.actor_user_id or "unknown",
            bot_user_id=self._runtime.inbound.bot_user_id or "bot",
            origin=RuntimeTurnOrigin(self._runtime.origin.value),
            permission_ceiling=frozenset({"superuser"} if self._runtime.actor_is_superuser else ()),
            delegated_authority=None,
            authority_revision=1,
        )
        self._capability_runtime = TurnCapabilityRuntime(
            registry=snapshot,
            index=index,
            authority=authority,
            scene=self._scene_facts(),
            memory_view=memory_view,
            policy_context=policy_context,
            append_only=self._service._responses_append_only(),
            schema_token_budget=tooling.schema_token_budget if tooling is not None else None,
            mcp_schema_token_budget=mcp.schema_token_budget if mcp is not None else None,
            mcp_tool_limit=mcp.selected_tool_limit if mcp is not None else None,
            first_round_hard_cap=(
                tooling.first_round_hard_cap if tooling is not None else None
            ),
            ensure_metadata=ensure_metadata,
            refresh_registry=self._refresh_capability_registry,
            on_searched=self._publish_capability_searched,
        )
        return self._capability_runtime

    def _publish_capability_searched(self, report: CapabilitySearchReport) -> None:
        publisher = getattr(self._service, "_event_publisher", None)
        if publisher is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._search_event_tasks.append(
            loop.create_task(
                publish_notification(
                    publisher,
                    EventName.CAPABILITY_SEARCHED,
                    {
                        "origin": report.origin,
                        "hit_count": report.hit_count,
                        "latency_ms": report.latency_ms,
                        "capability_ids": list(report.capability_ids),
                    },
                )
            )
        )

    def _host_priority_capability_ids(self) -> tuple[str, ...]:
        """Pin tools implied by deployment config, not the current message or origin."""

        names: list[str] = []
        tooling = (
            self._runtime.runtime_config.tooling
            if self._runtime.runtime_config is not None
            else None
        )
        if tooling is not None:
            names.extend(getattr(tooling, "first_round_pin_ids", ()))
        elif getattr(self._service, "_settings", None) is not None:
            names.extend(self._service._settings.tooling_first_round_pin_ids)
        web_route = self._runtime.web_route
        if self._native_web_fallback or (
            web_route is not None
            and web_route.provider is WebProvider.TAVILY
            and web_route.reason is WebRouteReason.MODE
        ):
            # Deployment-wide Tavily, or this turn already fell back. Do not pin
            # from a URL, user override, or domain rule: those change tools[]
            # per message and punch the DeepSeek prefix from token 0.
            names.extend(("web_search", "read_webpage"))
        return tuple(dict.fromkeys(names))

    def _scene_facts(self) -> Any:
        from qq_ai_bot.domain.conversations import ScopeType as DomainScopeType
        from qq_ai_bot.runtime.authority import TurnSceneFacts

        inbound = self._runtime.inbound
        scope = inbound.scope_type
        if scope is DomainScopeType.GROUP:
            return TurnSceneFacts(
                scope_type=scope,
                group_id=inbound.group_id,
                image_present=bool(inbound.attachments or inbound.reply_attachments),
                mentions_bot=inbound.mentions_bot,
                replies_to_bot=replies_to_bot(inbound),
                reply_present=bool(inbound.reply_text or inbound.reply_sender_user_id),
            )
        return TurnSceneFacts(
            scope_type=scope,
            group_id=None,
            image_present=bool(inbound.attachments or inbound.reply_attachments),
            mentions_bot=inbound.mentions_bot,
            replies_to_bot=replies_to_bot(inbound),
            reply_present=bool(inbound.reply_text or inbound.reply_sender_user_id),
        )

    def _log_tool_exposure(
        self,
        definitions: tuple[ChatTool, ...],
        *,
        reason: str,
    ) -> None:
        """Log bounded capability metadata without message text or tool arguments."""

        exposed_tools = ",".join(sorted(tool.name for tool in definitions)) or "none"
        logger.info(
            "agent_tools_exposed conversation_hash=%s origin=%s "
            "tools=%s exposed_count=%d requestable_count=%d reason=%s",
            identifier_hash(self._runtime.conversation_key) or "missing",
            self._runtime.origin.value,
            exposed_tools,
            len(definitions),
            len(self._requestable_catalog.entries) if self._requestable_catalog is not None else 0,
            reason,
        )

    def begin_batch(self, calls: tuple[ToolCall, ...], runtime: AgentRuntime) -> None:
        del runtime
        self._batch_rejected = ""
        names = {call.function.name for call in calls}
        if "decline_reply" in names and (len(calls) != 1 or self.has_prior_reply_effects()):
            self._batch_rejected = "decline_reply_batch_rejected"
            self._batch = []
            return
        self._batch = list(calls)

    def has_prior_reply_effects(self) -> bool:
        control = self._runtime.reply_control
        return bool(
            control is not None
            and (control.had_effect or control.layout_applied or control.declined)
        )

    def declined_reply(self) -> bool:
        control = self._runtime.reply_control
        return bool(control is not None and control.declined)

    def did_use_web(self) -> bool:
        """Expose a provider-metadata-derived effect to the shared Agent loop."""

        return self._web_was_used

    async def execute(self, name: str, arguments_json: str, runtime: AgentRuntime) -> str:
        if self._batch_rejected:
            return json.dumps(
                {
                    "ok": False,
                    "error": self._batch_rejected,
                    "detail": "decline_reply 必须是尚未产生效果时的单独调用。",
                },
                ensure_ascii=False,
            )
        async with self._batch_lock:
            if not self._batch:
                return json.dumps(
                    {"ok": False, "error": "tool_batch_state_missing"}, ensure_ascii=False
                )
            call_index = next(
                (
                    index
                    for index, item in enumerate(self._batch)
                    if item.function.name == name and item.function.arguments == arguments_json
                ),
                None,
            )
            if call_index is None:
                return json.dumps(
                    {"ok": False, "error": "tool_batch_state_mismatch"}, ensure_ascii=False
                )
            call = self._batch.pop(call_index)
        if name == _SET_REPLY_TARGET_NAME and not self._runtime.align_conversation_prefix_tools:
            return self._set_reply_target(arguments_json)
        if self._runtime.tools_closed:
            return json.dumps(
                {
                    "ok": False,
                    "error": "tools_closed",
                    "detail": "本轮只声明会话前缀工具 schema，不允许真实调用。",
                },
                ensure_ascii=False,
            )
        if self._tools_closed:
            return json.dumps(
                {
                    "ok": False,
                    "error": (
                        "mutation_already_committed" if self._mutation_committed else "tools_closed"
                    ),
                    "detail": (
                        "本轮已有修改成功提交，后续工具调用已关闭。"
                        if self._mutation_committed
                        else "本轮工具调用已因之前的终止错误关闭。"
                    ),
                },
                ensure_ascii=False,
            )
        if name == REQUEST_TOOLS_NAME:
            if self._exclusive_write() and not self._locator_open():
                return json.dumps(
                    {
                        "ok": False,
                        "error": "capability_not_loaded",
                        "detail": "记忆写入定位尚未失败，本轮不能提前加载其他能力。",
                    },
                    ensure_ascii=False,
                )
            return await self._request_tools(arguments_json)
        capability_runtime = self._capability_runtime
        if capability_runtime is not None:
            ok, error = capability_runtime.validate_call(name, arguments_json)
            if not ok and error != UNDECLARED_TOOL:
                return json.dumps(
                    {"ok": False, "error": error or NO_LONGER_AUTHORIZED},
                    ensure_ascii=False,
                )
        if name not in self._callable_tool_names:
            requestable = (
                self._requestable_catalog.by_model_name(name)
                if self._requestable_catalog is not None
                else None
            )
            if requestable is None:
                return json.dumps(
                    {"ok": False, "error": "unknown_capability"},
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "ok": False,
                    "error": "capability_not_loaded",
                    "detail": (
                        "该工具未在本轮正式加载；请先调用 request_tools 按能力描述请求，"
                        "再使用返回的真实工具名"
                    ),
                },
                ensure_ascii=False,
            )
        entry = self._catalog.by_model_name(name) if self._catalog is not None else None
        descriptor = entry.descriptor if entry is not None else None
        if descriptor is None or descriptor.binding is None:
            return json.dumps({"ok": False, "error": "unknown_capability"})
        binding = descriptor.binding
        if not self._first_real_tool_recorded:
            first_round_hit = not self._request_tools_called
            self._service._tool_metrics.record_first_round_tool_hit(hit=first_round_hit)
            logger.info(
                "agent_first_round_tool_hit conversation_hash=%s hit=%s tool=%s",
                identifier_hash(self._runtime.conversation_key) or "missing",
                first_round_hit,
                descriptor.model_name,
            )
            self._first_real_tool_recorded = True
        effective_descriptor = self._effective_descriptor(call, descriptor)
        is_web_tool = effective_descriptor.namespace_id.startswith("web.")
        is_memory_read_tool = (
            effective_descriptor.namespace_id.startswith("memory.")
            and effective_descriptor.effect is CapabilityEffect.READ_STATE
        )
        is_memory_write_tool = effective_descriptor.namespace_id == "memory.state.write"
        if is_memory_read_tool and not self._exclusive_write() and not self._eager_memory_read():
            self._service._tool_metrics.record_automatic_memory_read_tool_call(
                locator_fallback=self._locator_open()
            )
        config = self._runtime.runtime_config
        assert config is not None
        mutation_identity = self._mutation_identity(call)
        mutation_committed = False
        if mutation_identity is not None and mutation_identity in self._completed_admin_mutations:
            result = json.dumps(
                {
                    "ok": False,
                    "error": "duplicate_mutation",
                    "detail": "本轮已经成功执行过相同修改，不再重复执行。",
                },
                ensure_ascii=False,
            )
        elif is_web_tool and self._web_calls_used >= config.web.max_calls_per_turn:
            result = json.dumps(
                {
                    "ok": False,
                    "error": "web_tool_limit_exceeded",
                    "detail": (
                        f"本轮最多执行 {config.web.max_calls_per_turn} 次联网工具，"
                        "请根据已有结果回答。"
                    ),
                },
                ensure_ascii=False,
            )
        elif self._admin_retry_constraint is not None and not self._matches_retry(
            call,
            self._admin_retry_constraint,
        ):
            result = json.dumps(
                {
                    "ok": False,
                    "error": "retry_scope_violation",
                    "detail": "参数修正只能重试刚才失败的同一个工具和操作。",
                },
                ensure_ascii=False,
            )
            self._tools_closed = True
        else:
            execution_runtime = self._request_runtime()
            if mutation_identity is not None and execution_runtime.turn_token is not None:
                await self._service._turn_coordinator.mark_mutation_started(
                    execution_runtime.turn_token
                )
            try:
                parsed = json.loads(arguments_json)
            except json.JSONDecodeError:
                parsed = None
            if not isinstance(parsed, dict):
                result = json.dumps(
                    {"ok": False, "error": "invalid_json"},
                    ensure_ascii=False,
                )
            else:
                started = time.perf_counter()
                try:

                    async def invoke_binding() -> ToolExecutionResult:
                        return await binding.invoke(
                            {str(key): value for key, value in parsed.items()},
                            ToolInvocationContext(
                                runtime=execution_runtime,
                                call_id=call.id,
                                conversation_key=execution_runtime.conversation_key,
                                actor_user_id=execution_runtime.actor_user_id,
                                trigger_message_id=execution_runtime.trigger_message_id,
                                provider_metadata={
                                    "contains_images": bool(
                                        self._runtime.inbound.attachments
                                        or self._runtime.inbound.reply_attachments
                                    ),
                                    "web_was_used": self._web_was_used,
                                },
                            ),
                        )

                    outcome = await self._service._run_effect(
                        execution_runtime.turn_snapshot,
                        invoke_binding,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    outcome = ToolExecutionResult(
                        ok=False,
                        error_code=type(exc).__name__,
                        public_message="工具执行失败",
                        retryable=False,
                        provider_id=descriptor.provider_id,
                        tool_name=descriptor.provider_tool_name or descriptor.model_name,
                    )
                mutation_committed = self._is_mutating_call(call) and resolve_mutation_commit(
                    outcome,
                    effective_descriptor,
                )
                should_finalize_after_commit = bool(
                    mutation_committed
                    and (
                        (is_memory_write_tool and self._exclusive_write())
                        or outcome.finalize_after_commit is True
                        or effective_descriptor.finalize_after_commit
                    )
                )
                outcome = replace(
                    outcome,
                    mutation_committed=mutation_committed,
                    finalize_after_commit=True if should_finalize_after_commit else None,
                )
                tooling = config.tooling
                mcp = config.mcp
                is_mcp = effective_descriptor.trust_source is CapabilityTrustSource.MCP
                result_tokens = (
                    mcp.result_token_budget
                    if is_mcp and mcp is not None and mcp.result_token_budget is not None
                    else (tooling.result_token_budget if tooling is not None else None)
                )
                result_budget = (
                    result_tokens * 4
                    if result_tokens is not None
                    else config.agent.tool_result_max_characters
                )
                item_limit = (
                    mcp.result_item_limit
                    if is_mcp and mcp is not None and mcp.result_item_limit is not None
                    else (tooling.result_item_limit if tooling is not None else None)
                )
                artifact_store = (
                    self._service._tool_artifacts
                    if tooling is not None and tooling.result_artifact_enabled
                    else None
                )
                retention_seconds = (
                    mcp.artifact_retention_seconds
                    if is_mcp and mcp is not None
                    else (
                        tooling.result_artifact_retention_seconds if tooling is not None else None
                    )
                )
                budgeted = await ToolResultBudgeter(
                    max_characters=result_budget,
                    item_limit=item_limit,
                    artifacts=artifact_store,
                    artifact_retention_seconds=retention_seconds,
                ).render(outcome)
                result = budgeted.text
                self._service._tool_metrics.record_invocation(
                    descriptor.provider_id,
                    descriptor.provider_tool_name or descriptor.model_name,
                    outcome.ok,
                )
                if self._service._tool_invocations is not None:
                    await self._service._tool_invocations.record_invocation(
                        conversation_key=execution_runtime.conversation_key,
                        provider_id=descriptor.provider_id,
                        tool_name=descriptor.provider_tool_name or descriptor.model_name,
                        success=outcome.ok,
                        latency_seconds=time.perf_counter() - started,
                        result_size=len(result.encode("utf-8")),
                        artifact_created=budgeted.artifact_id is not None,
                        error_category=outcome.error_code,
                        trigger_message_id=execution_runtime.trigger_message_id,
                        bot_user_id=execution_runtime.inbound.bot_user_id,
                        result_excerpt=result,
                    )
            if contains_internal_capability_payload(result):
                self._capability_was_used = True
            if is_web_tool:
                self._web_calls_used += 1
                self._web_was_used = True
        decoded = self._service._decode_tool_result(result)
        if self._capability_runtime is not None and self.is_side_effecting(
            name, arguments_json, runtime
        ):
            self._capability_runtime.mark_side_effect()
        session = self._memory()
        if session is not None and (is_memory_write_tool or is_memory_read_tool):
            await session.observe_tool_result(name, result)
        if is_memory_write_tool and session is not None and session.exclusive_write:
            self._service._record_memory_mutation_turn_outcome(
                self._memory_mutation_outcome(decoded)
            )
        if (
            descriptor.provider_id == "automation"
            and descriptor.provider_tool_name == "automation_create"
        ):
            data = decoded.get("data")
            self._automation_persisted = bool(
                decoded.get("ok")
                and isinstance(data, dict)
                and data.get("confirmation") == "persisted"
                and isinstance(data.get("automation_id"), int)
            )
        if self._is_mutating_call(call):
            if bool(decoded.get("ok")):
                self._admin_retry_constraint = None
                self._admin_terminal_failure = None
                if mutation_identity is not None and mutation_committed:
                    self._completed_admin_mutations.add(mutation_identity)
                    self._remember_committed_mutation(decoded)
                    self._mutation_committed = True
                    if self._exclusive_write():
                        self._tools_closed = True
            elif (decoded.get("error") or decoded.get("error_code")) == "duplicate_mutation":
                # A prior identical call already committed in this turn. Keep
                # the successful result available so the model can summarize it.
                self._admin_retry_constraint = None
                self._admin_terminal_failure = None
            elif (decoded.get("error") or decoded.get("error_code")) in {
                "memory_candidate_ambiguous",
                "memory_candidate_not_found",
            }:
                self._admin_terminal_failure = None
                self._admin_retry_constraint = None
                pass
            elif bool(decoded.get("retryable")):
                self._admin_terminal_failure = None
                self._admin_retry_constraint = self._retry_identity(call)
                if self._admin_retry_constraint is None:
                    self._tools_closed = True
            elif (decoded.get("error") or decoded.get("error_code")) in _ADMIN_RETRYABLE_ERRORS:
                self._admin_terminal_failure = decoded
                self._admin_retry_constraint = self._retry_identity(call)
                if self._admin_retry_constraint is None:
                    self._tools_closed = True
            else:
                self._admin_terminal_failure = decoded
                self._tools_closed = True
        return result

    def finalize(self, content: str, runtime: AgentRuntime) -> str:
        memory_text = self._memory_mutation_final_text()
        if memory_text is not None:
            return memory_text
        if self._admin_terminal_failure is not None:
            return self._service._admin_failure_text(self._admin_terminal_failure)
        if self._capability_was_used and contains_internal_capability_payload(content):
            return "我已经在本轮内部读取了权限范围，但没有生成合适的简短回答。请再问一次。"
        return enforce_creation_claim(
            content,
            scheduled_intent=self._runtime.scheduled_automation_intent,
            persisted=self._automation_persisted,
        )

    def has_visible_effects(self) -> bool:
        """Return whether queued effects can produce a reply without model text."""

        effects = self._runtime.reply_effects or ()
        if any(isinstance(effect, PendingVoiceReplyEffect) for effect in effects):
            # Voice synthesis needs the final model text as its spoken content.
            # Treating the queued voice request itself as visible content can make
            # a successful tool call end in a completely silent turn.
            return False
        return any(
            isinstance(effect, PendingReplyEffect)
            and (
                effect.mode in {EmojiReplyMode.PREFERRED, EmojiReplyMode.EMOJI_ONLY}
                or effect.placement is EmojiPlacement.ONLY
            )
            for effect in effects
        )

    def post_commit_recovery_text(self) -> str | None:
        """Return a deterministic reply when model finalization fails after a commit."""

        memory_text = self._memory_mutation_final_text()
        if memory_text is not None:
            return memory_text
        if not self._committed_mutation_messages:
            return None
        return "\n".join(self._committed_mutation_messages)

    def _remember_committed_mutation(self, result: dict[str, object]) -> None:
        message = str(result.get("public_message") or "").strip()
        if not message:
            receipt = json.dumps(
                result,
                ensure_ascii=False,
                default=str,
                separators=(",", ":"),
            )
            message = f"操作已经提交。以下是工具返回的结果：\n{receipt}"
        if message not in self._committed_mutation_messages:
            self._committed_mutation_messages.append(message)

    def exhausted(self, runtime: AgentRuntime) -> str:
        memory_text = self._memory_mutation_final_text()
        if memory_text is not None:
            return memory_text
        if self._admin_terminal_failure is not None:
            return self._service._admin_failure_text(self._admin_terminal_failure)
        return "这次操作的工具调用次数过多，已停止继续执行。请把请求拆小后再试。"

    @staticmethod
    def _memory_mutation_outcome(result: dict[str, object]) -> str:
        data = result.get("data")
        payload = data if isinstance(data, dict) else {}
        applied = str(payload.get("applied_operation") or "")
        outcome = str(payload.get("outcome") or "")
        error = str(result.get("error") or result.get("error_code") or "")
        if applied == "noop" or outcome in {"no_change", "deduplicated"}:
            return "noop"
        if error == "memory_candidate_ambiguous":
            return "ambiguous"
        if error == "memory_candidate_not_found":
            return "not_found"
        if result.get("mutation_committed") is True:
            return "committed"
        return "rejected"

    def _memory_mutation_final_text(self) -> str | None:
        session = self._memory()
        if session is None:
            return None
        text = session.finalize_text()
        if not isinstance(text, str):
            return None
        if session.receipt_gated and not session.mutation_terminal:
            self._service._record_memory_mutation_turn_outcome("not_attempted")
        return text

    def _is_mutating_call(self, call: ToolCall) -> bool:
        entry = (
            self._catalog.by_model_name(call.function.name) if self._catalog is not None else None
        )
        descriptor = entry.descriptor if entry is not None else None
        admin_tools = getattr(self._service, "_admin_tools", None)
        if descriptor is not None and descriptor.provider_id == "admin" and admin_tools is not None:
            return cast(AdminToolService, admin_tools).is_mutating_call(
                call.function.name,
                call.function.arguments,
            )
        return bool(
            descriptor is not None
            and self._effective_descriptor(call, descriptor).risk is not CapabilityRisk.READ
        )

    @staticmethod
    def _effective_descriptor(
        call: ToolCall,
        descriptor: CapabilityDescriptor,
    ) -> CapabilityDescriptor:
        """Use a gateway target descriptor for risk/commit coordination."""

        resolver = getattr(descriptor.binding, "target_descriptor", None)
        if not callable(resolver):
            return descriptor
        try:
            arguments = json.loads(call.function.arguments)
        except json.JSONDecodeError:
            return descriptor
        if not isinstance(arguments, dict):
            return descriptor
        target = resolver({str(key): value for key, value in arguments.items()})
        return target or descriptor

    def parallel_safe(self, name: str, runtime: AgentRuntime) -> bool:
        del runtime
        if name in {REQUEST_TOOLS_NAME, _SET_REPLY_TARGET_NAME}:
            return False
        entry = self._catalog.by_model_name(name) if self._catalog is not None else None
        return bool(entry is not None and entry.descriptor.parallel_safe)

    def is_side_effecting(
        self,
        name: str,
        arguments_json: str,
        runtime: AgentRuntime,
    ) -> bool:
        """Classify cache invalidation through the same descriptor used for execution."""

        del runtime
        if name in {REQUEST_TOOLS_NAME, _SET_REPLY_TARGET_NAME}:
            return False
        entry = self._catalog.by_model_name(name) if self._catalog is not None else None
        descriptor = entry.descriptor if entry is not None else None
        if descriptor is None:
            return False
        call = ToolCall(
            id="cache-classification",
            function=ToolFunction(name=name, arguments=arguments_json),
        )
        return self._effective_descriptor(call, descriptor).risk is not CapabilityRisk.READ

    def counts_toward_limit(self, name: str, runtime: AgentRuntime) -> bool:
        """Keep local response controls and Artifact reads outside the business budget."""

        del runtime
        return name not in {
            _SET_REPLY_TARGET_NAME,
            _ARTIFACT_READER_NAME,
        }

    def _mutation_identity(self, call: ToolCall) -> tuple[str, str] | None:
        if not self._is_mutating_call(call):
            return None
        try:
            arguments = json.loads(call.function.arguments)
        except json.JSONDecodeError:
            normalized = call.function.arguments.strip()
        else:
            normalized = json.dumps(
                arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        return call.function.name, normalized

    def _response_control_definitions(self) -> tuple[ChatTool, ...]:
        if self._prefix_policy_origin() not in {
            TurnOrigin.USER_MESSAGE,
            TurnOrigin.AUTONOMOUS_GROUP,
        }:
            return ()
        if self._runtime.reply_target_control is not None:
            return (_SET_REPLY_TARGET_TOOL,)
        return ()

    def _set_reply_target(self, arguments_json: str) -> str:
        control = self._runtime.reply_target_control
        if control is None:
            return json.dumps(
                {"ok": False, "error": "reply_control_unavailable"},
                ensure_ascii=False,
            )
        try:
            arguments = json.loads(arguments_json)
        except json.JSONDecodeError:
            arguments = None
        if not isinstance(arguments, dict) or set(arguments) - {"event_id"}:
            return json.dumps(
                {"ok": False, "error": "invalid_arguments"},
                ensure_ascii=False,
            )
        event_id = arguments.get("event_id")
        if event_id is not None and (not isinstance(event_id, int) or isinstance(event_id, bool)):
            return json.dumps(
                {"ok": False, "error": "invalid_event_id"},
                ensure_ascii=False,
            )
        accepted, outcome = control.apply(event_id)
        logger.info(
            "agent_reply_target_control accepted=%s outcome=%s event_id=%s",
            accepted,
            outcome,
            event_id if event_id is not None else "none",
        )
        return json.dumps(
            {
                "ok": accepted,
                "outcome": outcome,
                "reply_to_event_id": event_id if accepted else None,
            },
            ensure_ascii=False,
        )

    async def _request_tools(self, arguments_json: str) -> str:
        self._request_tools_called = True
        self._service._tool_metrics.record_request_tools()
        if not self._exclusive_write() and not self._eager_memory_read():
            self._service._tool_metrics.record_automatic_memory_request_tools()
        try:
            arguments = json.loads(arguments_json)
        except json.JSONDecodeError:
            arguments = None
        if not isinstance(arguments, dict):
            return json.dumps(
                {"ok": False, "error": "invalid_arguments", "detail": "参数必须是对象"},
                ensure_ascii=False,
            )
        query = arguments.get("query")
        max_results = arguments.get("max_results", 4)
        if (
            not isinstance(query, str)
            or len(query.strip()) < 2
            or not isinstance(max_results, int)
            or isinstance(max_results, bool)
            or not 1 <= max_results <= 8
        ):
            return json.dumps(
                {
                    "ok": False,
                    "error": "invalid_arguments",
                    "detail": "query 至少 2 个字符，max_results 必须为 1 到 8",
                },
                ensure_ascii=False,
            )
        capability_runtime = self._ensure_capability_runtime()
        payload = await capability_runtime.request_tools(
            CapabilityQuery(
                text=query.strip(),
                origin=self._runtime.origin,
                limit=max_results,
                affinity_namespace_ids=capability_runtime.affinity_namespace_ids,
            )
        )
        loaded = payload.get("data") if payload.get("ok") else None
        loaded_tools = ()
        if isinstance(loaded, dict):
            raw_loaded = loaded.get("loaded_tools")
            if isinstance(raw_loaded, list):
                loaded_tools = tuple(raw_loaded)
        if not payload.get("ok") or not loaded_tools:
            self._service._tool_metrics.record_request_tools_zero_result()
            logger.info(
                "agent_request_tools_result conversation_hash=%s loaded_count=0",
                identifier_hash(self._runtime.conversation_key) or "missing",
            )
            return json.dumps(payload, ensure_ascii=False)
        loaded_names = {
            str(item.get("name"))
            for item in loaded_tools
            if isinstance(item, dict) and item.get("name")
        }
        loaded_write = capability_runtime.requested_exclusive_write()
        session = self._memory()
        if loaded_write and session is not None:
            session.request_exclusive_write()
        if (
            not self._exclusive_write()
            and not self._eager_memory_read()
            and any(
                isinstance(item, dict)
                and str(item.get("namespace", "")).startswith("memory.")
                and str(item.get("namespace")) != "memory.state.write"
                for item in loaded_tools
            )
        ):
            self._service._tool_metrics.record_automatic_memory_read_tools_loaded()
        logger.info(
            "agent_request_tools_result conversation_hash=%s loaded_count=%d",
            identifier_hash(self._runtime.conversation_key) or "missing",
            len(loaded_names),
        )
        self._requested_tool_names.update(loaded_names)
        self._catalog = capability_runtime.authorized_catalog
        self._requestable_catalog = self._catalog
        self._callable_tool_names = set(capability_runtime.callable_capability_ids())
        return json.dumps(payload, ensure_ascii=False)

    def _retry_identity(self, call: ToolCall) -> tuple[str, str] | None:
        if not self._is_mutating_call(call):
            return None
        try:
            arguments = json.loads(call.function.arguments)
        except json.JSONDecodeError:
            return None
        if not isinstance(arguments, dict):
            return None
        operation = next(
            (
                arguments[key]
                for key in ("action", "key", "change_id", "automation_id", "id", "name")
                if key in arguments
            ),
            call.function.name,
        )
        if not isinstance(operation, (str, int)) or isinstance(operation, bool):
            return None
        return call.function.name, str(operation)

    def _matches_retry(self, call: ToolCall, expected: tuple[str, str]) -> bool:
        return self._retry_identity(call) == expected

    def _request_runtime(self) -> ToolRuntime:
        runtime = self._runtime
        reply_effects = runtime.reply_effects if runtime.reply_effects is not None else []
        return replace(
            runtime,
            origin=TurnOrigin.USER_MESSAGE,
            allow_automation=True,
            reply_effects=reply_effects,
            native_web_fallback=self._native_web_fallback,
        )


class ChatService:
    """Answer with cross-scope person memory and an event-bound Agent runtime."""

    def __init__(
        self,
        *,
        settings: Settings,
        provider: ModelCompleter | None = None,
        model_executor: ModelExecutor | None = None,
        concurrency: ConcurrencyManager,
        ledger: EventLedgerRepository,
        people: PeopleRepository,
        memories: MemoryFactService,
        tools: AgentToolService,
        relationships: RelationshipRepository,
        web_sources: WebSearchSourceRepository,
        runtime_config: RuntimeConfigService,
        time_service: TimeContextService,
        source_policy: SourceDisplayPolicy | None = None,
        source_renderer: SourceRenderer | None = None,
        memory_context: MemoryContextService | None = None,
        memory_attribution: MemoryAttributionWorker | None = None,
        context_assembler: ContextAssembler | None = None,
        prompt_composer: PromptComposer | None = None,
        turn_coordinator: ConversationTurnCoordinator | None = None,
        reply_sequence: ReplySequenceManager | None = None,
        emoji_effects: EmojiReplyEffectService | None = None,
        speech_effects: VoiceReplyEffectService | None = None,
        reply_effects: ReplyEffectRepository | None = None,
        voice_preferences: VoicePreferenceService | None = None,
        event_publisher: LifecycleEventPublisher | None = None,
        tool_artifacts: ToolArtifactWriter | None = None,
        tool_invocations: ToolInvocationRecorder | None = None,
        rollup_repository: ConversationRollupRepository | None = None,
        rollup_service: ConversationRollupService | None = None,
        conversation_scopes: ConversationScopeRepository | None = None,
        effect_gate: ConversationEffectGate | None = None,
    ) -> None:
        self._settings = settings
        self._ledger_origin = TurnOrigin.USER_MESSAGE.value
        models = require_model_executor(
            model_executor,
            provider=provider,
            model=settings.llm_model or "fake",
        )
        self._models = models
        self._concurrency = concurrency
        self._ledger = ledger
        self._conversation_scopes = conversation_scopes or ConversationScopeRepository(
            ledger._database
        )
        self._effect_gate = effect_gate or ConversationEffectGate()
        self._people = people
        self._memories = memories
        self._relationships = relationships
        self._tools = tools
        self._web_sources = web_sources
        self._source_policy = source_policy or SourceDisplayPolicy()
        self._source_renderer = source_renderer or SourceRenderer()
        self._runtime_config = runtime_config
        self._web_router = WebProviderRouter(
            tavily_domains=settings.web.tavily_domains,
            allow_provider_override=settings.web.web_allow_provider_override,
            fallback_on_access_denied=settings.web.web_fallback_on_access_denied,
            fallback_on_target_miss=settings.web.web_fallback_on_target_miss,
        )
        self._agent_runner = AgentRunner(models, concurrency, web_router=self._web_router)
        self._capability_index = CapabilityIndexCache()
        self._admin_tools: AdminToolService | None = None
        self._automation_tools: AutomationToolProvider | None = None
        self._plugin_tools: PluginToolProvider | None = None
        self._external_tool_providers: list[ToolProvider] = []
        self._tool_artifacts = tool_artifacts
        self._tool_invocations = tool_invocations
        self._tool_metrics = ToolKernelMetrics()
        self._time = time_service
        if memory_context is None:
            memory_repository = MemoryFactRepository(self._ledger._database)
            memory_context = MemoryContextService(
                query_builder=MemoryQueryBuilder(MemoryTargetResolver(self._people)),
                retriever=MemoryRetriever(
                    repository=memory_repository,
                    lexical_index=SQLiteMemoryFTSIndex(self._ledger._database),
                ),
                facts=self._memories,
            )
        self._memory_context = memory_context
        self._memory_attribution = memory_attribution
        if context_assembler is not None:
            self._context_assembler = context_assembler
        else:
            if rollup_repository is None or rollup_service is None:
                raise TypeError("rollup_repository and rollup_service are required")
            self._context_assembler = ContextAssembler(
                settings=settings,
                ledger=self._ledger,
                people=self._people,
                memory_context=memory_context,
                relationships=self._relationships,
                time_service=self._time,
                rollup_repository=rollup_repository,
                rollup_service=rollup_service,
            )
        self._prompt_composer = prompt_composer or PromptComposer(settings)
        self._turn_coordinator = turn_coordinator or ConversationTurnCoordinator(
            cancel_replies_on_new_message=settings.reply_sequence_cancel_on_new_message,
            interrupt_autonomous_on_new_message=(
                settings.conversation_interrupt_autonomous_on_new_message
            ),
        )
        self._reply_sequence = reply_sequence or ReplySequenceManager(self._turn_coordinator)
        self._reply_target_resolver = ReplyTargetResolver(self._ledger)
        self._emoji_effects = emoji_effects
        self._speech_effects = speech_effects
        self._reply_effects = reply_effects
        self._voice_preferences = voice_preferences
        self._event_publisher = event_publisher

    def set_admin_tools(self, service: AdminToolService) -> None:
        """Attach privileged tools to this same Agent loop without a second router."""

        self._admin_tools = service

    def set_automation_tools(self, service: AutomationToolProvider) -> None:
        """Attach owner-scoped scheduling tools without introducing a second Agent."""

        self._automation_tools = service

    def set_plugin_tools(self, service: PluginToolProvider) -> None:
        """Attach approved plugin tools without a parallel chat router."""

        self._plugin_tools = service

    def register_tool_provider(self, provider: ToolProvider) -> None:
        """Register one host-owned provider before the application starts."""

        if any(item.provider_id == provider.provider_id for item in self._external_tool_providers):
            raise ValueError(f"duplicate tool provider: {provider.provider_id}")
        self._external_tool_providers.append(provider)

    def _responses_append_only(self) -> bool:
        protocol = getattr(self._agent_runner._models, "protocol", None)
        if not callable(protocol):
            return False
        try:
            return protocol(ModelTask.CHAT_AGENT) is ModelProtocol.RESPONSES
        except (AttributeError, KeyError, RuntimeError, ValueError):
            return False

    def _build_tool_registry(
        self,
        runtime: ToolRuntime,
        *,
        web_was_used: bool,
    ) -> ToolProviderRegistry:
        """Adapt every domain service once; execution later uses bindings only."""

        registry = ToolProviderRegistry()

        def core_definitions(context: ToolRuntime) -> tuple[ChatTool, ...]:
            definitions = self._tools.definitions(context)
            return definitions

        async def core_execute(name: str, arguments: str, context: ToolRuntime) -> object:
            return await self._tools.execute(name, arguments, context)

        registry.register(
            InProcessToolProvider(
                provider_id="core",
                source=CapabilityTrustSource.CORE,
                definitions=core_definitions,
                execute=core_execute,
                bot_aliases=self._settings.bot_aliases,
            )
        )
        if self._tool_artifacts is not None:
            artifacts = self._tool_artifacts

            async def artifact_execute(
                name: str,
                arguments: str,
                context: ToolRuntime,
            ) -> object:
                del name
                decoded = json.loads(arguments)
                if not isinstance(decoded, dict):
                    raise ValueError("artifact arguments must be an object")
                handle = str(decoded.get("handle", ""))
                operation = str(decoded.get("operation", "text"))
                raw_path = decoded.get("path", [])
                if not isinstance(raw_path, list) or any(
                    isinstance(part, bool) or not isinstance(part, (str, int)) for part in raw_path
                ):
                    return ToolExecutionResult(
                        ok=False,
                        error_code="artifact_path_invalid",
                        public_message="Artifact path 必须是字符串键和整数下标组成的数组",
                        provider_id=_ARTIFACT_PROVIDER_ID,
                        tool_name=_ARTIFACT_READER_NAME,
                    )
                offset = int(decoded.get("offset", 0))
                limit = int(decoded.get("limit", 8000))
                query = str(decoded.get("query", ""))
                max_characters = _core_result_character_budget(context.runtime_config)
                result = await artifacts.read(
                    handle,
                    operation=operation,
                    path=tuple(raw_path),
                    offset=offset,
                    limit=limit,
                    query=query,
                    max_characters=max_characters,
                )
                if result is None:
                    return ToolExecutionResult(
                        ok=False,
                        error_code="artifact_not_found",
                        public_message="Artifact 不存在或已过期",
                        provider_id=_ARTIFACT_PROVIDER_ID,
                        tool_name=_ARTIFACT_READER_NAME,
                    )
                error_code = result.get("error_code")
                if isinstance(error_code, str):
                    detail = str(result.get("detail") or "Artifact 读取失败")
                    error_data = {
                        key: value
                        for key, value in result.items()
                        if key not in {"error_code", "detail"}
                    }
                    return ToolExecutionResult(
                        ok=False,
                        data=error_data or None,
                        error_code=error_code,
                        public_message=detail,
                        provider_id=_ARTIFACT_PROVIDER_ID,
                        tool_name=_ARTIFACT_READER_NAME,
                    )
                if result.get("mode") != "text":
                    return ToolExecutionResult(
                        ok=True,
                        data=result,
                        provider_id=_ARTIFACT_PROVIDER_ID,
                        tool_name=_ARTIFACT_READER_NAME,
                    )
                return _fit_artifact_page_result(
                    result,
                    max_characters=max_characters,
                )

            registry.register(
                InProcessToolProvider(
                    provider_id="artifacts",
                    source=CapabilityTrustSource.CORE,
                    definitions=lambda _context: (
                        ChatTool(
                            name="read_tool_artifact",
                            description=(
                                "读取工具产生的短期 Artifact。JSON 优先使用 inspect 查看结构、"
                                "get 按路径读取、search 返回关键词命中的完整对象；旧文本使用 text。"
                            ),
                            parameters={
                                "type": "object",
                                "properties": {
                                    "handle": {"type": "string"},
                                    "operation": {
                                        "type": "string",
                                        "enum": ["inspect", "get", "search", "text"],
                                        "description": (
                                            "JSON 使用 inspect/get/search；"
                                            "省略或 text 保持旧文本读取"
                                        ),
                                    },
                                    "path": {
                                        "type": "array",
                                        "items": {
                                            "anyOf": [
                                                {"type": "string"},
                                                {"type": "integer"},
                                            ]
                                        },
                                        "maxItems": 32,
                                        "description": (
                                            "相对返回中 logical_root 的 JSON 路径，"
                                            "对象键用字符串、数组下标用整数"
                                        ),
                                    },
                                    "offset": {
                                        "type": "integer",
                                        "minimum": 0,
                                        "description": "JSON 的键/元素/匹配偏移，或 text 字符偏移",
                                    },
                                    "limit": {
                                        "type": "integer",
                                        "minimum": 1,
                                        "maximum": 32000,
                                        "description": "JSON 条目数，或 text 最大字符数",
                                    },
                                    "query": {
                                        "type": "string",
                                        "description": "search 关键词，或 text 的字符串定位词",
                                    },
                                },
                                "required": ["handle"],
                                "additionalProperties": False,
                            },
                        ),
                    ),
                    execute=artifact_execute,
                )
            )
        if runtime.allow_automation and self._automation_tools is not None:
            automation = self._automation_tools

            async def automation_execute(
                name: str,
                arguments: str,
                context: ToolRuntime,
            ) -> object:
                return await automation.execute(name, arguments, context)

            registry.register(
                InProcessToolProvider(
                    provider_id="automation",
                    source=CapabilityTrustSource.AUTOMATION,
                    definitions=lambda _context: automation.definitions(),
                    execute=automation_execute,
                )
            )
        if runtime.allow_admin_actions and self._admin_tools is not None:
            admin = self._admin_tools

            async def admin_execute(
                name: str,
                arguments: str,
                context: ToolRuntime,
            ) -> object:
                return await admin.execute(name, arguments, context)

            registry.register(
                InProcessToolProvider(
                    provider_id="admin",
                    source=CapabilityTrustSource.ADMIN,
                    definitions=lambda _context: admin.definitions(),
                    execute=admin_execute,
                )
            )
        if self._plugin_tools is not None:
            plugin = self._plugin_tools

            async def plugin_execute(
                name: str,
                arguments: str,
                context: ToolRuntime,
            ) -> object:
                return await plugin.execute(
                    name,
                    arguments,
                    context,
                    web_was_used=web_was_used,
                )

            registry.register(
                InProcessToolProvider(
                    provider_id="plugin",
                    source=CapabilityTrustSource.PLUGIN,
                    definitions=lambda context: plugin.definitions(
                        context,
                        web_was_used=web_was_used,
                    ),
                    execute=plugin_execute,
                    plugin_read_only=plugin.is_read_only,
                )
            )
        for provider in self._external_tool_providers:
            registry.register(provider)
        return registry

    def configure_runtime_controls(self, runtime: RuntimeConfigSnapshot) -> None:
        """Apply HOT controls shared by the Agent prompt pipeline."""

        self._prompt_composer.configure_plugin_limits(runtime)

    def _record_memory_mutation_turn_outcome(self, outcome: str) -> None:
        if self._memory_context is not None:
            self._memory_context.metrics.record_mutation_turn_outcome(outcome)

    def set_event_publisher(self, publisher: LifecycleEventPublisher) -> None:
        """Attach the host notification bus without changing reply control flow."""

        self._event_publisher = publisher

    async def respond(
        self,
        inbound: InboundMessage,
        identity: ConversationScope,
        profile: UserProfileSnapshot,
        content: str,
        sender: OutboundSender,
        *,
        autonomous: bool = False,
        runtime_snapshot: RuntimeConfigSnapshot | None = None,
        visual_observation: VisualObservation | None = None,
        visual_input_present: bool = False,
        visual_failure: bool = False,
        turn_token: TurnToken | None = None,
        turn_snapshot: ConversationTurnSnapshot | None = None,
        structured_memory_command: MemoryStructuredCommand = MemoryStructuredCommand.NONE,
    ) -> int:
        """Run one ordered Agent turn and return the sent message count."""

        self._ledger_origin = (
            TurnOrigin.AUTONOMOUS_GROUP.value if autonomous else TurnOrigin.USER_MESSAGE.value
        )

        async with self._concurrency.conversation(identity.key):
            runtime_config = runtime_snapshot or await self._runtime_config.snapshot(
                user_id=inbound.sender.user_id,
                group_id=inbound.group_id,
            )
            if not visual_input_present and self._source_policy.standalone_request(content):
                sources = await self._web_sources.latest(identity.key)
                source_text = self._source_renderer.render(
                    sources,
                    maximum=runtime_config.web.extract_max_results,
                )
                reply = source_text or "当前对话中没有可提供的联网来源。"
                await self._deliver_and_record(
                    inbound,
                    sender,
                    OutboundMessage(text=reply),
                    turn_snapshot,
                )
                return 1

            source_display_requested = self._source_policy.requested(content)
            turn_origin = TurnOrigin.AUTONOMOUS_GROUP if autonomous else TurnOrigin.USER_MESSAGE
            self._ledger_origin = turn_origin.value
            memory_session = self._open_memory_session(
                inbound,
                identity,
                content,
                runtime_config,
                autonomous=autonomous,
                visual_input_present=visual_input_present,
                structured_command=structured_memory_command,
            )

            async def build_messages() -> tuple[
                tuple[ChatMessage, ...],
                frozenset[int],
                str,
                tuple[MemoryExposure, ...],
                MemoryQueryIntent | None,
                PromptRequestDiagnostics,
            ]:
                return await self._build_messages(
                    inbound,
                    identity,
                    profile,
                    content,
                    runtime_config,
                    visual_observation=visual_observation,
                    visual_failure=visual_failure,
                    turn_origin=turn_origin,
                    memory_session=memory_session,
                    turn_snapshot=turn_snapshot,
                )

            (
                messages,
                visible_event_ids,
                memory_turn_id,
                automatic_memory_exposures,
                memory_intent,
                prompt_diagnostics,
            ) = await self._run_effect(turn_snapshot, build_messages)
            exclusive_write = memory_session is not None and memory_session.exclusive_write
            scheduled_automation_intent = bool(
                not autonomous
                and not visual_input_present
                and self._automation_tools is not None
                and any(
                    tool.name == "automation_create"
                    for tool in self._automation_tools.definitions()
                )
                and is_scheduled_automation_request(content)
            )
            scheduled_automation_allowed = bool(scheduled_automation_intent and not exclusive_write)
            if scheduled_automation_allowed:
                messages = (
                    *messages[:-1],
                    ChatMessage(
                        role="system",
                        content=(
                            "当前消息可能涉及未来触发任务。如果需要创建定时任务，先用 "
                            "request_tools 加载 automation_create，再调用它。"
                            "如果只是当前查询、列举、讨论或无需持久化，则不要创建。"
                            "只有 automation_create 返回 confirmation=persisted 和真实 "
                            "automation_id 后，才能声称任务已经创建。"
                        ),
                    ),
                    messages[-1],
                )
            messages = _with_memory_mutation_contract(messages, exclusive_write)
            gateway = (
                cast(OneBotToolGateway, sender)
                if callable(getattr(sender, "call_api", None))
                else None
            )
            reply_target_control = ReplyTargetControl(visible_event_ids=visible_event_ids)
            reply_control = ReplyControlState(
                spec=default_reply_spec(hard_max_messages=runtime_config.reply.hard_max_messages)
            )
            reply_effects: list[ReplyEffect] = []
            if self._memory_context is not None and memory_session is not None:
                self._memory_context.metrics.record_runtime_access(memory_session.contract)
            web_route = self._web_router.select(content, runtime_config.web.mode)
            if web_route is not None:
                logger.info(
                    "web_route_selected conversation_hash=%s provider=%s reason=%s "
                    "matched_domain=%s attempt=%d fallback_allowed=%s",
                    identifier_hash(identity.key) or "missing",
                    web_route.provider.value,
                    web_route.reason.value,
                    web_route.matched_domain or "none",
                    web_route.attempt,
                    web_route.fallback_allowed,
                )
            voice_spontaneous_allowed = await self._voice_spontaneous_allowed(
                identity.key,
                inbound.sender.user_id,
                runtime_config,
            )
            runtime = ToolRuntime(
                inbound=inbound,
                gateway=gateway,
                allow_generic_onebot=(
                    not autonomous
                    and not visual_input_present
                    and inbound.sender.user_id in self._settings.superusers
                ),
                allow_admin_actions=(
                    not autonomous
                    and not visual_input_present
                    and inbound.sender.user_id in self._settings.superusers
                ),
                allow_automation=(not autonomous and not visual_input_present),
                conversation_key=identity.key,
                trigger_message_id=inbound.message_id,
                source_display_requested=source_display_requested,
                actor_user_id=inbound.sender.user_id,
                actor_is_superuser=inbound.sender.user_id in self._settings.superusers,
                current_group_id=inbound.group_id,
                mentioned_user_ids=inbound.mentioned_user_ids,
                runtime_config=runtime_config,
                origin=turn_origin,
                read_only=autonomous,
                turn_token=turn_token,
                turn_snapshot=turn_snapshot,
                reply_effects=reply_effects,
                reply_target_control=reply_target_control,
                reply_control=reply_control,
                voice_spontaneous_allowed=voice_spontaneous_allowed,
                selection_query=content,
                scheduled_automation_intent=scheduled_automation_allowed,
                native_web_fallback=bool(
                    web_route is not None
                    and web_route.provider is WebProvider.TAVILY
                    and web_route.reason is WebRouteReason.MODE
                ),
                web_route=web_route,
                memory_turn_id=memory_turn_id,
                memory_exposures=automatic_memory_exposures,
                memory_intent=memory_intent,
                memory_session=memory_session,
                prompt_diagnostics=prompt_diagnostics,
            )
            if turn_token is not None:
                async with self._turn_coordinator.track(turn_token, "generation"):
                    completed_agent = await self._run_agent(identity.key, messages, runtime)
            else:
                completed_agent = await self._run_agent(identity.key, messages, runtime)
            agent_result = completed_agent.result
            if agent_result.suppress_delivery:

                async def finish_suppressed() -> None:
                    await self._finish_memory_turn(
                        memory_session,
                        run_id=inbound.message_id,
                        delivered_text="",
                        delivered=False,
                        cancelled=False,
                    )

                await self._run_effect(turn_snapshot, finish_suppressed)
                return 0
            response_text = agent_result.text
            if agent_result.native_tool_events:
                native_response = recover_native_web_response(
                    events=agent_result.native_tool_events,
                    citations=agent_result.citations,
                    answer_text=agent_result.text,
                )

                async def save_native_response() -> None:
                    await self._web_sources.save_response(
                        conversation_key=identity.key,
                        trigger_message_id=inbound.message_id,
                        provider="deepseek_native",
                        response=native_response,
                        max_runs=runtime_config.web.source_max_runs_per_conversation,
                    )

                await self._run_effect(turn_snapshot, save_native_response)
                if not native_response.sources:
                    logger.warning(
                        "native_web_source_parse_failed conversation_hash=%s action_count=%d",
                        identifier_hash(identity.key) or "missing",
                        len(agent_result.native_tool_events),
                    )
                    completed_route = agent_result.web_route
                    source_failure = self._web_router.missing_source_failure(
                        completed_route,
                        source_display_requested=source_display_requested,
                        source_count=len(native_response.sources),
                    )
                    if source_failure is not None and completed_route is not None:
                        logger.warning(
                            "web_provider_fallback from_provider=deepseek_native "
                            "to_provider=tavily reason_category=%s",
                            source_failure.value,
                        )
                        fallback_limit = min(
                            2,
                            runtime.max_model_requests_override
                            or runtime_config.agent.max_model_requests,
                        )
                        fallback_runtime = replace(
                            runtime,
                            native_web_fallback=True,
                            web_route=self._web_router.fallback(
                                completed_route,
                                source_failure,
                            ),
                            max_model_requests_override=fallback_limit,
                        )
                        completed_agent = await self._run_agent(
                            identity.key,
                            messages,
                            fallback_runtime,
                        )
                        agent_result = completed_agent.result
                        response_text = agent_result.text
            sources = await self._web_sources.for_trigger(
                conversation_key=identity.key,
                trigger_message_id=inbound.message_id,
            )
            reply_to_message_id = await self._resolve_reply_target(
                inbound=inbound,
                conversation_key=identity.key,
                control=reply_target_control,
            )
            response_text = self._source_renderer.sanitize_model_text(response_text, sources)
            effects = runtime.reply_effects or []
            emoji_effects = [effect for effect in effects if isinstance(effect, PendingReplyEffect)]
            queued_voice = next(
                (effect for effect in effects if isinstance(effect, PendingVoiceReplyEffect)),
                None,
            )
            try:
                rendered = clean_model_output(
                    response_text,
                    max_characters=self._settings.max_output_characters,
                )
            except LLMEmptyResponseError:
                if not emoji_effects:
                    raise
                rendered = ""
            attribution_response_text = rendered
            prepared_effects: list[tuple[PendingReplyEffect, OutboundMessage]] = []
            preparation_fallbacks: list[OutboundMessage] = []
            if self._emoji_effects is not None:
                for effect in emoji_effects[: runtime_config.emoji.max_effects_per_reply]:
                    try:

                        async def prepare_emoji(
                            pending_effect: PendingReplyEffect = effect,
                            rendered_text: str = rendered,
                        ) -> EmojiPreparationResult:
                            assert self._emoji_effects is not None
                            return await self._emoji_effects.prepare(
                                pending_effect,
                                inbound=inbound,
                                response_text=rendered_text,
                                runtime=runtime_config,
                            )

                        preparation = await self._run_effect(
                            turn_snapshot,
                            prepare_emoji,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.exception(
                            "emoji_prepare_unexpected_failure exception_category=%s",
                            type(exc).__name__,
                        )
                        preparation = EmojiPreparationResult(
                            status=EmojiPreparationStatus.UNEXPECTED_FAILURE,
                            reason_code="unexpected_prepare_failure",
                        )
                    if preparation.status is EmojiPreparationStatus.READY:
                        assert preparation.message is not None
                        prepared_effects.append((effect, preparation.message))
                        continue
                    fallback_text = self._emoji_preparation_failure_text(effect, preparation)
                    if not fallback_text:
                        continue
                    if (
                        effect.mode is EmojiReplyMode.EMOJI_ONLY
                        or effect.placement is EmojiPlacement.ONLY
                    ):
                        rendered = fallback_text
                    elif not preparation_fallbacks:
                        preparation_fallbacks.append(OutboundMessage(text=fallback_text))
            prepared_voice: PreparedVoiceReply | None = None
            if (
                queued_voice is not None
                and turn_token is not None
                and self._speech_effects is not None
            ):

                async def prepare_voice() -> PreparedVoiceReply | None:
                    assert self._speech_effects is not None
                    return await self._speech_effects.prepare(
                        inbound=inbound,
                        response_text=rendered,
                        runtime=runtime_config,
                        token=turn_token,
                        mode=queued_voice.mode,
                        style_hint=queued_voice.style_hint,
                        language_hint=queued_voice.language_hint,
                        profile_id=queued_voice.profile_id,
                    )

                prepared_voice = await self._run_effect(turn_snapshot, prepare_voice)
            if (
                not rendered
                and not prepared_effects
                and not preparation_fallbacks
                and prepared_voice is None
            ):
                # A failed optional media effect must never turn a planned reply
                # into silence. AgentRunner normally prevents this, while this
                # guard also covers selectors/synthesizers that decline an effect.
                rendered = "我在，刚才没有生成可用的回复。"
            if turn_token is not None:
                if source_display_requested:
                    source_text = self._source_renderer.render(
                        sources,
                        maximum=runtime_config.web.extract_max_results,
                    )
                    if source_text:
                        rendered = clean_model_output(
                            f"{rendered}\n\n{source_text}",
                            max_characters=self._settings.max_output_characters,
                        )

                agent_body_delivered = False
                voice_message_id = id(prepared_voice.message) if prepared_voice is not None else 0

                async def record_chunk(
                    message: OutboundMessage,
                    receipt: OutboundSendReceipt,
                ) -> None:
                    nonlocal agent_body_delivered
                    if id(message) == voice_message_id or (
                        bool(message.text.strip())
                        and not message.media
                        and id(message) not in fallback_message_ids
                    ):
                        agent_body_delivered = True
                    if message.media and self._emoji_effects is not None:
                        await self._emoji_effects.record_send_accepted(
                            message,
                            source="reply_effect",
                        )
                    recorded = await self._record_outbound_message(inbound, message, receipt)
                    if message.media and self._emoji_effects is not None:
                        await self._emoji_effects.record_success(
                            message,
                            inbound=inbound,
                            source="reply_effect",
                            ledger_recorded=recorded,
                        )
                    if message.media and self._speech_effects is not None:
                        try:
                            await self._speech_effects.record_success(message)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            logger.exception(
                                "speech_post_send_record_failed exception_category=%s",
                                type(exc).__name__,
                            )
                    if any(media.kind is AttachmentKind.AUDIO for media in message.media):
                        reply_control.voice_sent = True
                    elif message.media:
                        reply_control.emoji_sent = True
                    if message.text.strip() and not message.media:
                        reply_control.text_sent = True
                    if id(message) in fallback_message_ids:
                        await publish_notification(
                            self._event_publisher,
                            EventName.EMOJI_FALLBACK_TEXT_SENT,
                            {"scope_type": inbound.scope_type.value},
                        )

                async def before_send(message: OutboundMessage) -> None:
                    if message.media and self._emoji_effects is not None:
                        await self._emoji_effects.record_send_attempted(
                            message,
                            source="reply_effect",
                        )

                async def record_failure(message: OutboundMessage, _error: Exception) -> None:
                    async def record() -> None:
                        if message.media and self._emoji_effects is not None:
                            await self._emoji_effects.record_failure(
                                message,
                                source="reply_effect",
                            )
                        if message.media and self._speech_effects is not None:
                            await self._speech_effects.record_failure(message)

                    await self._run_effect(turn_snapshot, record)

                effect_by_emoji_id = {
                    media.emoji_id: effect
                    for effect, message in prepared_effects
                    for media in message.media
                    if media.emoji_id
                }
                fallback_message_ids: set[int] = {id(message) for message in preparation_fallbacks}
                send_failure_notice_created = False

                async def recover_failure(
                    message: OutboundMessage,
                    _error: Exception,
                ) -> DeliveryFailureRecovery:
                    nonlocal send_failure_notice_created
                    emoji_id = next(
                        (media.emoji_id for media in message.media if media.emoji_id),
                        None,
                    )
                    failed_effect = (
                        effect_by_emoji_id.get(emoji_id) if emoji_id is not None else None
                    )
                    if failed_effect is None:
                        return DeliveryFailureRecovery(handled=False)
                    if (
                        failed_effect.mode is EmojiReplyMode.OPTIONAL
                        and not failed_effect.explicit_request
                    ):
                        return DeliveryFailureRecovery(handled=True)
                    if send_failure_notice_created:
                        return DeliveryFailureRecovery(handled=True)
                    send_failure_notice_created = True
                    failure_text = (
                        "表情没发出去，发送失败了。"
                        if failed_effect.mode is EmojiReplyMode.EMOJI_ONLY
                        or failed_effect.placement is EmojiPlacement.ONLY
                        else "表情没发出去，先用文字回你。"
                    )
                    fallback = OutboundMessage(text=failure_text)
                    fallback_message_ids.add(id(fallback))
                    return DeliveryFailureRecovery(
                        handled=True,
                        replacement_messages=(fallback,),
                    )

                async def deliver_chunk(message: OutboundMessage) -> OutboundSendReceipt:
                    async def deliver() -> OutboundSendReceipt:
                        await before_send(message)
                        receipt = await sender.send(message)
                        if not isinstance(receipt, OutboundSendReceipt):
                            raise TypeError("outbound sender returned no delivery receipt")
                        await record_chunk(message, receipt)
                        return receipt

                    return await self._run_effect(turn_snapshot, deliver)

                before = tuple(
                    message
                    for effect, message in prepared_effects
                    if effect.placement is EmojiPlacement.BEFORE_TEXT
                )
                after = tuple(
                    message
                    for effect, message in prepared_effects
                    if effect.placement is not EmojiPlacement.BEFORE_TEXT
                )
                after = (*after, *preparation_fallbacks)
                voice_only_confirmed = False
                if (
                    queued_voice is not None
                    and queued_voice.mode is VoiceMode.VOICE
                    and prepared_voice is not None
                ):
                    voice_message = prepared_voice.message
                    if reply_to_message_id is not None:
                        voice_message = replace(
                            voice_message,
                            reply_to_message_id=reply_to_message_id,
                        )
                    try:
                        receipt = await deliver_chunk(voice_message)
                    except Exception as exc:
                        retried = False
                        if voice_message.reply_to_message_id is not None:
                            voice_message = replace(voice_message, reply_to_message_id=None)
                            try:
                                receipt = await deliver_chunk(voice_message)
                            except Exception as retry_exc:
                                await record_failure(prepared_voice.message, retry_exc)
                            else:
                                retried = True
                        if not retried:
                            await record_failure(prepared_voice.message, exc)
                            prepared_voice = None
                        else:
                            voice_only_confirmed = True
                            prepared_voice = None
                    else:
                        voice_only_confirmed = True
                        prepared_voice = None
                elif prepared_voice is not None:
                    after = (*after, prepared_voice.message)
                suppress_text = bool(prepared_effects) and any(
                    effect.mode is EmojiReplyMode.EMOJI_ONLY
                    or effect.placement is EmojiPlacement.ONLY
                    for effect, _message in prepared_effects
                )
                suppress_text = suppress_text or voice_only_confirmed

                sequence = await self._reply_sequence.send(
                    text=rendered,
                    spec=reply_control.spec,
                    runtime=runtime_config,
                    token=turn_token,
                    sender=sender,
                    record_outbound=record_chunk,
                    record_failure=record_failure,
                    deliver_outbound=deliver_chunk,
                    recover_failure=recover_failure,
                    before_messages=before,
                    after_messages=after,
                    suppress_text=suppress_text,
                    reply_to_message_id=reply_to_message_id,
                )

                async def finish_delivery() -> None:
                    await self._record_reply_effects(
                        conversation_key=identity.key,
                        source_event_id=inbound.message_id,
                        user_id=inbound.sender.user_id,
                        control=reply_control,
                        cancelled=sequence.cancelled,
                    )
                    await self._finish_memory_turn(
                        memory_session,
                        run_id=inbound.message_id,
                        delivered_text=attribution_response_text,
                        delivered=agent_body_delivered,
                        cancelled=sequence.cancelled,
                    )

                await self._run_effect(turn_snapshot, finish_delivery)
                return sequence.sent_messages
            chunks = self._render_chunks(rendered, runtime_config) if rendered else ()
            legacy_messages = [
                message
                for effect, message in prepared_effects
                if effect.placement is EmojiPlacement.BEFORE_TEXT
            ]
            suppress_text = bool(prepared_effects) and any(
                effect.mode is EmojiReplyMode.EMOJI_ONLY or effect.placement is EmojiPlacement.ONLY
                for effect, _message in prepared_effects
            )
            if not suppress_text:
                legacy_messages.extend(OutboundMessage(text=chunk) for chunk in chunks)
            legacy_messages.extend(
                message
                for effect, message in prepared_effects
                if effect.placement is not EmojiPlacement.BEFORE_TEXT
            )
            legacy_messages.extend(preparation_fallbacks)
            if reply_to_message_id is not None and legacy_messages:
                legacy_messages[0] = replace(
                    legacy_messages[0],
                    reply_to_message_id=reply_to_message_id,
                )
            legacy_effect_by_emoji_id = {
                media.emoji_id: effect
                for effect, message in prepared_effects
                for media in message.media
                if media.emoji_id
            }
            legacy_failure_notice_sent = False
            legacy_fallback_ids = {id(message) for message in preparation_fallbacks}
            agent_body_delivered = False
            sent_count = 0
            for index, outbound in enumerate(legacy_messages):
                if len(legacy_messages) > 1 and index > 0:
                    delay = random.uniform(
                        runtime_config.reply.delay_min_seconds,
                        runtime_config.reply.delay_max_seconds,
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
                try:
                    if outbound.media and self._emoji_effects is not None:
                        await self._emoji_effects.record_send_attempted(
                            outbound,
                            source="reply_effect",
                        )
                    receipt = await self._send_with_fence(sender, outbound, turn_snapshot)
                    if not isinstance(receipt, OutboundSendReceipt):
                        raise TypeError("outbound sender returned no delivery receipt")
                except Exception as exc:
                    retry_succeeded = False
                    if outbound.reply_to_message_id is not None:
                        outbound = replace(outbound, reply_to_message_id=None)
                        logger.warning(
                            "reply_quote_delivery_failed retry_without_quote=true "
                            "exception_category=%s",
                            type(exc).__name__,
                        )
                        try:
                            receipt = await self._send_with_fence(sender, outbound, turn_snapshot)
                            if not isinstance(receipt, OutboundSendReceipt):
                                raise TypeError("outbound sender returned no delivery receipt")
                        except Exception as retry_exc:
                            exc = retry_exc
                        else:
                            retry_succeeded = True
                    if not retry_succeeded:
                        if outbound.media and self._emoji_effects is not None:
                            await self._emoji_effects.record_failure(
                                outbound,
                                source="reply_effect",
                            )
                        emoji_id = next(
                            (media.emoji_id for media in outbound.media if media.emoji_id),
                            None,
                        )
                        failed_effect = (
                            legacy_effect_by_emoji_id.get(emoji_id)
                            if emoji_id is not None
                            else None
                        )
                        if failed_effect is None:
                            raise exc
                        if (
                            failed_effect.mode is EmojiReplyMode.OPTIONAL
                            and not failed_effect.explicit_request
                        ):
                            continue
                        if legacy_failure_notice_sent:
                            continue
                        legacy_failure_notice_sent = True
                        fallback = OutboundMessage(
                            text=(
                                "表情没发出去，发送失败了。"
                                if failed_effect.mode is EmojiReplyMode.EMOJI_ONLY
                                or failed_effect.placement is EmojiPlacement.ONLY
                                else "表情没发出去，先用文字回你。"
                            )
                        )
                        fallback_receipt = await self._send_with_fence(
                            sender, fallback, turn_snapshot
                        )
                        if not isinstance(fallback_receipt, OutboundSendReceipt):
                            raise TypeError("outbound sender returned no delivery receipt") from exc
                        sent_count += 1
                        await self._record_outbound_message(inbound, fallback, fallback_receipt)
                        await publish_notification(
                            self._event_publisher,
                            EventName.EMOJI_FALLBACK_TEXT_SENT,
                            {"scope_type": inbound.scope_type.value},
                        )
                        continue
                sent_count += 1
                if (
                    outbound.text.strip()
                    and not outbound.media
                    and id(outbound) not in legacy_fallback_ids
                ):
                    agent_body_delivered = True
                if outbound.media and self._emoji_effects is not None:
                    await self._emoji_effects.record_send_accepted(
                        outbound,
                        source="reply_effect",
                    )
                recorded = await self._record_outbound_message(inbound, outbound, receipt)
                if outbound.media and self._emoji_effects is not None:
                    await self._emoji_effects.record_success(
                        outbound,
                        inbound=inbound,
                        source="reply_effect",
                        ledger_recorded=recorded,
                    )
            if source_display_requested:
                source_text = self._source_renderer.render(
                    sources,
                    maximum=runtime_config.web.extract_max_results,
                )
                if source_text:
                    receipt = await self._send_with_fence(
                        sender,
                        OutboundMessage(text=source_text),
                        turn_snapshot,
                    )
                    await self._record_outbound(inbound, source_text, receipt)
                    sent_count += 1
            await self._finish_memory_turn(
                memory_session,
                run_id=inbound.message_id,
                delivered_text=attribution_response_text,
                delivered=agent_body_delivered,
                cancelled=False,
            )
            return sent_count

    def _open_memory_session(
        self,
        inbound: InboundMessage,
        identity: ConversationScope,
        content: str,
        runtime: RuntimeConfigSnapshot,
        *,
        autonomous: bool,
        visual_input_present: bool,
        structured_command: MemoryStructuredCommand,
    ) -> TurnMemorySession | None:
        if self._memory_context is None:
            return None
        origin = (
            RuntimeTurnOrigin.AUTONOMOUS_GROUP if autonomous else RuntimeTurnOrigin.USER_MESSAGE
        )
        attachments = (*inbound.attachments, *inbound.reply_attachments)
        image_present = visual_input_present or any(
            item.kind is AttachmentKind.IMAGE for item in attachments
        )
        return TurnMemorySession.open(
            inbound=inbound,
            identity=identity,
            runtime=runtime,
            memory_context=self._memory_context,
            origin=origin,
            user_question=content,
            authority=TurnAuthority(
                actor_user_id=inbound.sender.user_id,
                bot_user_id=inbound.bot_user_id or "bot",
                origin=origin,
                permission_ceiling=frozenset(),
                delegated_authority=None,
                authority_revision=1,
            ),
            structured_command=structured_command,
            image_present=image_present,
            attribution=self._memory_attribution,
        )

    async def _finish_memory_turn(
        self,
        session: TurnMemorySession | None,
        *,
        run_id: str,
        delivered_text: str,
        delivered: bool,
        cancelled: bool,
    ) -> None:
        if session is None:
            return
        if cancelled:
            status = DeliveryStatus.CANCELLED
        elif delivered:
            status = DeliveryStatus.COMPLETE
        else:
            status = DeliveryStatus.FAILED
        await session.on_delivery_confirmed(
            DeliverySummary(
                final_agent_run_id=run_id,
                status=status,
                delivered_text=delivered_text,
            )
        )
        await session.close()

    async def _voice_spontaneous_allowed(
        self,
        conversation_key: str,
        user_id: str,
        runtime: RuntimeConfigSnapshot,
    ) -> bool:
        if self._voice_preferences is not None:
            mode = await self._voice_preferences.current_mode(user_id)
            if mode is VoicePreferenceMode.TEXT_ONLY:
                return False
        if self._reply_effects is None:
            return True
        cadence = await self._reply_effects.voice_cadence(conversation_key)
        return self._reply_effects.spontaneous_allowed(
            cadence,
            frequency=runtime.speech.spontaneous_frequency,
        )

    async def _record_reply_effects(
        self,
        *,
        conversation_key: str,
        source_event_id: str,
        user_id: str,
        control: ReplyControlState,
        cancelled: bool,
    ) -> None:
        if cancelled or self._reply_effects is None:
            return
        if not (control.text_sent or control.voice_sent or control.emoji_sent):
            return
        eligible = None
        if self._voice_preferences is not None:
            mode = await self._voice_preferences.current_mode(user_id)
            if mode is VoicePreferenceMode.TEXT_ONLY:
                eligible = False
        await self._reply_effects.record(
            conversation_key=conversation_key,
            source_event_id=source_event_id,
            text_sent=control.text_sent,
            voice_sent=control.voice_sent,
            emoji_sent=control.emoji_sent,
            voice_request_basis=control.voice_request_basis or "none",
            voice_cadence_eligible=eligible,
        )

    async def handle_turn(
        self,
        inbound: InboundMessage,
        identity: ConversationScope,
        profile: UserProfileSnapshot,
        content: str,
        sender: OutboundSender,
        **kwargs: Any,
    ) -> int:
        """Production ConversationRuntime entry that never accepts a PlannedTurn."""

        return await self.respond(inbound, identity, profile, content, sender, **kwargs)

    async def _build_messages(
        self,
        inbound: InboundMessage,
        identity: ConversationScope,
        profile: UserProfileSnapshot,
        content: str,
        runtime: RuntimeConfigSnapshot,
        *,
        visual_observation: VisualObservation | None = None,
        visual_failure: bool = False,
        turn_origin: TurnOrigin = TurnOrigin.USER_MESSAGE,
        memory_session: TurnMemorySession | None = None,
        turn_snapshot: ConversationTurnSnapshot | None = None,
    ) -> tuple[
        tuple[ChatMessage, ...],
        frozenset[int],
        str,
        tuple[MemoryExposure, ...],
        MemoryQueryIntent | None,
        PromptRequestDiagnostics,
    ]:
        retrieval = None
        persist_exposure = True
        memory_mode = MemoryContextMode.LEXICAL
        memory_intent: MemoryQueryIntent | None = None
        if memory_session is not None:
            retrieval = await memory_session.prefetch()
            if retrieval is None:
                retrieval = empty_retrieval()
            persist_exposure = False
            memory_intent = memory_session.prefetch_intent
            if memory_intent is not None:
                memory_mode = memory_intent.mode
        if turn_snapshot is None:
            raise ConversationCoverageError("chat turn requires a conversation snapshot")
        context = await self._context_assembler.assemble(
            inbound=inbound,
            identity=identity,
            profile=profile,
            turn=turn_snapshot,
            content=content,
            runtime=runtime,
            memory_mode=memory_mode,
            self_recall=False,
            memory_intent=memory_intent,
            turn_origin=turn_origin.value,
            memory_retrieval=retrieval,
            persist_memory_exposure=persist_exposure,
        )
        if memory_session is not None:
            memory_session.stage_prompt_selection(
                context.injected_memory_ids,
                context.memory_exposures,
            )
        composition = self._prompt_composer.compose(
            inbound=inbound,
            context=context,
            runtime=runtime,
            visual_observation=visual_observation,
            visual_failure=visual_failure,
        )
        return (
            composition.messages,
            context.visible_event_ids,
            context.memory_turn_id,
            context.memory_exposures,
            context.memory_intent,
            PromptRequestDiagnostics(
                conversation_prefix_hash=composition.metrics.conversation_prefix_hash,
                prompt_snapshot_fingerprint=(composition.metrics.prompt_snapshot_fingerprint),
                static_prompt_revision=composition.metrics.stable_prefix_hash,
            ),
        )

    async def _resolve_reply_target(
        self,
        *,
        inbound: InboundMessage,
        conversation_key: str,
        control: ReplyTargetControl | None,
    ) -> str | None:
        source = "none"
        event_id: int | None = None
        if control is not None and control.override_applied:
            source = "agent"
            event_id = control.event_id
        if event_id is None:
            if source == "agent":
                logger.info(
                    "reply_target_resolved conversation_hash=%s source=agent "
                    "event_id=none outcome=cleared",
                    identifier_hash(conversation_key) or "missing",
                )
            return None
        resolution = await self._reply_target_resolver.resolve(event_id, inbound=inbound)
        logger.info(
            "reply_target_resolved conversation_hash=%s source=%s event_id=%d outcome=%s",
            identifier_hash(conversation_key) or "missing",
            source,
            event_id,
            resolution.reason,
        )
        return resolution.platform_message_id

    async def _run_agent(
        self,
        conversation_key: str,
        initial_messages: tuple[ChatMessage, ...],
        runtime: ToolRuntime,
    ) -> _CompletedAgentRun:
        config = runtime.runtime_config
        if config is None:
            config = await self._runtime_config.snapshot(
                user_id=runtime.inbound.sender.user_id,
                group_id=runtime.inbound.group_id,
            )
            runtime = replace(runtime, runtime_config=config)
        exposure_registry = MemoryExposureRegistry(runtime.memory_exposures)
        runtime = replace(runtime, memory_exposure_registry=exposure_registry)
        runtime = await self._prepare_tool_candidates(runtime)
        current_time = await self._time.current(runtime.inbound.sender.user_id)
        backend = _ChatAgentBackend(self, runtime)
        if runtime.turn_snapshot is not None and not await self._validate_turn_snapshot(
            runtime.turn_snapshot
        ):
            raise TurnSupersededError("turn generation changed before model invocation")

        async def before_model_request() -> None:
            snapshot = runtime.turn_snapshot
            if snapshot is not None and not await self._validate_turn_snapshot(snapshot):
                raise TurnSupersededError("turn generation changed before model invocation")

        result = await self._agent_runner.run(
            initial_messages,
            AgentRuntime(
                origin=runtime.origin,
                actor_user_id=runtime.actor_user_id,
                actor_is_superuser=runtime.actor_is_superuser,
                delegated_authority=None,
                conversation_key=conversation_key,
                current_group_id=runtime.current_group_id,
                bot_user_id=runtime.inbound.bot_user_id,
                gateway=runtime.gateway,
                runtime_config=config,
                current_time=current_time,
                allowed_capabilities=(
                    frozenset({"web", "web_search"})
                    if not runtime.tools_closed and not runtime.read_only
                    else frozenset()
                ),
                max_tool_calls=config.agent.max_tool_calls,
                max_model_requests=(
                    min(
                        config.agent.max_model_requests,
                        runtime.max_model_requests_override,
                    )
                    if runtime.max_model_requests_override is not None
                    else config.agent.max_model_requests
                ),
                prompt_diagnostics=runtime.prompt_diagnostics,
                before_model_request=before_model_request,
                force_tavily_fallback=runtime.native_web_fallback,
                web_route=runtime.web_route,
            ),
            backend,
        )
        return _CompletedAgentRun(
            result=result,
            memory_exposures=exposure_registry.snapshot(),
        )

    async def _validate_turn_snapshot(self, snapshot: ConversationTurnSnapshot) -> bool:
        return self._turn_coordinator.version_matches(
            snapshot.scope_key,
            snapshot.coordinator_version,
        ) and await self._conversation_scopes.generation_matches(
            snapshot.scope_id,
            snapshot.generation,
        )

    async def _run_effect(
        self,
        snapshot: ConversationTurnSnapshot | None,
        effect: Callable[[], Awaitable[_EffectResult]],
    ) -> _EffectResult:
        if snapshot is None:
            return await effect()
        try:
            async with self._effect_gate.permit(
                snapshot,
                validate=self._validate_turn_snapshot,
                timeout_seconds=self._settings.conversation_effect_gate_timeout_seconds,
            ):
                return await effect()
        except (EffectGateTimeoutError, EffectPermitRejectedError) as exc:
            raise TurnSupersededError("turn effect permit was rejected") from exc

    async def _send_with_fence(
        self,
        sender: OutboundSender,
        message: OutboundMessage,
        snapshot: ConversationTurnSnapshot | None,
    ) -> OutboundSendReceipt:
        async def send() -> OutboundSendReceipt:
            receipt = await sender.send(message)
            if not isinstance(receipt, OutboundSendReceipt):
                raise TypeError("outbound sender returned no delivery receipt")
            return receipt

        return await self._run_effect(snapshot, send)

    async def _deliver_and_record(
        self,
        inbound: InboundMessage,
        sender: OutboundSender,
        message: OutboundMessage,
        snapshot: ConversationTurnSnapshot | None,
    ) -> OutboundSendReceipt:
        async def deliver() -> OutboundSendReceipt:
            receipt = await sender.send(message)
            if not isinstance(receipt, OutboundSendReceipt):
                raise TypeError("outbound sender returned no delivery receipt")
            await self._record_outbound_message(inbound, message, receipt)
            return receipt

        return await self._run_effect(snapshot, deliver)

    async def generate_external_reply(
        self,
        *,
        event: EventRecord,
        authorization_user_id: str,
        runtime: RuntimeConfigSnapshot,
        agent_intent: str,
        turn_token: TurnToken,
        turn_snapshot: ConversationTurnSnapshot,
    ) -> AgentRunResult:
        """Generate one tool-free reply for a persisted external event.

        The synthetic envelope below is authority metadata only.  It is never
        appended to the ledger and the prompt identifies the trigger as an
        untrusted external event rather than a QQ user message.
        """

        self._ledger_origin = TurnOrigin.PLUGIN_BACKGROUND.value
        conversation_key = event.scope.key
        if turn_snapshot.scope_key != conversation_key:
            raise TurnSupersededError("external turn snapshot scope mismatch")
        context = await self._context_assembler.assemble_external(
            event=event,
            turn=turn_snapshot,
            authorization_user_id=authorization_user_id,
            runtime=runtime,
            agent_intent=agent_intent,
        )
        composition = self._prompt_composer.compose_external(
            context=context,
            runtime=runtime,
            source_plugin_id=event.source_plugin_id or "",
            external_source=event.external_source or "external",
            event_type=event.external_event_type or "event",
            agent_intent=agent_intent,
        )
        inbound = InboundMessage(
            message_id=event.platform_message_id,
            event_type="external_event",
            scope_type=event.scope_type,
            sender=SenderIdentity(user_id=authorization_user_id),
            text=event.content,
            bot_user_id=event.bot_user_id,
            group_id=event.group_id,
            received_at=event.occurred_at,
        )
        tool_runtime = ToolRuntime(
            inbound=inbound,
            gateway=None,
            allow_generic_onebot=False,
            allow_admin_actions=False,
            allow_automation=False,
            conversation_key=conversation_key,
            trigger_message_id=event.platform_message_id,
            actor_user_id=authorization_user_id,
            actor_is_superuser=False,
            current_group_id=event.group_id,
            runtime_config=runtime,
            origin=TurnOrigin.PLUGIN_BACKGROUND,
            tools_closed=True,
            read_only=True,
            align_conversation_prefix_tools=True,
            turn_token=turn_token,
            turn_snapshot=turn_snapshot,
            reply_target_control=ReplyTargetControl(visible_event_ids=context.visible_event_ids),
            selection_query=event.content,
            web_route=self._web_router.select("", runtime.web.mode),
            max_model_requests_override=min(2, runtime.agent.max_model_requests),
            prompt_diagnostics=PromptRequestDiagnostics(
                conversation_prefix_hash=composition.metrics.conversation_prefix_hash,
                prompt_snapshot_fingerprint=(composition.metrics.prompt_snapshot_fingerprint),
                static_prompt_revision=composition.metrics.stable_prefix_hash,
            ),
        )
        completed = await self._run_agent(conversation_key, composition.messages, tool_runtime)
        result = completed.result
        try:
            rendered = clean_model_output(
                result.text,
                max_characters=self._settings.max_output_characters,
            )
        except LLMEmptyResponseError:
            rendered = ""
        return replace(result, text=rendered)

    async def _prepare_tool_candidates(self, runtime: ToolRuntime) -> ToolRuntime:
        """Apply artifact retention; capability runtime owns discovery and exposure."""

        config = runtime.runtime_config
        assert config is not None
        if self._tool_artifacts is not None and config.tooling is not None:
            self._tool_artifacts.configure_retention(
                config.tooling.result_artifact_retention_seconds
            )
        return runtime

    @staticmethod
    def _decode_tool_result(value: str) -> dict[str, object]:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return {"ok": False, "error": "invalid_tool_result"}
        return payload if isinstance(payload, dict) else {"ok": False}

    @staticmethod
    def _admin_failure_text(result: dict[str, object]) -> str:
        detail = str(
            result.get("public_message")
            or result.get("detail")
            or result.get("error")
            or result.get("error_code")
            or "未知错误"
        )
        return f"操作未完成：{detail}"

    def _render_chunks(
        self,
        rendered: str,
        runtime: RuntimeConfigSnapshot,
    ) -> tuple[str, ...]:
        return split_qq_message(
            rendered,
            limit=runtime.reply.max_qq_message_chars,
        )

    async def _record_outbound(
        self,
        inbound: InboundMessage,
        content: str,
        receipt: OutboundSendReceipt,
        *,
        reply_to_message_id: str | None = None,
    ) -> bool:
        return await self._record_outbound_message(
            inbound,
            OutboundMessage(text=content, reply_to_message_id=reply_to_message_id),
            receipt,
        )

    async def _record_outbound_message(
        self,
        inbound: InboundMessage,
        message: OutboundMessage,
        receipt: OutboundSendReceipt,
    ) -> bool:
        """Persist text and ledger-safe media metadata after confirmed delivery."""

        if not isinstance(receipt, OutboundSendReceipt):
            raise TypeError("confirmed outbound recording requires a delivery receipt")
        platform_message_id = receipt.platform_message_id
        media_segments = tuple(self._ledger_media_segment(media) for media in message.media)
        content = self._ledger_content(message)
        recorded = False
        try:
            await self._ledger.append(
                bot_user_id=inbound.bot_user_id or "unknown-bot",
                platform_message_id=platform_message_id,
                scope_type=inbound.scope_type,
                sender_user_id=inbound.bot_user_id or "unknown-bot",
                direction="outbound",
                content=content,
                segments=(
                    *(
                        ({"type": "reply", "data": {"id": message.reply_to_message_id}},)
                        if message.reply_to_message_id
                        else ()
                    ),
                    *(({"type": "text", "data": {"text": message.text}},) if message.text else ()),
                    *media_segments,
                ),
                group_id=inbound.group_id,
                private_peer_user_id=(
                    inbound.sender.user_id if inbound.scope_type is ScopeType.PRIVATE else None
                ),
                reply_to_message_id=message.reply_to_message_id,
                sender_is_bot=True,
                origin=self._ledger_origin,
            )
            recorded = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "confirmed_outbound_record_failed transport=%s exception_category=%s",
                receipt.transport,
                type(exc).__name__,
            )
        await publish_notification(
            self._event_publisher,
            EventName.REPLY_SENT,
            {
                "trigger_message_id": inbound.message_id,
                "platform_message_id": platform_message_id,
                "scope_type": inbound.scope_type.value,
                "character_count": len(content),
                "delivered": True,
                "recorded": recorded,
            },
        )
        return recorded

    async def record_confirmed_outbound(
        self,
        inbound: InboundMessage,
        message: OutboundMessage,
        receipt: OutboundSendReceipt,
    ) -> bool:
        """Share the same ledger boundary with deterministic media commands."""

        return await self._record_outbound_message(inbound, message, receipt)

    @staticmethod
    def _emoji_preparation_failure_text(
        effect: PendingReplyEffect,
        result: EmojiPreparationResult,
    ) -> str:
        if effect.mode is EmojiReplyMode.OPTIONAL and not effect.explicit_request:
            return ""
        if (
            effect.mode is not EmojiReplyMode.EMOJI_ONLY
            and effect.placement is not EmojiPlacement.ONLY
        ):
            return "表情没发出去，先用文字回你。"
        if result.status is EmojiPreparationStatus.NO_CANDIDATE:
            return "我这边暂时没有可用的表情。"
        if result.status is EmojiPreparationStatus.REPOSITORY_UNAVAILABLE:
            return "表情没发出去，表情库暂时不可用。"
        if result.status in {
            EmojiPreparationStatus.ASSET_MISSING,
            EmojiPreparationStatus.STORAGE_MISSING,
        }:
            return "这张表情暂时无法读取，我先不乱发。"
        return "表情没发出去，表情功能刚才出了点问题。"

    @staticmethod
    def _ledger_content(message: OutboundMessage) -> str:
        """Return only user-visible or spoken content, never internal voice metadata."""

        spoken_text = next((media.spoken_text for media in message.media if media.spoken_text), "")
        return message.text or spoken_text

    @staticmethod
    def _ledger_media_segment(media: OutboundMedia) -> dict[str, object]:
        if media.kind is AttachmentKind.AUDIO:
            return {
                "type": "record",
                "data": {
                    "summary": media.summary[:2000],
                    "mime_type": media.mime_type,
                    "duration_milliseconds": media.duration_milliseconds,
                    "profile_id": media.voice_profile_id or "",
                    "reference_key": media.voice_reference_key or "",
                    "target_language": media.voice_language or "",
                    "generation_id": media.generation_id,
                },
            }
        return {
            "type": "image",
            "data": {
                "emoji_id": media.emoji_id or "",
                "summary": media.summary[:2000],
                "mime_type": media.mime_type,
                "animated": media.animated,
            },
        }
