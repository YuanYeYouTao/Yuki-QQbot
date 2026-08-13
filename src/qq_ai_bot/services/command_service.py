"""Deterministic `/ai` command execution, separate from the chat message pipeline."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from qq_ai_bot import __version__
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import AdminActor
from qq_ai_bot.admin.permission_catalog import PermissionCatalogService
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.automation.worker import AutomationWorker
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.conversations import ConversationIdentity, ScopeType
from qq_ai_bot.domain.messages import InboundMessage, OutboundMessage
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.emoji.admin import EmojiAdminService
from qq_ai_bot.mcp.admin import MCPCommandHandler
from qq_ai_bot.memory.rebuild.service import MemoryRebuildService
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.model_runtime.repository import ModelInvocationRepository
from qq_ai_bot.persistence.repositories import (
    ConversationRepository,
    PeopleRepository,
)
from qq_ai_bot.planner.observability import PlannerObservability
from qq_ai_bot.planner.repository import PlannerRepository
from qq_ai_bot.plugin_host.command_adapter import PluginCommandAdapter
from qq_ai_bot.plugin_host.direct_command_router import DirectCommandMatch
from qq_ai_bot.services.admin.config_admin import ConfigAdminService
from qq_ai_bot.services.admin.group_admin import GroupAdminService
from qq_ai_bot.services.admin.memory_admin import MemoryAdminService
from qq_ai_bot.services.admin.preference_admin import PreferenceAdminService
from qq_ai_bot.services.admin.private_access_admin import PrivateAccessAdminService
from qq_ai_bot.services.admin.relationship_admin import RelationshipAdminService
from qq_ai_bot.services.automation_commands import AutomationCommandHandler
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.config_commands import ConfigCommandHandler
from qq_ai_bot.services.media_resolver import OneBotMediaGateway
from qq_ai_bot.services.policies import CommandName, command_requires_superuser
from qq_ai_bot.services.profile_commands import ProfileCommandHandler
from qq_ai_bot.services.turn_coordinator import ConversationTurnCoordinator
from qq_ai_bot.services.vision_service import VisionService
from qq_ai_bot.speech.admin import SpeechAdminService

_NUMERIC_PLATFORM_ID = re.compile(r"[1-9][0-9]{4,19}")


@dataclass(frozen=True, slots=True)
class CommandExecution:
    """Text and lifecycle effects produced by one deterministic command."""

    text: str
    record_reply: bool = True
    reset_after_reply: bool = False
    outbound: OutboundMessage | None = None


class CommandService:
    """Execute deterministic commands without creating a second AI routing layer."""

    def __init__(
        self,
        *,
        settings: Settings,
        conversations: ConversationRepository,
        people: PeopleRepository,
        memories: MemoryFactService,
        concurrency: ConcurrencyManager,
        onebot_connected: Callable[[], bool],
        runtime_config: RuntimeConfigService,
        relationship_admin: RelationshipAdminService,
        memory_admin: MemoryAdminService,
        preference_admin: PreferenceAdminService,
        group_admin: GroupAdminService,
        private_access_admin: PrivateAccessAdminService,
        config_admin: ConfigAdminService,
        permission_catalog: PermissionCatalogService,
        vision_service: VisionService | None = None,
        automation_service: AutomationService | None = None,
        automation_repository: AutomationRepository | None = None,
        automation_worker: AutomationWorker | None = None,
        turn_coordinator: ConversationTurnCoordinator | None = None,
        planner_observability: PlannerObservability | None = None,
        planner_repository: PlannerRepository | None = None,
        plugin_commands: PluginCommandAdapter | None = None,
        emoji_admin: EmojiAdminService | None = None,
        speech_admin: SpeechAdminService | None = None,
        model_invocations: ModelInvocationRepository | None = None,
        mcp_commands: MCPCommandHandler | None = None,
        memory_rebuild: MemoryRebuildService | None = None,
    ) -> None:
        self._settings = settings
        self._conversations = conversations
        self._people = people
        self._concurrency = concurrency
        self._onebot_connected = onebot_connected
        self._runtime_config = runtime_config
        self._group_admin = group_admin
        self._private_access_admin = private_access_admin
        self._vision = vision_service
        self._automation_repository = automation_repository
        self._automation_worker = automation_worker
        self._turn_coordinator = turn_coordinator
        self._planner_observability = planner_observability
        self._planner_repository = planner_repository
        self._plugin_commands = plugin_commands
        self._emoji_admin = emoji_admin
        self._speech_admin = speech_admin
        self._model_invocations = model_invocations
        self._mcp_commands = mcp_commands
        self._memory_rebuild = memory_rebuild
        self._profile_commands = ProfileCommandHandler(
            people=people,
            memories=memories,
            memory_admin=memory_admin,
            preference_admin=preference_admin,
            relationship_admin=relationship_admin,
            memory_rebuild=memory_rebuild,
            bot_display_name=settings.bot_display_name,
        )
        self._config_commands = ConfigCommandHandler(
            config_admin=config_admin,
            permission_catalog=permission_catalog,
        )
        self._automation_commands = AutomationCommandHandler(
            settings=settings,
            automation_service=automation_service,
        )

    async def execute_direct_plugin(
        self,
        message: InboundMessage,
        identity: ConversationIdentity,
        match: DirectCommandMatch,
    ) -> CommandExecution:
        if self._plugin_commands is None:
            return CommandExecution("插件直达命令当前不可用。")
        runtime = await self._runtime_config.snapshot(
            user_id=message.sender.user_id,
            group_id=message.group_id,
        )
        text = await self._plugin_commands.execute_direct(
            message=message,
            identity=identity,
            match=match,
            runtime=runtime,
        )
        return CommandExecution(text)

    @staticmethod
    def may_write(command: CommandName, argument: str) -> bool:
        """Conservatively close deterministic writes on every image-bearing turn."""

        if command in {
            CommandName.ON,
            CommandName.OFF,
            CommandName.GROUP,
            CommandName.PRIVATE,
            CommandName.FORGETME,
        }:
            return True
        operation = argument.split(maxsplit=1)[0].casefold() if argument.strip() else ""
        if command is CommandName.MEMORY:
            normalized = argument.casefold().split()
            return not (
                operation in {"", "list", "evidence", "search"}
                or normalized[:2] == ["index", "status"]
            )
        if command is CommandName.PREFERENCE:
            return operation not in {"", "list"}
        if command is CommandName.AFFECTION:
            return operation not in {"", "show", "history"}
        if command is CommandName.CONFIG:
            return operation not in {"", "list", "get", "history"}
        if command is CommandName.AUTOMATION:
            return operation not in {"", "list", "show", "history"}
        if command is CommandName.PLUGIN:
            return operation not in {"", "list", "show", "permissions", "doctor"}
        if command is CommandName.EMOJI:
            return operation not in {"", "list", "show", "stats", "doctor"}
        if command is CommandName.VOICE:
            return operation in {"use", "reload", "cache", "test"}
        if command is CommandName.MCP:
            return operation in {"refresh", "reconnect", "enable", "disable", "doctor", "call"}
        return False

    async def execute(
        self,
        command: CommandName,
        message: InboundMessage,
        identity: ConversationIdentity,
        profile: UserProfileSnapshot,
        argument: str,
        started: float,
        gateway: OneBotMediaGateway | None = None,
    ) -> CommandExecution:
        is_superuser = message.sender.user_id in self._settings.superusers
        actor = AdminActor(
            user_id=message.sender.user_id,
            is_superuser=is_superuser,
            trigger_message_id=message.message_id,
            conversation_key=identity.key,
            current_group_id=message.group_id,
            mentioned_user_ids=message.mentioned_user_ids,
            current_message_text=message.text,
            bot_user_id=message.bot_user_id,
        )
        record_reply = command is not CommandName.FORGETME
        reset_after_reply = False
        if command_requires_superuser(command) and not is_superuser:
            text = "权限不足：该命令仅限超级管理员。"
        elif command is CommandName.HELP:
            text = self._help_text()
        elif command is CommandName.NEW:
            text = "已开始新的当前场景上下文；永久聊天账本和人物记忆仍然保留。"
            reset_after_reply = True
        elif command is CommandName.STATUS:
            count = await self._conversations.count_messages(identity)
            pending_restart = await self._runtime_config.pending_restart_count()
            vision_busy = self._vision is not None and self._vision.busy
            vision_queue_depth = self._vision.queue_depth if self._vision is not None else 0
            vision_running = self._vision.running_count if self._vision is not None else 0
            emoji_status = await self._emoji_admin.status() if self._emoji_admin is not None else {}
            emoji_counts = emoji_status.get("counts", {})
            automation_count = (
                await self._automation_repository.active_count()
                if self._automation_repository is not None
                else 0
            )
            automation_last_run = (
                await self._automation_repository.latest_run_at()
                if self._automation_repository is not None
                else None
            )
            automation_next_run = (
                await self._automation_repository.next_due_at()
                if self._automation_repository is not None
                else None
            )
            automation_worker_status = (
                "运行中"
                if self._automation_worker is not None and self._automation_worker.running
                else "未运行"
            )
            automation_last_text = automation_last_run.isoformat() if automation_last_run else "无"
            automation_next_text = automation_next_run.isoformat() if automation_next_run else "无"
            planner_metrics = (
                self._planner_observability.snapshot()
                if self._planner_observability is not None
                else None
            )
            latest_planner = (
                await self._planner_repository.latest()
                if self._planner_repository is not None
                else None
            )
            planner_model = (
                latest_planner.planner_model
                if latest_planner is not None and latest_planner.planner_model
                else "尚无规划记录"
            )
            planner_latency = (
                planner_metrics.last_latency_seconds
                if planner_metrics and planner_metrics.last_latency_seconds is not None
                else "无"
            )
            speech_status: dict[str, object] = {}
            if self._speech_admin is not None:
                speech_status = await self._speech_admin.status_data(
                    await self._runtime_config.snapshot(
                        user_id=message.sender.user_id,
                        group_id=message.group_id,
                    )
                )
            mcp_health = self._mcp_commands.health() if self._mcp_commands is not None else None
            mcp_last_call = (
                mcp_health.last_call_at.isoformat()
                if mcp_health is not None and mcp_health.last_call_at is not None
                else "无"
            )
            mcp_last_error = (
                mcp_health.last_error_category
                if mcp_health is not None and mcp_health.last_error_category is not None
                else "无"
            )
            text = (
                f"OneBot 连接：{'已连接' if self._onebot_connected() else '未连接'}\n"
                f"模型：{self._settings.llm_model or '未配置'}\n"
                f"视觉功能：{'已启用' if self._settings.vision_enabled else '未启用'}\n"
                f"视觉模型：{self._settings.vision_model or '未配置'}\n"
                f"视觉请求繁忙：{'是' if vision_busy else '否'}\n"
                f"视觉排队/运行：{vision_queue_depth}/{vision_running}\n"
                f"表情系统：{'已启用' if self._settings.emoji_enabled else '未启用'}\n"
                f"表情 Worker：{'运行中' if emoji_status.get('worker_running') else '未运行'}\n"
                f"表情候选/已采用/待处理："
                f"{emoji_counts.get('candidate', 0)}/"
                f"{emoji_counts.get('adopted', 0)}/"
                f"{emoji_counts.get('jobs_pending', 0)}\n"
                f"当前切点后的事件数：{count}\n"
                f"请求处理中：{'是' if self._concurrency.is_processing(identity.key) else '否'}\n"
                "Planner：固定启用\n"
                f"Planner 模型：{planner_model}\n"
                f"活动 Planner：{planner_metrics.active_requests if planner_metrics else 0}\n"
                f"最近 Planner 延迟："
                f"{planner_latency}\n"
                f"最近 Planner 决策时间："
                f"{latest_planner.created_at.isoformat() if latest_planner else '无'}\n"
                f"待重启配置数：{pending_restart}\n"
                f"自动化：{'已启用' if self._settings.automation_enabled else '未启用'}\n"
                f"自动化 Worker：{automation_worker_status}\n"
                f"活跃自动化任务：{automation_count}\n"
                f"最近自动化执行：{automation_last_text}\n"
                f"最近待执行时间：{automation_next_text}\n"
                f"本地语音：{'已启用' if speech_status.get('enabled') else '未启用'}\n"
                f"语音 Provider：{speech_status.get('provider', '未初始化')}\n"
                f"语音 Worker："
                f"{'已就绪' if speech_status.get('worker_ready') else '未就绪'}\n"
                f"默认声线：{speech_status.get('default_profile') or '未设置'}\n"
                f"可用语音风格：{speech_status.get('style_count', 0)}\n"
                f"语音队列深度：{speech_status.get('queue_depth', 0)}\n"
                f"最近语音生成：{speech_status.get('last_generation_at') or '无'}\n"
                f"最近语音耗时："
                f"{speech_status.get('last_generation_latency_seconds') or '无'}\n"
                f"MCP：{'已启用' if mcp_health and mcp_health.enabled else '未启用'}\n"
                f"MCP Server：{mcp_health.configured_servers if mcp_health else 0}\n"
                f"MCP 已连接：{mcp_health.connected_servers if mcp_health else 0}\n"
                f"MCP 缓存工具：{mcp_health.cached_tools if mcp_health else 0}\n"
                f"MCP 最近调用：{mcp_last_call}\n"
                f"MCP 最近错误：{mcp_last_error}\n"
                f"服务版本：{__version__}"
            )
        elif command is CommandName.STOP:
            cancelled = await self._concurrency.cancel(identity.key)
            if self._turn_coordinator is not None:
                cancelled = (
                    await self._turn_coordinator.cancel_interruptible(
                        self._turn_coordinator.key_for(message)
                    )
                    or cancelled
                )
            text = "已取消当前 AI 请求。" if cancelled else "当前没有正在处理的 AI 请求。"
        elif command in {CommandName.ON, CommandName.OFF}:
            if message.scope_type is not ScopeType.GROUP or message.group_id is None:
                text = "该命令只能在群聊中使用。"
            else:
                enabled = command is CommandName.ON
                if enabled:
                    await self._group_admin.enable_current_group(actor, message.group_id)
                else:
                    await self._group_admin.disable_current_group(actor, message.group_id)
                text = "已启用当前群。" if enabled else "已停用当前群。"
        elif command in {CommandName.PRIVATE, CommandName.GROUP}:
            parsed = self._parse_access_switch(argument)
            if parsed is None:
                noun = "QQ号" if command is CommandName.PRIVATE else "群号"
                text = f"格式错误，请使用 /ai {command.value} <{noun}> on|off。"
            else:
                target_id, enabled = parsed
                try:
                    if command is CommandName.PRIVATE:
                        if enabled:
                            await self._private_access_admin.enable_user(actor, target_id)
                        else:
                            await self._private_access_admin.disable_user(actor, target_id)
                        text = (
                            "已开启指定 QQ 用户的私聊权限。"
                            if enabled
                            else "已关闭指定 QQ 用户的私聊权限。"
                        )
                    else:
                        if enabled:
                            await self._group_admin.enable_current_group(actor, target_id)
                        else:
                            await self._group_admin.disable_current_group(actor, target_id)
                        text = f"已{'启用' if enabled else '停用'}群 {target_id}。"
                except ValueError as exc:
                    text = str(exc)
        elif command is CommandName.PING:
            text = f"pong ({(time.perf_counter() - started) * 1000:.1f} ms)"
        elif command is CommandName.WHOAMI:
            text = await self._profile_commands.whoami(message, profile, argument)
        elif command is CommandName.FORGETME:
            if argument:
                text = "该命令不接受参数，只能删除发送者本人数据。"
            else:
                if self._memory_rebuild is not None:
                    await self._memory_rebuild.forget_person(message.sender.user_id)
                deleted = await self._people.delete_person(message.sender.user_id)
                text = (
                    "已彻底删除与你 QQ 号关联的人物、关系分数、记忆、成员关系和可归属聊天事件。"
                    if deleted
                    else "没有找到与你 QQ 号关联的数据。"
                )
        elif command is CommandName.MEMORY:
            text = await self._profile_commands.memory(
                actor=actor,
                argument=argument,
            )
        elif command is CommandName.PREFERENCE:
            text = await self._profile_commands.preference(
                actor=actor,
                argument=argument,
            )
        elif command is CommandName.AFFECTION:
            text = await self._profile_commands.affection(
                actor=actor,
                argument=argument,
            )
        elif command is CommandName.CAPABILITIES:
            text = self._config_commands.capabilities(message, argument)
        elif command is CommandName.CONFIG:
            text = await self._config_commands.config(actor, argument)
        elif command is CommandName.AUTOMATION:
            text = await self._automation_commands.execute(
                message=message,
                identity=identity,
                argument=argument,
            )
        elif command is CommandName.PLUGIN:
            if self._plugin_commands is None:
                text = "插件系统当前未启用。"
            else:
                text = await self._plugin_commands.execute(
                    message=message,
                    identity=identity,
                    argument=argument,
                    runtime=await self._runtime_config.snapshot(
                        user_id=message.sender.user_id,
                        group_id=message.group_id,
                    ),
                )
        elif command is CommandName.EMOJI:
            if self._emoji_admin is None:
                text = "表情系统当前未启用。"
            else:
                text = await self._emoji_admin.execute(
                    actor=actor,
                    message=message,
                    argument=argument,
                    gateway=gateway,
                )
        elif command is CommandName.VOICE:
            if self._speech_admin is None:
                text = "本地语音服务未初始化。"
            else:
                try:
                    speech_result = await self._speech_admin.execute(
                        actor=actor,
                        message=message,
                        argument=argument,
                        runtime=await self._runtime_config.snapshot(
                            user_id=message.sender.user_id,
                            group_id=message.group_id,
                        ),
                    )
                except (LookupError, ValueError, PermissionError, RuntimeError, OSError) as exc:
                    text = str(exc)
                else:
                    return CommandExecution(
                        text=speech_result.text,
                        record_reply=record_reply,
                        reset_after_reply=reset_after_reply,
                        outbound=speech_result.outbound,
                    )
        elif command is CommandName.MODEL:
            if argument.strip().casefold() != "stats":
                text = "格式错误，请使用 /ai model stats。"
            elif self._model_invocations is None:
                text = "模型调用统计尚未初始化。"
            else:
                stats = await self._model_invocations.stats()
                by_task = await self._model_invocations.stats_by_task()
                by_profile = await self._model_invocations.stats_by_profile()
                recent_errors = await self._model_invocations.recent_errors(
                    limit=self._settings.model_stats_recent_error_limit
                )
                lines = [
                    f"模型调用：{stats.invocations} 次（成功 {stats.successes}，失败 "
                    f"{stats.failures}）",
                    f"Token：输入 {stats.prompt_tokens}，输出 {stats.completion_tokens}，"
                    f"缓存命中 {stats.cached_prompt_tokens}，合计 {stats.total_tokens}；"
                    f"缺少 usage {stats.unknown_usage} 次",
                    f"平均延迟：{stats.average_latency_seconds:.3f} 秒",
                    "按任务：",
                ]
                lines.extend(
                    f"- {task.value}: {item.invocations} 次 / {item.total_tokens} Token"
                    for task, item in sorted(by_task.items(), key=lambda pair: pair[0].value)
                )
                lines.append("按档案：")
                lines.extend(
                    f"- {profile_id}: {item.invocations} 次 / {item.total_tokens} Token"
                    for profile_id, item in sorted(by_profile.items())
                )
                lines.append("最近错误：")
                lines.extend(
                    f"- {item.created_at.isoformat()} {item.task.value}/{item.profile_id}: "
                    f"{item.error_category}"
                    for item in recent_errors
                )
                if not recent_errors:
                    lines.append("- 无")
                text = "\n".join(lines)
        elif command is CommandName.MCP:
            if self._mcp_commands is None:
                text = "MCP 子系统尚未初始化"
            else:
                text = await self._mcp_commands.execute(
                    argument,
                    is_superuser=is_superuser,
                )
        else:
            text = "未知命令，请使用 /ai help 查看帮助。"

        return CommandExecution(
            text=text,
            record_reply=record_reply,
            reset_after_reply=reset_after_reply,
        )

    @staticmethod
    def _help_text() -> str:
        return (
            "QQ AI 助手命令：\n"
            "/ai help | new | status | stop | ping | whoami | forgetme\n"
            "/ai memory list|add|update|delete|evidence\n"
            "/ai memory show|explain|history <fact_id>\n"
            "/ai memory conflicts [user <QQ号>] | correct | invalidate | restore\n"
            "/ai memory merge|resolve|doctor|maintenance（超级管理员操作按权限执行）\n"
            "/ai memory self-reflection run（超级管理员立即运行一轮自省）\n"
            "/ai memory dream plan|start|list|status|show|cancel|resume|retry|rollback"
            "（超级管理员）\n"
            "/ai memory search person <QQ号> <query>（超级管理员）\n"
            "/ai memory search group <群号> <query>（超级管理员）\n"
            "/ai memory index status|rebuild（超级管理员）\n"
            "/ai memory embedding status|doctor|retry|rebuild|purge-old（超级管理员）\n"
            "/ai preference list|set|delete\n"
            "/ai affection show [user <QQ号>] | history\n"
            "/ai affection set|adjust|trust user <QQ号> <数值>（超级管理员）\n"
            "/ai capabilities [类别]（查看当前 QQ 的完整权限与可改范围）\n"
            "/ai config list|get|set|unset|history|rollback（超级管理员）\n"
            "/ai automation list|show|pause|resume|cancel|run|history <任务ID>\n"
            "/ai plugin list|show|permissions|approve|enable|disable|doctor|run\n"
            "/ai emoji list|show|adopt|unadopt|reject|ban|pin|reanalyze\n"
            "/ai emoji stats|cleanup|doctor|import\n"
            "/ai voice status|profiles|show|use|styles|test|reload|cache cleanup\n"
            "/ai model stats（超级管理员）\n"
            "/ai mcp list|show|status|tools|search|refresh|reconnect|enable|disable|doctor|call\n"
            "/ai on|off（超级管理员，当前群）\n"
            "/ai group <群号> on|off（超级管理员）\n"
            "/ai private <QQ号> on|off（超级管理员；阻止/恢复私聊）\n"
            "超级管理员可在 memory/preference 操作名后加 user <QQ号>。"
        )

    async def mark_media_sent(self, message: OutboundMessage) -> None:
        if self._speech_admin is not None:
            await self._speech_admin.mark_sent(message)

    @staticmethod
    def _parse_access_switch(argument: str) -> tuple[str, bool] | None:
        parts = argument.casefold().split()
        if len(parts) != 2 or _NUMERIC_PLATFORM_ID.fullmatch(parts[0]) is None:
            return None
        if parts[1] not in {"on", "off"}:
            return None
        return parts[0], parts[1] == "on"
