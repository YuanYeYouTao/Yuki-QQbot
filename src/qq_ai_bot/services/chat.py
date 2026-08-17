"""Person-centric context assembly, bounded Agent loop, sending, and ledger writes."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol, cast

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.admin.permission_catalog import contains_internal_capability_payload
from qq_ai_bot.automation.intent import enforce_creation_claim, is_scheduled_automation_request
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities import (
    AuthorityContext,
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityExposure,
    CapabilityPolicyContext,
    CapabilityPolicyEngine,
    CapabilityRisk,
    CapabilityTrustSource,
    FlashToolReranker,
    InProcessToolProvider,
    ToolArtifactWriter,
    ToolCandidateSelector,
    ToolExecutionResult,
    ToolInvocationContext,
    ToolKernelMetrics,
    ToolProvider,
    ToolProviderRegistry,
    ToolResultBudgeter,
    ToolSchemaBudgeter,
    UnifiedToolCatalog,
    UnifiedToolCatalogEntry,
    resolve_mutation_commit,
)
from qq_ai_bot.capabilities.request import (
    REQUEST_TOOLS_NAME,
    match_requestable_tools,
    request_tools_definition,
)
from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.reply import ReplyEffect
from qq_ai_bot.domain.conversations import ConversationIdentity, ScopeType
from qq_ai_bot.domain.messages import (
    AttachmentKind,
    ChatMessage,
    ChatTool,
    InboundMessage,
    OutboundMedia,
    OutboundMessage,
    OutboundSendReceipt,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.emoji.effects import EmojiReplyEffectService
from qq_ai_bot.emoji.models import (
    EmojiIntent,
    EmojiPlacement,
    EmojiPreparationResult,
    EmojiPreparationStatus,
    EmojiReplyMode,
    PendingReplyEffect,
)
from qq_ai_bot.llm.base import LLMEmptyResponseError
from qq_ai_bot.memory.attribution import (
    MemoryAttributionJob,
    MemoryAttributionWorker,
    MemoryExposure,
    MemoryExposureRegistry,
)
from qq_ai_bot.memory.context import MemoryContextService
from qq_ai_bot.memory.enums import MemoryAccessMode, MemoryContextMode
from qq_ai_bot.memory.fts import SQLiteMemoryFTSIndex
from qq_ai_bot.memory.models import MemoryQueryIntent
from qq_ai_bot.memory.query import MemoryQueryBuilder
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.retrieval import MemoryRetriever
from qq_ai_bot.memory.runtime.finalizer import (
    finalize_mutation_text,
    mutation_view_from_tool_result,
)
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.targets import MemoryTargetResolver
from qq_ai_bot.model_runtime.executor import ModelCompleter, ModelExecutor, require_model_executor
from qq_ai_bot.persistence.repositories import (
    EventLedgerRepository,
    PeopleRepository,
    RelationshipRepository,
    WebSearchSourceRepository,
)
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.planner.models import (
    PlannedTurn,
    PlannerReasonCode,
    ToolGroup,
    ToolMode,
    ToolScopeSummary,
    ToolSelection,
)
from qq_ai_bot.planner.observability import identifier_hash
from qq_ai_bot.services.agent_runner import (
    AgentRunner,
    AgentRunResult,
    AgentRuntime,
    AgentToolBackend,
)
from qq_ai_bot.services.agent_tools import AgentToolService, OneBotToolGateway, ToolRuntime
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.context_assembler import ContextAssembler
from qq_ai_bot.services.plugin_events import (
    LifecycleEventPublisher,
    publish_notification,
)
from qq_ai_bot.services.prompt_composer import PromptComposer
from qq_ai_bot.services.renderer import clean_model_output, split_qq_message
from qq_ai_bot.services.reply_sequence import (
    DeliveryFailureRecovery,
    ReplySequenceManager,
)
from qq_ai_bot.services.reply_target import ReplyTargetControl, ReplyTargetResolver
from qq_ai_bot.services.source_policy import SourceDisplayPolicy
from qq_ai_bot.services.source_renderer import SourceRenderer
from qq_ai_bot.services.turn_coordinator import ConversationTurnCoordinator, TurnToken
from qq_ai_bot.speech.models import VoiceAgentToolPolicy
from qq_ai_bot.speech.reply_effect import (
    PendingVoiceReplyEffect,
    PreparedVoiceReply,
    VoiceReplyEffectService,
)
from qq_ai_bot.time.service import TimeContextService
from qq_ai_bot.vision.models import VisualObservation
from qq_ai_bot.web.models import WebProvider
from qq_ai_bot.web.native_sources import recover_native_web_response
from qq_ai_bot.web.router import WebProviderRouter
from yuki_plugin_sdk.events import EventName

logger = logging.getLogger(__name__)

# Inherited Planner scope is a discovery mode, not permission to fill the
# schema budget. Six related tools plus the two stable discovery tools
# (get_my_capabilities and request_tools) keep the initial set compact while
# preserving access to the complete actor-authorized catalog.
_INHERITED_RELATED_TOOL_LIMIT = 6
_INHERITED_CANDIDATE_POOL_LIMIT = 24
_ARTIFACT_PROVIDER_ID = "artifacts"
_ARTIFACT_READER_NAME = "read_tool_artifact"
_SET_REPLY_TARGET_NAME = "set_reply_target"
_PLANNER_FAIL_CLOSED_MESSAGE = "本轮规划服务暂时不可用，未执行任何工具或持久化操作，请稍后重试。"
_MEMORY_MUTATION_EXECUTION_CONTRACT = (
    "本轮是后端授权的长期记忆变更终端轮次。必须先调用当前唯一暴露的长期记忆写能力，"
    "并严格以真实工具回执为准；不得直接用正文确认、模拟或承诺变更。定位失败时也必须"
    "保留真实失败回执，不得改用管理员能力。本轮不继续处理其他问答。"
)
_PLANNER_FAIL_CLOSED_REASONS = frozenset(
    {
        PlannerReasonCode.PLANNER_TIMEOUT_FALLBACK,
        PlannerReasonCode.PLANNER_INVALID_RESPONSE_FALLBACK,
        PlannerReasonCode.PLANNER_PROVIDER_ERROR_FALLBACK,
    }
)
_SET_REPLY_TARGET_TOOL = ChatTool(
    name=_SET_REPLY_TARGET_NAME,
    description=(
        "控制本轮最终 QQ 引用回复目标。Planner 已给出默认目标时通常不要调用；仅在多人混聊、"
        "需要明确回应某条较早消息或 Planner 目标不合适时调用。event_id 必须来自当前上下文"
        "消息行的 #EventRecord.id；省略 event_id 表示取消 Planner 的引用。该函数只设置本轮"
        "回复样式，不发送消息。每轮最多成功设置一次。"
    ),
    parameters={
        "type": "object",
        "properties": {"event_id": {"type": "integer", "minimum": 1}},
        "additionalProperties": False,
    },
)


def _initial_scopes_for_memory_access(
    access: MemoryAccessMode,
    scopes: frozenset[str],
) -> frozenset[str]:
    """Make Planner memory access the sole first-round Memory Scope decision."""

    if access in {MemoryAccessMode.TOOL, MemoryAccessMode.MUTATION}:
        return frozenset((*scopes, ToolGroup.MEMORY.value))
    return frozenset(
        scope
        for scope in scopes
        if scope != ToolGroup.MEMORY.value and not scope.startswith(f"{ToolGroup.MEMORY.value}.")
    )


def _automatic_memory_mode(
    access: MemoryAccessMode,
    mode: MemoryContextMode,
) -> MemoryContextMode:
    return mode if access is MemoryAccessMode.AUTOMATIC else MemoryContextMode.NONE


def _with_memory_mutation_contract(
    messages: tuple[ChatMessage, ...],
    access: MemoryAccessMode,
) -> tuple[ChatMessage, ...]:
    if access is not MemoryAccessMode.MUTATION:
        return messages
    return (*messages, ChatMessage(role="system", content=_MEMORY_MUTATION_EXECUTION_CONTRACT))


_BUILTIN_SCOPE_DESCRIPTIONS = {
    "memory": (
        "搜索近期或永久聊天历史；读取人物、群和 {bot_name} 自我长期记忆；"
        "创建、纠正、撤销、恢复和管理长期记忆"
    ),
    "relationship": "全局查询 {bot_name} 对已认识人物的好感度、信任度和关系阶段",
    "web": "联网搜索公开信息，并读取网页、链接和在线资料",
    "automation": "创建、查询、修改和删除提醒、定时任务与周期任务",
    "onebot": "执行 QQ 平台、群聊、好友和消息相关操作",
    "config": "读取和修改 {bot_name} 的运行配置",
    "admin": "超级管理员诊断和管理操作",
    "capability": "查询当前真实用户拥有的权限和可操作能力",
    "speech": "处理已经由 Planner 授权的语音回复",
    "plugin": "调用当前已批准并运行的本地插件能力",
}

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

    def planner_scope_descriptions(self) -> tuple[str, ...]: ...

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
        self._tools_closed = False
        self._web_was_used = False
        self._web_calls_used = 0
        self._capability_was_used = False
        self._admin_retry_constraint: tuple[str, str] | None = None
        self._admin_terminal_failure: dict[str, object] | None = None
        self._completed_admin_mutations: set[tuple[str, str]] = set()
        self._committed_mutation_messages: list[str] = []
        self._mutation_committed = False
        self._automation_persisted = False
        self._batch: list[ToolCall] = []
        self._catalog: UnifiedToolCatalog | None = None
        self._requestable_catalog: UnifiedToolCatalog | None = None
        self._provider_registry: ToolProviderRegistry | None = None
        self._requested_tool_names: set[str] = set()
        self._callable_tool_names: set[str] = set()
        self._tool_turn_recorded = False
        self._request_tools_called = False
        self._first_real_tool_recorded = False
        self._memory_locator_failed = False
        self._memory_mutation_attempted = False
        self._last_memory_mutation_result: dict[str, object] | None = None
        self._native_web_fallback = runtime.native_web_fallback

    def enable_native_web_fallback(self) -> None:
        """Allow Tavily tools only after the Runner verifies a fallback condition."""

        self._native_web_fallback = True

    def mark_native_web_used(self) -> None:
        """Apply post-Web isolation before same-response local calls execute."""

        self._web_was_used = True

    def definitions(self, runtime: AgentRuntime, *, web_was_used: bool) -> tuple[ChatTool, ...]:
        self._web_was_used = self._web_was_used or web_was_used
        response_controls = (
            ()
            if self._runtime.memory_access is MemoryAccessMode.MUTATION
            else self._response_control_definitions()
        )
        if self._tools_closed:
            self._callable_tool_names = {tool.name for tool in response_controls}
            self._log_tool_exposure(
                response_controls,
                selected_scopes=(),
                reason="business_tools_closed",
            )
            return response_controls
        request_runtime = self._request_runtime()
        self._provider_registry = self._service._build_tool_registry(
            request_runtime,
            web_was_used=self._web_was_used,
        )
        self._catalog = self._provider_registry.catalog(request_runtime)
        policy_scopes = tuple(sorted(self._runtime.tool_groups))
        if any(scope.startswith("mcp.") for scope in policy_scopes) and "mcp" not in policy_scopes:
            policy_scopes = (*policy_scopes, "mcp")
        selection = ToolSelection(
            mode=self._runtime.tool_mode,
            scopes=policy_scopes,
        )
        policy = CapabilityPolicyEngine()
        policy_context = CapabilityPolicyContext(
            authority=AuthorityContext(
                actor_user_id=self._runtime.actor_user_id,
                is_superuser=self._runtime.actor_is_superuser,
            ),
            origin=self._runtime.origin,
            tool_selection=selection,
            contains_images=bool(
                self._runtime.inbound.attachments or self._runtime.inbound.reply_attachments
            ),
            web_was_used=self._web_was_used,
        )
        visible = policy.visible(
            tuple(entry.descriptor for entry in self._catalog.entries),
            policy_context,
        )
        visible_names = {descriptor.model_name for descriptor in visible}
        # Planner scopes prioritize the initial schema set. They are not an
        # authority boundary: request_tools may load any capability permitted
        # by the real actor, origin and current tool mode.
        authority_visible = policy.visible(
            tuple(entry.descriptor for entry in self._catalog.entries),
            replace(
                policy_context,
                tool_selection=ToolSelection(
                    mode=(
                        ToolMode.INHERIT
                        if self._runtime.origin is TurnOrigin.USER_MESSAGE
                        else self._runtime.tool_mode
                    ),
                    scopes=(),
                ),
            ),
        )
        authority_visible_names = {descriptor.model_name for descriptor in authority_visible}
        self._requestable_catalog = replace(
            self._catalog,
            entries=tuple(
                entry
                for entry in self._catalog.entries
                if entry.descriptor.model_name in authority_visible_names
            ),
        )
        if not self._tool_turn_recorded and self._requestable_catalog.entries:
            self._service._tool_metrics.record_tool_enabled_turn(
                planner_scope_explicit=self._runtime.planner_scopes_explicit
            )
            self._tool_turn_recorded = True
        filtered_catalog = replace(
            self._catalog,
            entries=tuple(
                entry
                for entry in self._catalog.entries
                if entry.descriptor.model_name in visible_names
            ),
        )
        if self._runtime.memory_access in {
            MemoryAccessMode.TOOL,
            MemoryAccessMode.MUTATION,
        }:
            allowed_memory_effects = (
                {CapabilityEffect.WRITE_STATE}
                if self._runtime.memory_access is MemoryAccessMode.MUTATION
                else {CapabilityEffect.READ_STATE, CapabilityEffect.EXTERNAL_READ}
            )
            filtered_catalog = replace(
                filtered_catalog,
                entries=tuple(
                    entry
                    for entry in filtered_catalog.entries
                    if (
                        ToolGroup.MEMORY.value in entry.descriptor.scope_ids
                        and entry.descriptor.effect in allowed_memory_effects
                    )
                    or entry.descriptor.model_name in self._requested_tool_names
                    or (
                        self._runtime.memory_access is MemoryAccessMode.TOOL
                        and entry.descriptor.exposure is CapabilityExposure.DIRECT_ALWAYS
                    )
                ),
            )
        if self._runtime.scheduled_automation_intent:
            # A deterministic automation hint grants visibility, not an
            # obligation to create a task.  Keep automation_create present
            # through schema selection while preserving every other
            # Planner-approved scope for the Agent's own decision.
            filtered_catalog = replace(
                filtered_catalog,
                entries=tuple(
                    replace(
                        entry,
                        descriptor=replace(
                            entry.descriptor,
                            exposure=CapabilityExposure.DIRECT_ALWAYS,
                        ),
                    )
                    if entry.descriptor.model_name == "automation_create"
                    else entry
                    for entry in filtered_catalog.entries
                ),
            )
        if self._runtime.planner_scopes_explicit and not self._runtime.tool_groups:
            filtered_catalog = replace(
                filtered_catalog,
                entries=tuple(
                    entry
                    for entry in filtered_catalog.entries
                    if entry.descriptor.exposure is CapabilityExposure.DIRECT_ALWAYS
                    or entry.descriptor.model_name in self._requested_tool_names
                ),
            )
        if (
            self._runtime.selected_tool_names is not None
            and self._runtime.memory_access is not MemoryAccessMode.MUTATION
        ):
            filtered_catalog = replace(
                filtered_catalog,
                entries=tuple(
                    entry
                    for entry in filtered_catalog.entries
                    if entry.descriptor.model_name in self._runtime.selected_tool_names
                    or entry.descriptor.model_name in self._requested_tool_names
                    or entry.descriptor.exposure is CapabilityExposure.DIRECT_ALWAYS
                ),
            )
        if self._requested_tool_names:
            # A model-requested tool must survive the same schema/count budgets
            # that caused it to be omitted initially. Policy filtering happened
            # above, so this changes exposure priority rather than authority.
            filtered_catalog = replace(
                filtered_catalog,
                entries=tuple(
                    replace(
                        entry,
                        descriptor=replace(
                            entry.descriptor,
                            exposure=CapabilityExposure.DIRECT_ALWAYS,
                        ),
                    )
                    if entry.descriptor.model_name in self._requested_tool_names
                    else entry
                    for entry in filtered_catalog.entries
                ),
            )
        config = self._runtime.runtime_config
        assert config is not None
        tooling = config.tooling
        mcp = config.mcp
        known_scopes = {scope.scope_id for scope in filtered_catalog.scopes}
        selected_scopes = tuple(
            scope for scope in sorted(self._runtime.tool_groups) if scope in known_scopes
        )
        if (
            any(scope.startswith("mcp.") for scope in self._runtime.tool_groups)
            and "mcp" in known_scopes
            and "mcp" not in selected_scopes
        ):
            selected_scopes = (*selected_scopes, "mcp")
        if mcp is not None:
            mcp_entries = tuple(
                entry
                for entry in filtered_catalog.entries
                if entry.descriptor.trust_source is CapabilityTrustSource.MCP
            )
            if mcp_entries:
                mcp_budgeted = ToolSchemaBudgeter(
                    selected_tool_limit=(
                        None
                        if self._runtime.planner_scopes_explicit and selected_scopes
                        else mcp.selected_tool_limit
                    ),
                    schema_token_budget=mcp.schema_token_budget,
                ).select(
                    replace(filtered_catalog, entries=mcp_entries),
                    scopes=selected_scopes,
                    query=f"{self._runtime.selection_query} {self._runtime.planner_intent}",
                )
                allowed_mcp = {entry.descriptor.model_name for entry in mcp_budgeted.entries}
                filtered_catalog = replace(
                    filtered_catalog,
                    entries=tuple(
                        entry
                        for entry in filtered_catalog.entries
                        if entry.descriptor.trust_source is not CapabilityTrustSource.MCP
                        or entry.descriptor.model_name in allowed_mcp
                    ),
                )
        if (
            self._runtime.tool_groups
            and not selected_scopes
            and self._runtime.origin is not TurnOrigin.USER_MESSAGE
            and not any(
                entry.descriptor.exposure is CapabilityExposure.DIRECT_ALWAYS
                for entry in filtered_catalog.entries
            )
        ):
            self._callable_tool_names = {tool.name for tool in response_controls}
            self._log_tool_exposure(
                response_controls,
                selected_scopes=selected_scopes,
                reason="selected_scopes_unavailable",
            )
            return response_controls
        budgeted = ToolSchemaBudgeter(
            selected_tool_limit=(
                None
                if self._runtime.planner_scopes_explicit and selected_scopes
                else (tooling.selected_tool_limit if tooling is not None else None)
            ),
            schema_token_budget=tooling.schema_token_budget if tooling is not None else None,
        ).select(
            filtered_catalog,
            scopes=selected_scopes,
            query=f"{self._runtime.selection_query} {self._runtime.planner_intent}",
        )
        definitions = tuple(entry.descriptor.as_chat_tool() for entry in budgeted.entries)
        exposed_names = {tool.name for tool in definitions}
        may_request_more = bool(
            self._runtime.origin is TurnOrigin.USER_MESSAGE
            and self._requestable_catalog is not None
            and (
                self._runtime.memory_access is not MemoryAccessMode.MUTATION
                or self._memory_locator_failed
            )
            and any(
                entry.descriptor.model_name not in exposed_names
                for entry in self._requestable_catalog.entries
            )
        )
        if may_request_more:
            definitions = (*definitions, request_tools_definition())
        if response_controls:
            definitions = (
                *(tool for tool in definitions if tool.name != _SET_REPLY_TARGET_NAME),
                *response_controls,
            )
        self._callable_tool_names = {tool.name for tool in definitions}
        for entry in budgeted.entries:
            self._service._tool_metrics.record_selection(
                entry.provider_id,
                entry.descriptor.provider_tool_name or entry.descriptor.model_name,
                entry.estimated_schema_tokens,
            )
        if self._admin_retry_constraint is not None:
            definitions = tuple(
                tool for tool in definitions if tool.name == self._admin_retry_constraint[0]
            )
        definitions = tuple(sorted(definitions, key=lambda tool: tool.name))
        self._log_tool_exposure(
            definitions,
            selected_scopes=selected_scopes,
            reason="ready",
        )
        return definitions

    def _log_tool_exposure(
        self,
        definitions: tuple[ChatTool, ...],
        *,
        selected_scopes: tuple[str, ...],
        reason: str,
    ) -> None:
        """Log bounded capability metadata without message text or tool arguments."""

        planner_scope_source = "explicit" if self._runtime.planner_scopes_explicit else "inherited"
        planner_tool_groups = (
            self._runtime.planner_tool_groups
            if self._runtime.planner_tool_groups is not None
            else self._runtime.tool_groups
        )
        planner_scopes = (
            ",".join(sorted(planner_tool_groups)) or "none"
            if self._runtime.planner_scopes_explicit
            else "backend_authorized"
        )
        automation_scope_added = bool(
            self._runtime.scheduled_automation_intent
            and ToolGroup.AUTOMATION.value in self._runtime.tool_groups
            and ToolGroup.AUTOMATION.value not in planner_tool_groups
        )
        memory_scope_added = bool(
            ToolGroup.MEMORY.value in self._runtime.tool_groups
            and ToolGroup.MEMORY.value not in planner_tool_groups
        )
        effective_scopes = ",".join(selected_scopes) or (
            "none" if self._runtime.planner_scopes_explicit else "backend_authorized"
        )
        exposed_tools = ",".join(sorted(tool.name for tool in definitions)) or "none"
        logger.info(
            "agent_tools_exposed conversation_hash=%s origin=%s tool_mode=%s "
            "planner_scope_source=%s planner_scopes=%s automation_scope_added=%s "
            "memory_scope_added=%s "
            "effective_scopes=%s "
            "tools=%s exposed_count=%d requestable_count=%d reason=%s",
            identifier_hash(self._runtime.conversation_key) or "missing",
            self._runtime.origin.value,
            self._runtime.tool_mode.value,
            planner_scope_source,
            planner_scopes,
            automation_scope_added,
            memory_scope_added,
            effective_scopes,
            exposed_tools,
            len(definitions),
            len(self._requestable_catalog.entries) if self._requestable_catalog is not None else 0,
            reason,
        )

    def begin_batch(self, calls: tuple[ToolCall, ...], runtime: AgentRuntime) -> None:
        del runtime
        self._batch = list(calls)

    def did_use_web(self) -> bool:
        """Expose a provider-metadata-derived effect to the shared Agent loop."""

        return self._web_was_used

    async def execute(self, name: str, arguments_json: str, runtime: AgentRuntime) -> str:
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
        if name == _SET_REPLY_TARGET_NAME:
            return self._set_reply_target(arguments_json)
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
            if (
                self._runtime.memory_access is MemoryAccessMode.MUTATION
                and not self._memory_locator_failed
            ):
                return json.dumps(
                    {
                        "ok": False,
                        "error": "capability_not_loaded",
                        "detail": "记忆写入定位尚未失败，本轮不能提前加载其他能力。",
                    },
                    ensure_ascii=False,
                )
            return self._request_tools(arguments_json)
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
        if not self._first_real_tool_recorded:
            first_round_hit = not self._request_tools_called
            self._service._tool_metrics.record_first_round_tool_hit(hit=first_round_hit)
            logger.info(
                "agent_first_round_tool_hit conversation_hash=%s hit=%s "
                "planner_scope_explicit=%s tool=%s",
                identifier_hash(self._runtime.conversation_key) or "missing",
                first_round_hit,
                self._runtime.planner_scopes_explicit,
                descriptor.model_name,
            )
            self._first_real_tool_recorded = True
        effective_descriptor = self._effective_descriptor(call, descriptor)
        is_web_tool = ToolGroup.WEB.value in effective_descriptor.scope_ids
        is_memory_read_tool = (
            ToolGroup.MEMORY.value in effective_descriptor.scope_ids
            and effective_descriptor.effect is CapabilityEffect.READ_STATE
        )
        is_memory_write_tool = (
            ToolGroup.MEMORY.value in effective_descriptor.scope_ids
            and effective_descriptor.effect is CapabilityEffect.WRITE_STATE
        )
        if is_memory_read_tool and self._runtime.memory_access is MemoryAccessMode.AUTOMATIC:
            self._service._tool_metrics.record_automatic_memory_read_tool_call(
                locator_fallback=self._memory_locator_failed
            )
            self._memory_locator_failed = False
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
                    outcome = await descriptor.binding.invoke(
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
                        (
                            is_memory_write_tool
                            and self._runtime.memory_access is MemoryAccessMode.MUTATION
                        )
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
        if is_memory_write_tool and self._runtime.memory_access is MemoryAccessMode.MUTATION:
            if not self._memory_mutation_attempted:
                self._service._record_memory_mutation_turn_outcome("attempted")
            self._memory_mutation_attempted = True
            self._last_memory_mutation_result = decoded
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
                    if self._runtime.memory_access is MemoryAccessMode.MUTATION:
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
                self._memory_locator_failed = True
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
        if self._runtime.memory_access is MemoryAccessMode.MUTATION:
            return self._memory_mutation_final_text()
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

        if (
            self._runtime.memory_access is MemoryAccessMode.MUTATION
            and self._memory_mutation_attempted
        ):
            return self._memory_mutation_final_text()
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
        if self._runtime.memory_access is MemoryAccessMode.MUTATION:
            return self._memory_mutation_final_text()
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

    def _memory_mutation_final_text(self) -> str:
        result = self._last_memory_mutation_result
        if not self._memory_mutation_attempted or result is None:
            self._service._record_memory_mutation_turn_outcome("not_attempted")
        return finalize_mutation_text(
            mutation_view_from_tool_result(
                result,
                attempted=self._memory_mutation_attempted,
            )
        )

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
        if self._runtime.origin not in {
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

    def _request_tools(self, arguments_json: str) -> str:
        self._request_tools_called = True
        self._service._tool_metrics.record_request_tools()
        if self._runtime.memory_access is MemoryAccessMode.AUTOMATIC:
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
        if self._requestable_catalog is None:
            self._service._tool_metrics.record_request_tools_zero_result()
            return json.dumps(
                {"ok": False, "error": "capability_catalog_unavailable"},
                ensure_ascii=False,
            )
        matches = match_requestable_tools(
            self._requestable_catalog,
            query=query,
            limit=max_results,
            excluded_names=frozenset(self._callable_tool_names),
        )
        if not matches:
            self._service._tool_metrics.record_request_tools_zero_result()
            logger.info(
                "agent_request_tools_result conversation_hash=%s loaded_count=0",
                identifier_hash(self._runtime.conversation_key) or "missing",
            )
            return json.dumps(
                {
                    "ok": False,
                    "error": "capability_not_found",
                    "detail": "当前真实用户和场景允许的工具目录中没有匹配能力",
                },
                ensure_ascii=False,
            )
        loaded_names = {match.entry.descriptor.model_name for match in matches}
        if self._runtime.memory_access is MemoryAccessMode.AUTOMATIC and any(
            ToolGroup.MEMORY.value in match.entry.descriptor.scope_ids
            and match.entry.descriptor.effect is CapabilityEffect.READ_STATE
            for match in matches
        ):
            self._service._tool_metrics.record_automatic_memory_read_tools_loaded()
        logger.info(
            "agent_request_tools_result conversation_hash=%s loaded_count=%d",
            identifier_hash(self._runtime.conversation_key) or "missing",
            len(loaded_names),
        )
        loaded_scopes = {scope for match in matches for scope in match.entry.descriptor.scope_ids}
        self._requested_tool_names.update(loaded_names)
        selected = self._runtime.selected_tool_names
        self._runtime = replace(
            self._runtime,
            tool_mode=(
                ToolMode.INHERIT
                if self._runtime.tool_mode is ToolMode.NONE
                and self._runtime.origin is TurnOrigin.USER_MESSAGE
                else self._runtime.tool_mode
            ),
            tool_groups=frozenset((*self._runtime.tool_groups, *loaded_scopes)),
            selected_tool_names=(
                None if selected is None else frozenset((*selected, *loaded_names))
            ),
        )
        return json.dumps(
            {
                "ok": True,
                "data": {
                    "loaded_tools": [
                        {
                            "name": match.entry.descriptor.model_name,
                            "capability": match.entry.descriptor.canonical_name,
                            "description": match.entry.compact_description,
                        }
                        for match in matches
                    ],
                    "instruction": "下一步直接调用 loaded_tools 中的真实工具",
                },
            },
            ensure_ascii=False,
        )

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
        return replace(
            self._runtime,
            allow_generic_onebot=self._runtime.allow_generic_onebot,
            allow_admin_actions=self._runtime.allow_admin_actions,
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
        event_publisher: LifecycleEventPublisher | None = None,
        tool_artifacts: ToolArtifactWriter | None = None,
        tool_invocations: ToolInvocationRecorder | None = None,
    ) -> None:
        self._settings = settings
        models = require_model_executor(
            model_executor,
            provider=provider,
            model=settings.llm_model or "fake",
        )
        self._models = models
        self._concurrency = concurrency
        self._ledger = ledger
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
        self._tool_selector = ToolCandidateSelector()
        self._tool_reranker = FlashToolReranker(models)
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
        self._context_assembler = context_assembler or ContextAssembler(
            settings=settings,
            ledger=self._ledger,
            people=self._people,
            memory_context=memory_context,
            relationships=self._relationships,
            time_service=self._time,
        )
        self._prompt_composer = prompt_composer or PromptComposer(settings)
        self._turn_coordinator = turn_coordinator or ConversationTurnCoordinator(
            cancel_replies_on_new_message=settings.reply_sequence_cancel_on_new_message,
            interrupt_autonomous_on_new_message=(
                settings.planner_interrupt_autonomous_on_new_message
            ),
        )
        self._reply_sequence = reply_sequence or ReplySequenceManager(self._turn_coordinator)
        self._reply_target_resolver = ReplyTargetResolver(self._ledger)
        self._emoji_effects = emoji_effects
        self._speech_effects = speech_effects
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

    def planner_tool_scopes(
        self,
        base_scopes: tuple[str, ...],
        runtime: RuntimeConfigSnapshot | None = None,
    ) -> tuple[ToolScopeSummary, ...]:
        settings = getattr(self, "_settings", None)
        bot_name = settings.bot_display_name if settings is not None else "Yuki"
        summaries = [
            ToolScopeSummary(
                scope_id=scope,
                parent=scope.rpartition(".")[0] or None,
                display_name=scope,
                description=_BUILTIN_SCOPE_DESCRIPTIONS.get(
                    scope,
                    "{bot_name} 内置 " + scope + " 能力",
                ).format(bot_name=bot_name),
                tool_count=0,
                provider_ids=("core",),
                tags=(scope,),
            )
            for scope in base_scopes
        ]
        if self._plugin_tools is not None:
            plugin_descriptions = self._plugin_tools.planner_scope_descriptions()
            if plugin_descriptions:
                summaries.append(
                    ToolScopeSummary(
                        scope_id=ToolGroup.PLUGIN.value,
                        display_name="本地插件",
                        description="；".join(plugin_descriptions)[:300],
                        tool_count=len(plugin_descriptions),
                        provider_ids=("plugin",),
                        tags=("插件", "扩展"),
                    )
                )
        for provider in self._external_tool_providers:
            getter = getattr(provider, "scope_summaries", None)
            if callable(getter):
                try:
                    summaries.extend(getter(runtime))
                except TypeError:
                    summaries.extend(getter())
        merged: dict[str, ToolScopeSummary] = {}
        for item in summaries:
            previous = merged.get(item.scope_id)
            if previous is None:
                merged[item.scope_id] = item
                continue
            descriptions = tuple(
                dict.fromkeys(text for text in (previous.description, item.description) if text)
            )
            merged[item.scope_id] = ToolScopeSummary(
                scope_id=item.scope_id,
                parent=item.parent or previous.parent,
                display_name=item.display_name or previous.display_name,
                description="；".join(descriptions)[:300],
                tool_count=previous.tool_count + item.tool_count,
                provider_ids=tuple(sorted(set(previous.provider_ids) | set(item.provider_ids))),
                tags=tuple(sorted(set(previous.tags) | set(item.tags))),
            )
        return tuple(merged[key] for key in sorted(merged))

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
        """Apply HOT controls shared by the Agent and Planner prompt pipeline."""

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
        identity: ConversationIdentity,
        profile: UserProfileSnapshot,
        content: str,
        sender: OutboundSender,
        *,
        autonomous: bool = False,
        runtime_snapshot: RuntimeConfigSnapshot | None = None,
        visual_observation: VisualObservation | None = None,
        visual_input_present: bool = False,
        visual_failure: bool = False,
        planned_turn: PlannedTurn | None = None,
        turn_token: TurnToken | None = None,
    ) -> int:
        """Run one ordered Agent turn and return the sent message count."""

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
                result = await sender.send(OutboundMessage(text=reply))
                await self._record_outbound(inbound, reply, result)
                return 1

            source_display_requested = self._source_policy.requested(content)
            planner_emoji_only = bool(
                planned_turn is not None
                and planned_turn.plan.emoji.is_exclusive
                and planned_turn.plan.tool_mode is ToolMode.NONE
            )
            fallback_plan = planned_turn is not None and planned_turn.fallback_used
            if (
                planned_turn is not None
                and planned_turn.plan.reason_code in _PLANNER_FAIL_CLOSED_REASONS
                and not planner_emoji_only
            ):
                self._record_memory_mutation_turn_outcome("planner_fail_closed")
                logger.warning(
                    "planner_fallback_fail_closed conversation_hash=%s reason=%s",
                    identifier_hash(identity.key) or "missing",
                    planned_turn.plan.reason_code.value,
                )
                result = await sender.send(OutboundMessage(text=_PLANNER_FAIL_CLOSED_MESSAGE))
                await self._record_outbound(inbound, _PLANNER_FAIL_CLOSED_MESSAGE, result)
                return 1
            if planner_emoji_only:
                messages: tuple[ChatMessage, ...] = ()
                visible_event_ids: frozenset[int] = frozenset()
                memory_turn_id = ""
                automatic_memory_exposures: tuple[MemoryExposure, ...] = ()
                memory_intent: MemoryQueryIntent | None = None
            else:
                (
                    messages,
                    visible_event_ids,
                    memory_turn_id,
                    automatic_memory_exposures,
                    memory_intent,
                ) = await self._build_messages(
                    inbound,
                    identity,
                    profile,
                    content,
                    runtime_config,
                    visual_observation=visual_observation,
                    visual_failure=visual_failure,
                    planned_turn=planned_turn,
                    turn_origin=(
                        TurnOrigin.AUTONOMOUS_GROUP if autonomous else TurnOrigin.USER_MESSAGE
                    ),
                )
            memory_access = (
                planned_turn.plan.memory_context.access
                if planned_turn is not None
                else MemoryAccessMode.AUTOMATIC
            )
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
            scheduled_automation_allowed = bool(
                scheduled_automation_intent
                and not fallback_plan
                and memory_access is not MemoryAccessMode.MUTATION
            )
            if scheduled_automation_allowed:
                messages = (
                    *messages,
                    ChatMessage(
                        role="system",
                        content=(
                            "当前消息可能涉及未来触发任务，automation_create 已作为候选能力提供。"
                            "请根据用户的真实意图自行决定是否创建自动化；如果只是当前查询、"
                            "列举、讨论或无需持久化，则不要创建。可以使用本轮其他已授权工具。"
                            "只有 automation_create 返回 confirmation=persisted 和真实 "
                            "automation_id 后，才能声称任务已经创建。"
                        ),
                    ),
                )
            if not planner_emoji_only:
                messages = _with_memory_mutation_contract(messages, memory_access)
            gateway = (
                cast(OneBotToolGateway, sender)
                if callable(getattr(sender, "call_api", None))
                else None
            )
            reply_target_control = (
                ReplyTargetControl(visible_event_ids=visible_event_ids)
                if not planner_emoji_only
                else None
            )
            reply_effects: list[ReplyEffect] = []
            if planned_turn is not None and planned_turn.plan.emoji.mode is not EmojiReplyMode.NONE:
                reply_effects.append(
                    PendingReplyEffect(
                        mode=planned_turn.plan.emoji.mode,
                        placement=planned_turn.plan.emoji.placement,
                        goal=planned_turn.plan.emoji.goal,
                        emotion=planned_turn.plan.emoji.emotion,
                        explicit_request=(
                            planned_turn.plan.emoji.intent is EmojiIntent.EXPLICIT_REQUEST
                        ),
                        source="planner",
                    )
                )
            planner_scopes_explicit = bool(
                planned_turn is not None and planned_turn.plan.tool_selection_explicit
            )
            if self._memory_context is not None:
                self._memory_context.metrics.record_access(memory_access)
            planner_tool_groups = (
                frozenset(planned_turn.plan.tool_selection.scope_ids)
                if planned_turn is not None
                else frozenset(group.value for group in ToolGroup)
            )
            planner_tool_groups = _initial_scopes_for_memory_access(
                memory_access,
                planner_tool_groups,
            )
            tool_groups = planner_tool_groups
            if scheduled_automation_allowed and (planned_turn is None or planner_scopes_explicit):
                tool_groups = frozenset((*tool_groups, ToolGroup.AUTOMATION.value))
            web_route = self._web_router.select(content, runtime_config.web.mode)
            web_scope_authorized = not planner_scopes_explicit or any(
                scope == ToolGroup.WEB.value or scope.startswith(f"{ToolGroup.WEB.value}.")
                for scope in tool_groups
            )
            if web_route is not None and web_scope_authorized:
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
            runtime = ToolRuntime(
                inbound=inbound,
                gateway=gateway,
                allow_generic_onebot=(
                    not autonomous
                    and not visual_input_present
                    and not fallback_plan
                    and inbound.sender.user_id in self._settings.superusers
                ),
                allow_admin_actions=(
                    not autonomous
                    and not visual_input_present
                    and not fallback_plan
                    and inbound.sender.user_id in self._settings.superusers
                ),
                allow_automation=(
                    not autonomous and not visual_input_present and not fallback_plan
                ),
                conversation_key=identity.key,
                trigger_message_id=inbound.message_id,
                source_display_requested=source_display_requested,
                actor_user_id=inbound.sender.user_id,
                actor_is_superuser=inbound.sender.user_id in self._settings.superusers,
                current_group_id=inbound.group_id,
                mentioned_user_ids=inbound.mentioned_user_ids,
                runtime_config=runtime_config,
                origin=(TurnOrigin.AUTONOMOUS_GROUP if autonomous else TurnOrigin.USER_MESSAGE),
                tool_mode=(
                    ToolMode.INHERIT
                    if scheduled_automation_allowed
                    or (
                        memory_access in {MemoryAccessMode.TOOL, MemoryAccessMode.MUTATION}
                        and planned_turn is not None
                        and planned_turn.plan.tool_mode is ToolMode.NONE
                    )
                    else (
                        planned_turn.plan.tool_mode
                        if planned_turn is not None
                        else ToolMode.INHERIT
                    )
                ),
                tool_groups=tool_groups,
                turn_token=turn_token,
                reply_effects=reply_effects,
                reply_target_control=reply_target_control,
                voice_tool_authorized=(
                    planned_turn is not None
                    and planned_turn.plan.voice.agent_tool is VoiceAgentToolPolicy.REQUIRED
                ),
                planner_scopes_explicit=planner_scopes_explicit,
                planner_tool_groups=planner_tool_groups,
                selection_query=content,
                planner_intent=(planned_turn.plan.intent if planned_turn is not None else ""),
                scheduled_automation_intent=scheduled_automation_allowed,
                max_model_requests_override=(1 if fallback_plan else None),
                native_web_fallback=bool(
                    web_route is not None and web_route.provider is WebProvider.TAVILY
                ),
                web_route=web_route,
                memory_turn_id=memory_turn_id,
                memory_exposures=automatic_memory_exposures,
                memory_intent=memory_intent,
                memory_access=memory_access,
                planner_fallback=fallback_plan,
            )
            if planner_emoji_only:
                response_text = ""
            elif turn_token is not None:
                async with self._turn_coordinator.track(turn_token, "generation"):
                    completed_agent = await self._run_agent(identity.key, messages, runtime)
            else:
                completed_agent = await self._run_agent(identity.key, messages, runtime)
            if not planner_emoji_only:
                agent_result = completed_agent.result
                attributed_exposures = completed_agent.memory_exposures
                response_text = agent_result.text
                if agent_result.native_tool_events:
                    native_response = recover_native_web_response(
                        events=agent_result.native_tool_events,
                        citations=agent_result.citations,
                        answer_text=agent_result.text,
                    )
                    await self._web_sources.save_response(
                        conversation_key=identity.key,
                        trigger_message_id=inbound.message_id,
                        provider="deepseek_native",
                        response=native_response,
                        max_runs=runtime_config.web.source_max_runs_per_conversation,
                    )
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
                            attributed_exposures = completed_agent.memory_exposures
                            response_text = agent_result.text
            sources = await self._web_sources.for_trigger(
                conversation_key=identity.key,
                trigger_message_id=inbound.message_id,
            )
            reply_to_message_id = await self._resolve_reply_target(
                inbound=inbound,
                conversation_key=identity.key,
                planned_turn=planned_turn,
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
                        preparation = await self._emoji_effects.prepare(
                            effect,
                            inbound=inbound,
                            response_text=rendered,
                            runtime=runtime_config,
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
                planned_turn is not None
                and turn_token is not None
                and self._speech_effects is not None
            ):
                prepared_voice = await self._speech_effects.prepare(
                    inbound=inbound,
                    response_text=rendered,
                    runtime=runtime_config,
                    token=turn_token,
                    mode=planned_turn.plan.voice.mode,
                    style_hint=(
                        queued_voice.style_hint
                        if queued_voice is not None
                        else planned_turn.plan.voice.style_hint
                    ),
                    language_hint=(
                        queued_voice.language_hint
                        if queued_voice is not None
                        else planned_turn.plan.voice.language.value
                    ),
                    profile_id=queued_voice.profile_id if queued_voice is not None else "",
                )
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
            if planned_turn is not None and turn_token is not None:
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
                    if message.media and self._emoji_effects is not None:
                        await self._emoji_effects.record_failure(
                            message,
                            source="reply_effect",
                        )
                    if message.media and self._speech_effects is not None:
                        await self._speech_effects.record_failure(message)

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
                if prepared_voice is not None:
                    after = (*after, prepared_voice.message)
                suppress_text = bool(prepared_effects) and any(
                    effect.mode is EmojiReplyMode.EMOJI_ONLY
                    or effect.placement is EmojiPlacement.ONLY
                    for effect, _message in prepared_effects
                )
                if prepared_voice is not None:
                    suppress_text = suppress_text or prepared_voice.suppress_text

                sequence = await self._reply_sequence.send(
                    text=rendered,
                    plan=planned_turn.plan,
                    runtime=runtime_config,
                    token=turn_token,
                    sender=sender,
                    record_outbound=record_chunk,
                    record_failure=record_failure,
                    before_send=before_send,
                    recover_failure=recover_failure,
                    before_messages=before,
                    after_messages=after,
                    suppress_text=suppress_text,
                    reply_to_message_id=reply_to_message_id,
                )
                if not planner_emoji_only and agent_body_delivered and not sequence.cancelled:
                    self._enqueue_memory_attribution(
                        runtime=runtime,
                        user_question=content,
                        final_response=attribution_response_text,
                        exposures=attributed_exposures,
                    )
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
                    receipt = await sender.send(outbound)
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
                            receipt = await sender.send(outbound)
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
                        fallback_receipt = await sender.send(fallback)
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
                    receipt = await sender.send(OutboundMessage(text=source_text))
                    await self._record_outbound(inbound, source_text, receipt)
                    sent_count += 1
            if not planner_emoji_only and agent_body_delivered:
                self._enqueue_memory_attribution(
                    runtime=runtime,
                    user_question=content,
                    final_response=attribution_response_text,
                    exposures=attributed_exposures,
                )
            return sent_count

    def _enqueue_memory_attribution(
        self,
        *,
        runtime: ToolRuntime,
        user_question: str,
        final_response: str,
        exposures: tuple[MemoryExposure, ...],
    ) -> bool:
        config = runtime.runtime_config
        if (
            self._memory_attribution is None
            or config is None
            or not config.memory.usage_attribution_enabled
            or not runtime.memory_turn_id
            or runtime.memory_intent is None
            or runtime.planner_fallback
            or runtime.origin not in {TurnOrigin.USER_MESSAGE, TurnOrigin.AUTONOMOUS_GROUP}
            or not final_response.strip()
            or not exposures
        ):
            return False
        return self._memory_attribution.enqueue(
            MemoryAttributionJob(
                turn_id=runtime.memory_turn_id,
                user_id=runtime.inbound.sender.user_id,
                group_id=runtime.inbound.group_id,
                user_question=user_question,
                final_response=final_response,
                intent=runtime.memory_intent,
                exposures=exposures,
                runtime=config,
                enqueued_at=datetime.now(UTC),
            )
        )

    async def _build_messages(
        self,
        inbound: InboundMessage,
        identity: ConversationIdentity,
        profile: UserProfileSnapshot,
        content: str,
        runtime: RuntimeConfigSnapshot,
        *,
        visual_observation: VisualObservation | None = None,
        visual_failure: bool = False,
        planned_turn: PlannedTurn | None = None,
        turn_origin: TurnOrigin = TurnOrigin.USER_MESSAGE,
    ) -> tuple[
        tuple[ChatMessage, ...],
        frozenset[int],
        str,
        tuple[MemoryExposure, ...],
        MemoryQueryIntent | None,
    ]:
        context = await self._context_assembler.assemble(
            inbound=inbound,
            identity=identity,
            profile=profile,
            content=content,
            runtime=runtime,
            planner_intent="",
            memory_mode=(
                _automatic_memory_mode(
                    planned_turn.plan.memory_context.access,
                    planned_turn.plan.memory_context.mode,
                )
                if planned_turn is not None
                else MemoryContextMode.LEXICAL
            ),
            self_recall=(
                planned_turn.plan.memory_context.self_recall if planned_turn is not None else False
            ),
            memory_intent=(
                planned_turn.plan.memory_context.to_query_intent()
                if planned_turn is not None
                else None
            ),
            requested_limit=(
                planned_turn.plan.memory_context.requested_count
                if planned_turn is not None
                else None
            ),
            turn_origin=turn_origin.value,
        )
        return (
            self._prompt_composer.compose(
                inbound=inbound,
                context=context,
                runtime=runtime,
                visual_observation=visual_observation,
                visual_failure=visual_failure,
                planned_turn=planned_turn,
            ),
            context.visible_event_ids,
            context.memory_turn_id,
            context.memory_exposures,
            context.memory_intent,
        )

    async def _resolve_reply_target(
        self,
        *,
        inbound: InboundMessage,
        conversation_key: str,
        planned_turn: PlannedTurn | None,
        control: ReplyTargetControl | None,
    ) -> str | None:
        source = "none"
        event_id: int | None = None
        if control is not None and control.override_applied:
            source = "agent"
            event_id = control.event_id
        elif planned_turn is not None and planned_turn.plan.reply_to_event_id is not None:
            source = "planner"
            event_id = planned_turn.plan.reply_to_event_id
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
                allowed_capabilities=runtime.tool_groups,
                max_tool_calls=config.agent.max_tool_calls,
                max_model_requests=(
                    min(
                        config.agent.max_model_requests,
                        runtime.max_model_requests_override,
                    )
                    if runtime.max_model_requests_override is not None
                    else config.agent.max_model_requests
                ),
                force_tavily_fallback=runtime.native_web_fallback,
                web_route=runtime.web_route,
            ),
            backend,
        )
        return _CompletedAgentRun(
            result=result,
            memory_exposures=exposure_registry.snapshot(),
        )

    async def generate_external_reply(
        self,
        *,
        event: EventRecord,
        authorization_user_id: str,
        conversation_key: str,
        runtime: RuntimeConfigSnapshot,
        agent_intent: str,
        planned_turn: PlannedTurn,
        turn_token: TurnToken,
    ) -> AgentRunResult:
        """Generate one tool-free reply for a persisted external event.

        The synthetic envelope below is authority metadata only.  It is never
        appended to the ledger and the prompt identifies the trigger as an
        untrusted external event rather than a QQ user message.
        """

        context = await self._context_assembler.assemble_external(
            event=event,
            authorization_user_id=authorization_user_id,
            runtime=runtime,
            agent_intent=agent_intent,
        )
        messages = self._prompt_composer.compose_external(
            context=context,
            runtime=runtime,
            source_plugin_id=event.source_plugin_id or "",
            external_source=event.external_source or "external",
            event_type=event.external_event_type or "event",
            agent_intent=agent_intent,
            planned_turn=planned_turn,
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
            tool_mode=ToolMode.NONE,
            tool_groups=frozenset(),
            turn_token=turn_token,
            planner_scopes_explicit=True,
            planner_tool_groups=frozenset(),
            selection_query=event.content,
            planner_intent=planned_turn.plan.intent,
            selected_tool_names=frozenset(),
            max_model_requests_override=min(2, runtime.agent.max_model_requests),
        )
        completed = await self._run_agent(conversation_key, messages, tool_runtime)
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
        """Discover selected lazy scopes, then locally select and optionally rerank tools."""

        config = runtime.runtime_config
        assert config is not None
        if self._tool_artifacts is not None and config.tooling is not None:
            self._tool_artifacts.configure_retention(
                config.tooling.result_artifact_retention_seconds
            )
        if runtime.tool_mode is ToolMode.NONE:
            return replace(runtime, selected_tool_names=frozenset())
        if runtime.planner_scopes_explicit and not runtime.tool_groups:
            return replace(runtime, selected_tool_names=frozenset())
        registry = self._build_tool_registry(runtime, web_was_used=False)
        scopes = tuple(sorted(runtime.tool_groups))
        for provider in registry.providers():
            prepare = getattr(provider, "prepare_scopes", None)
            if callable(prepare):
                await prepare(scopes, runtime)
        catalog = registry.catalog(runtime)
        known_scopes = {scope.scope_id for scope in catalog.scopes}

        # An explicit Planner scope is a complete, intentional capability
        # package. The backend applies the schema-token safety budget, but the
        # compact inherited limit must not truncate this package.
        if runtime.planner_scopes_explicit:
            return runtime

        # Inherited scopes express backend authority, not a request to expose
        # the whole catalog. Only deterministic additions beyond the Planner's
        # inherited set may prioritize a scope during local relevance ranking.
        planner_groups = runtime.planner_tool_groups or frozenset()
        inherited_priority_scopes = tuple(
            scope for scope in sorted(runtime.tool_groups - planner_groups) if scope in known_scopes
        )
        if (
            any(scope.startswith("mcp.") for scope in inherited_priority_scopes)
            and "mcp" in known_scopes
            and "mcp" not in inherited_priority_scopes
        ):
            inherited_priority_scopes = (*inherited_priority_scopes, "mcp")

        mcp = config.mcp
        mode = mcp.tool_selection_mode if mcp is not None else "catalog"
        has_mcp_tools = any(
            item.descriptor.trust_source is CapabilityTrustSource.MCP for item in catalog.entries
        )
        tooling = config.tooling
        global_limit = tooling.selected_tool_limit if tooling is not None else None
        initial_limit = min(
            _INHERITED_RELATED_TOOL_LIMIT,
            global_limit if global_limit is not None else _INHERITED_RELATED_TOOL_LIMIT,
        )
        candidates = list(
            self._tool_selector.select(
                catalog,
                scopes=inherited_priority_scopes,
                user_request=runtime.selection_query,
                planner_intent=runtime.planner_intent,
                limit=_INHERITED_CANDIDATE_POOL_LIMIT,
                minimum_score=1,
            ).entries
        )
        if (
            mcp is not None
            and mcp.enabled
            and has_mcp_tools
            and mcp.selected_tool_limit is not None
        ):
            mcp_used = 0
            limited = []
            for candidate in candidates:
                if candidate.descriptor.trust_source is CapabilityTrustSource.MCP:
                    if mcp_used >= mcp.selected_tool_limit:
                        continue
                    mcp_used += 1
                limited.append(candidate)
            candidates = self._retain_required_tools(
                limited,
                catalog.entries,
                inherited_priority_scopes,
            )
        if mode == "hybrid" and has_mcp_tools and len(candidates) > initial_limit:
            candidates = list(
                await self._tool_reranker.rerank(
                    tuple(candidates),
                    user_request=runtime.selection_query,
                    planner_intent=runtime.planner_intent,
                    limit=initial_limit,
                    required_scope_ids=inherited_priority_scopes,
                )
            )
        else:
            candidates = self._retain_required_tools(
                candidates[:initial_limit],
                catalog.entries,
                inherited_priority_scopes,
            )
        return replace(
            runtime,
            selected_tool_names=frozenset(item.descriptor.model_name for item in candidates),
        )

    @staticmethod
    def _retain_required_tools(
        selected: list[UnifiedToolCatalogEntry],
        available: tuple[UnifiedToolCatalogEntry, ...],
        scopes: tuple[str, ...],
    ) -> list[UnifiedToolCatalogEntry]:
        selected_names = {item.descriptor.model_name for item in selected}
        required_scopes = set(scopes)
        for item in available:
            required = item.descriptor.exposure is CapabilityExposure.DIRECT_ALWAYS or bool(
                required_scopes.intersection(item.bundle_scope_ids)
            )
            if required and item.descriptor.model_name not in selected_names:
                selected.append(item)
                selected_names.add(item.descriptor.model_name)
        return selected

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
