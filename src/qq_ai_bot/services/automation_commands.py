"""Owner-scoped deterministic automation commands."""

from __future__ import annotations

from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.time.formatting import local_text


class AutomationCommandHandler:
    """Manage persisted automations through their canonical business service."""

    def __init__(
        self,
        *,
        settings: Settings,
        automation_service: AutomationService | None,
    ) -> None:
        self._settings = settings
        self._automation = automation_service

    async def execute(
        self,
        *,
        message: InboundMessage,
        identity: ConversationScope,
        argument: str,
    ) -> str:
        """Manage owner-scoped tasks through the same service used by Agent tools."""

        if self._automation is None or not self._settings.automation_enabled:
            return "自动化功能当前未启用。"
        parts = argument.split()
        operation = parts.pop(0).casefold() if parts else "list"
        if operation == "list":
            if parts:
                return "格式：/ai automation list"
            rows = await self._automation.list_current(message.sender.user_id)
            if not rows:
                return "当前没有运行中或已暂停的自动化任务。\n已结束任务：/ai automation completed"
            timezone = await self._automation.timezone(message.sender.user_id)
            lines = [f"当前任务（{timezone}）："]
            lines.extend(
                f"[ID {row.id}] [{row.status.value}] {row.name}；下次："
                f"{local_text(row.next_run_at, timezone)}"
                for row in rows
            )
            lines.append("已结束任务：/ai automation completed")
            return "\n".join(lines)
        if operation in {"completed", "archive"}:
            if parts:
                return "格式：/ai automation completed"
            rows = await self._automation.list_completed(message.sender.user_id)
            if not rows:
                return "完成历史为空。"
            timezone = await self._automation.timezone(message.sender.user_id)
            lines = [f"完成历史（{timezone}）："]
            lines.extend(
                f"[ID {row.id}] [{row.status.value}] {row.name}；最后运行："
                f"{local_text(row.last_run_at, timezone)}"
                for row in rows
            )
            return "\n".join(lines)
        if len(parts) != 1 or not parts[0].isdigit():
            return "格式：/ai automation show|pause|resume|cancel|run|history <自动化ID>"
        automation_id = int(parts[0])
        try:
            current = await self._automation.require_owned(automation_id, message.sender.user_id)
            if operation == "show":
                timezone = await self._automation.timezone(message.sender.user_id)
                return (
                    f"自动化 ID：{automation_id}\n名称：{current.name}\n"
                    f"状态：{current.status.value}\n时区：{timezone}\n下次："
                    f"{local_text(current.next_run_at, timezone)}\n"
                    f"能力：{', '.join(current.required_capabilities)}"
                )
            if operation == "history":
                history_rows = await self._automation.history(
                    automation_id,
                    creator_user_id=message.sender.user_id,
                )
                if not history_rows:
                    return "该任务暂无执行记录。"
                timezone = await self._automation.timezone(message.sender.user_id)
                return "\n".join(
                    f"[运行 ID {row.id}] [{row.status.value}] "
                    f"{local_text(row.scheduled_for, timezone)}"
                    + (f"；{row.error_category}" if row.error_category else "")
                    for row in history_rows
                )
            if operation == "pause":
                changed = await self._automation.pause(
                    automation_id,
                    inbound=message,
                    conversation_key=identity.key,
                )
                return "任务已暂停。" if changed else "任务状态没有改变。"
            if operation == "resume":
                changed = await self._automation.resume(
                    automation_id,
                    inbound=message,
                    conversation_key=identity.key,
                )
                return "任务已恢复。" if changed else "该任务不能恢复。"
            if operation == "cancel":
                changed = await self._automation.cancel(
                    automation_id,
                    inbound=message,
                    conversation_key=identity.key,
                )
                return "任务已取消。" if changed else "任务状态没有改变。"
            if operation == "run":
                changed = await self._automation.run_now(
                    automation_id,
                    inbound=message,
                    conversation_key=identity.key,
                )
                return "任务已进入待执行队列。" if changed else "该任务不能立即执行。"
        except ValueError as exc:
            return str(exc)
        return "可用操作：list、completed、show、pause、resume、cancel、run、history。"
