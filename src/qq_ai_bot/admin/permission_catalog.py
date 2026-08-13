"""Event-bound permission resolution and unified capability discovery."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import IntEnum, StrEnum
from typing import Literal

from qq_ai_bot.admin.action_service import ActionRegistry, ActionSpec
from qq_ai_bot.admin.config_registry import ConfigRegistry
from qq_ai_bot.admin.models import ConfigSpec
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.messages import InboundMessage

_INTERNAL_CAPABILITY_MARKERS = (
    '"transient_internal_reference"',
    '"do_not_copy_verbatim_to_user"',
)


def contains_internal_capability_payload(content: str) -> bool:
    """Reject accidental model echoes of the transient permission tool payload."""

    return any(marker in content for marker in _INTERNAL_CAPABILITY_MARKERS)


class PermissionLevel(IntEnum):
    """Ordered permission levels; middle levels are intentionally inactive today."""

    USER = 0
    TRUSTED = 10
    MODERATOR = 20
    SUPERUSER = 100


class CapabilityKind(StrEnum):
    """Stable kinds returned by the capability-discovery interface."""

    COMMAND = "command"
    CONFIGURATION = "configuration"
    ACTION = "action"
    ONEBOT = "onebot"


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """Safe metadata for one operation; it never carries a runtime value or secret."""

    id: str
    kind: CapabilityKind
    category: str
    display_name: str
    description: str
    minimum_level: PermissionLevel
    mutating: bool
    target_scopes: tuple[str, ...] = ()
    apply_mode: str | None = None
    value_type: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    search_terms: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe representation suitable for an LLM tool result."""

        return {
            "id": self.id,
            "kind": self.kind.value,
            "category": self.category,
            "display_name": self.display_name,
            "description": self.description,
            "minimum_level": self.minimum_level.name.casefold(),
            "mutating": self.mutating,
            "target_scopes": list(self.target_scopes),
            "apply_mode": self.apply_mode,
            "value_type": self.value_type,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "choices": list(self.choices),
        }


@dataclass(frozen=True, slots=True)
class CapabilityReport:
    """Complete capability view for the sender of one authoritative event."""

    actor_user_id: str
    permission_level: PermissionLevel
    permission_source: str
    capabilities: tuple[CapabilityDescriptor, ...]

    @property
    def mutable_config_count(self) -> int:
        return sum(
            descriptor.kind is CapabilityKind.CONFIGURATION and descriptor.mutating
            for descriptor in self.capabilities
        )

    @property
    def protected_config_count(self) -> int:
        return sum(
            descriptor.kind is CapabilityKind.CONFIGURATION and not descriptor.mutating
            for descriptor in self.capabilities
        )

    @property
    def business_action_count(self) -> int:
        return sum(descriptor.kind is CapabilityKind.ACTION for descriptor in self.capabilities)

    @property
    def mutating_action_count(self) -> int:
        return sum(
            descriptor.kind is CapabilityKind.ACTION and descriptor.mutating
            for descriptor in self.capabilities
        )

    @property
    def self_service_operation_count(self) -> int:
        return sum(descriptor.kind is CapabilityKind.COMMAND for descriptor in self.capabilities)

    @property
    def self_service_mutation_count(self) -> int:
        return sum(
            descriptor.kind is CapabilityKind.COMMAND and descriptor.mutating
            for descriptor in self.capabilities
        )

    @property
    def onebot_gateway_count(self) -> int:
        return sum(descriptor.kind is CapabilityKind.ONEBOT for descriptor in self.capabilities)

    def to_dict(self) -> dict[str, object]:
        """Return stable grouped lists plus exact summary counts."""

        groups: dict[str, dict[str, list[dict[str, object]]]] = {}
        for descriptor in self.capabilities:
            group = groups.setdefault(
                descriptor.category,
                {kind.value: [] for kind in CapabilityKind},
            )
            group[descriptor.kind.value].append(descriptor.to_dict())

        scopes = sorted(
            {scope for descriptor in self.capabilities for scope in descriptor.target_scopes}
        )
        apply_modes = sorted(
            {
                descriptor.apply_mode
                for descriptor in self.capabilities
                if descriptor.apply_mode is not None
            }
        )
        self_service = [
            descriptor.id
            for descriptor in self.capabilities
            if descriptor.kind is CapabilityKind.COMMAND
        ]
        return {
            "actor_user_id": self.actor_user_id,
            "permission_level": self.permission_level.name.casefold(),
            "permission_rank": int(self.permission_level),
            "permission_source": self.permission_source,
            "counts": {
                "total": len(self.capabilities),
                "mutable_configurations": self.mutable_config_count,
                "protected_configurations": self.protected_config_count,
                "business_actions": self.business_action_count,
                "mutating_business_actions": self.mutating_action_count,
                "self_service_operations": self.self_service_operation_count,
                "self_service_mutations": self.self_service_mutation_count,
                "onebot_api_gateways": self.onebot_gateway_count,
            },
            "groups": groups,
            "available_scopes": scopes,
            "available_apply_modes": apply_modes,
            "self_service_operations": self_service,
            "permission_levels": [
                {
                    "name": PermissionLevel.USER.name.casefold(),
                    "rank": int(PermissionLevel.USER),
                    "active": True,
                    "assignment": "default_user",
                },
                {
                    "name": PermissionLevel.TRUSTED.name.casefold(),
                    "rank": int(PermissionLevel.TRUSTED),
                    "active": False,
                    "assignment": "reserved_not_assignable",
                },
                {
                    "name": PermissionLevel.MODERATOR.name.casefold(),
                    "rank": int(PermissionLevel.MODERATOR),
                    "active": False,
                    "assignment": "reserved_not_assignable",
                },
                {
                    "name": PermissionLevel.SUPERUSER.name.casefold(),
                    "rank": int(PermissionLevel.SUPERUSER),
                    "active": True,
                    "assignment": "SUPERUSERS",
                },
            ],
        }

    def to_compact_dict(self) -> dict[str, object]:
        """Return every capability ID in a bounded, non-redundant tool payload."""

        groups: dict[str, dict[str, list[dict[str, object]]]] = {}
        for descriptor in self.capabilities:
            kind = descriptor.kind.value
            group = groups.setdefault(descriptor.category, {})
            item: dict[str, object] = {"id": descriptor.id}
            if descriptor.mutating:
                item["mutating"] = True
            if descriptor.target_scopes:
                item["scopes"] = list(descriptor.target_scopes)
            if descriptor.apply_mode is not None:
                item["apply_mode"] = descriptor.apply_mode
            # Detailed type/range metadata is available from the focused view. Repeating
            # it for every configuration makes the complete capability index exceed the
            # bounded tool-result budget as the registry grows.
            group.setdefault(kind, []).append(item)

        full = self.to_dict()
        return {
            "actor_user_id": full["actor_user_id"],
            "permission_level": full["permission_level"],
            "permission_rank": full["permission_rank"],
            "permission_source": full["permission_source"],
            "counts": full["counts"],
            "groups": groups,
        }

    def to_model_dict(
        self,
        mode: Literal["summary", "focused", "full"] = "summary",
    ) -> dict[str, object]:
        """Return a transient model view sized for the requested discovery task."""

        full = self.to_dict()
        base: dict[str, object] = {
            "transient_internal_reference": True,
            "do_not_copy_verbatim_to_user": True,
            "permission_level": full["permission_level"],
            "permission_source": full["permission_source"],
            "counts": full["counts"],
        }
        category_counts: dict[str, dict[str, int]] = {}
        for descriptor in self.capabilities:
            counts = category_counts.setdefault(
                descriptor.category,
                {
                    "total": 0,
                    "commands": 0,
                    "configurations": 0,
                    "actions": 0,
                    "onebot_gateways": 0,
                    "mutating": 0,
                },
            )
            counts["total"] += 1
            counts[
                {
                    CapabilityKind.COMMAND: "commands",
                    CapabilityKind.CONFIGURATION: "configurations",
                    CapabilityKind.ACTION: "actions",
                    CapabilityKind.ONEBOT: "onebot_gateways",
                }[descriptor.kind]
            ] += 1
            if descriptor.mutating:
                counts["mutating"] += 1
        base["categories"] = category_counts
        if self.onebot_gateway_count:
            base["onebot_scope"] = {
                "tool": "call_onebot_api(action, params)",
                "all_public_actions": True,
                "action_allowlist": None,
                "second_confirmation_required": False,
                "availability": "direct_superuser_non_autonomous_turn_before_web_tools",
            }
        if mode == "summary":
            return base
        if mode == "full":
            base.pop("categories", None)
            grouped_ids: dict[str, dict[str, list[str]]] = {}
            for descriptor in self.capabilities:
                grouped_ids.setdefault(descriptor.category, {}).setdefault(
                    descriptor.kind.value,
                    [],
                ).append(descriptor.id)
            base["capability_ids"] = grouped_ids
            base["available_scopes"] = full["available_scopes"]
            base["available_apply_modes"] = full["available_apply_modes"]
            return base
        base["capabilities"] = [
            {
                "id": descriptor.id,
                "kind": descriptor.kind.value,
                "category": descriptor.category,
                "name": descriptor.display_name,
                "description": descriptor.description,
                "mutating": descriptor.mutating,
                "scopes": list(descriptor.target_scopes),
                "apply_mode": descriptor.apply_mode,
                "value_type": descriptor.value_type,
                "minimum": descriptor.minimum,
                "maximum": descriptor.maximum,
                "choices": list(descriptor.choices),
                "aliases": list(descriptor.search_terms),
            }
            for descriptor in self.capabilities
        ]
        return base

    def render_text(self) -> str:
        """Render the complete report without asking an LLM to summarize it."""

        if not self.capabilities:
            return "当前权限下没有找到该类别的可用能力。"

        role_name = (
            "超级管理员" if self.permission_level is PermissionLevel.SUPERUSER else "普通用户"
        )
        lines = [
            f"当前权限：{role_name}（来源：{self.permission_source}）",
            f"可修改运行时配置参数：{self.mutable_config_count} 项",
            (
                "管理员业务接口："
                f"{self.business_action_count} 项，其中修改型 {self.mutating_action_count} 项"
            ),
            (
                "本人确定性自助接口："
                f"{self.self_service_operation_count} 项，其中修改型 "
                f"{self.self_service_mutation_count} 项"
            ),
            f"NapCat/OneBot 通用全接口网关：{self.onebot_gateway_count} 项",
        ]

        configurations = tuple(
            item
            for item in self.capabilities
            if item.kind is CapabilityKind.CONFIGURATION and item.mutating
        )
        protected = tuple(
            item
            for item in self.capabilities
            if item.kind is CapabilityKind.CONFIGURATION and not item.mutating
        )
        actions = tuple(item for item in self.capabilities if item.kind is CapabilityKind.ACTION)
        self_service = tuple(
            item for item in self.capabilities if item.kind is CapabilityKind.COMMAND
        )
        onebot = tuple(item for item in self.capabilities if item.kind is CapabilityKind.ONEBOT)
        if configurations:
            lines.extend(self._group_lines("可修改配置", configurations, id_prefix="config:"))
        if actions:
            lines.extend(self._group_lines("管理员业务接口", actions, id_prefix="action:"))
        if self_service:
            lines.extend(self._group_lines("本人自助接口", self_service, id_prefix="command:"))
        if onebot:
            lines.extend(
                self._group_lines(
                    "NapCat/OneBot 全接口权限（action 不设 denylist）",
                    onebot,
                    id_prefix="onebot:",
                )
            )
            lines.append(
                "- 权限说明：超级管理员在当前真实消息触发的普通聊天轮中，可通过 "
                "call_onebot_api(action, params) 调用当前 NapCat/OneBot 提供的全部公开 "
                "action，无 action 白名单或 denylist，也不需要二次确认；这不是只限于上面的 "
                f"{self.business_action_count} 项应用业务接口。自主群聊轮不开放；本轮使用联网"
                "工具后会撤销该网关。"
            )
        if protected:
            lines.extend(
                self._group_lines(
                    f"受保护配置（{len(protected)} 项，不可修改）",
                    protected,
                    id_prefix="config:",
                )
            )
        scopes = sorted({scope for item in self.capabilities for scope in item.target_scopes})
        modes = sorted(
            {item.apply_mode for item in self.capabilities if item.apply_mode is not None}
        )
        if scopes:
            lines.append("可用作用域：" + "、".join(scopes))
        if modes:
            lines.append("生效方式：" + "、".join(modes))
        lines.append("预留权限：trusted、moderator（当前未启用，也不能分配）。")
        return "\n".join(lines)

    @staticmethod
    def _group_lines(
        title: str,
        capabilities: tuple[CapabilityDescriptor, ...],
        *,
        id_prefix: str,
    ) -> list[str]:
        grouped: dict[str, list[str]] = {}
        for item in capabilities:
            normalized_id = item.id.removeprefix(id_prefix)
            if id_prefix == "action:":
                normalized_id = normalized_id.rsplit(":any_", maxsplit=1)[0]
            elif id_prefix == "command:":
                normalized_id = normalized_id.removesuffix(":self")
            grouped.setdefault(item.category, []).append(normalized_id)
        lines = [title + "："]
        lines.extend(f"- {category}：{', '.join(items)}" for category, items in grouped.items())
        return lines


class PermissionResolver:
    """Resolve only USER or SUPERUSER from the current transport event sender QQ."""

    def __init__(self, settings: Settings) -> None:
        self._superusers = settings.superusers

    def resolve(self, message: InboundMessage) -> PermissionLevel:
        """Ignore model claims and derive authority from the real inbound sender."""

        user_id = message.sender.user_id.strip()
        if not user_id:
            raise ValueError("current event sender QQ is required")
        if user_id in self._superusers:
            return PermissionLevel.SUPERUSER
        return PermissionLevel.USER

    def source(self, message: InboundMessage) -> str:
        return (
            "SUPERUSERS" if self.resolve(message) is PermissionLevel.SUPERUSER else "default_user"
        )


_BASE_SELF_SERVICE_CAPABILITIES = (
    CapabilityDescriptor(
        id="command:chat.help:self",
        kind=CapabilityKind.COMMAND,
        category="chat",
        display_name="查看帮助",
        description="通过确定性 /ai help 命令查看帮助。",
        minimum_level=PermissionLevel.USER,
        mutating=False,
        target_scopes=("current_conversation",),
    ),
    CapabilityDescriptor(
        id="command:chat.new:self",
        kind=CapabilityKind.COMMAND,
        category="chat",
        display_name="新建当前上下文",
        description="通过确定性 /ai new 命令写入当前用户和当前场景的上下文切点。",
        minimum_level=PermissionLevel.USER,
        mutating=True,
        target_scopes=("current_conversation",),
    ),
    CapabilityDescriptor(
        id="command:chat.status:self",
        kind=CapabilityKind.COMMAND,
        category="chat",
        display_name="查看状态",
        description="通过确定性 /ai status 命令查看当前服务与会话状态。",
        minimum_level=PermissionLevel.USER,
        mutating=False,
        target_scopes=("current_conversation",),
    ),
    CapabilityDescriptor(
        id="command:chat.stop:self",
        kind=CapabilityKind.COMMAND,
        category="chat",
        display_name="停止当前请求",
        description="通过确定性 /ai stop 命令取消当前会话正在进行的模型请求。",
        minimum_level=PermissionLevel.USER,
        mutating=False,
        target_scopes=("current_conversation",),
    ),
    CapabilityDescriptor(
        id="command:chat.ping:self",
        kind=CapabilityKind.COMMAND,
        category="chat",
        display_name="连接测试",
        description="通过确定性 /ai ping 命令检查 {bot_name} 响应。",
        minimum_level=PermissionLevel.USER,
        mutating=False,
        target_scopes=("current_conversation",),
    ),
    CapabilityDescriptor(
        id="command:identity.whoami:self",
        kind=CapabilityKind.COMMAND,
        category="identity",
        display_name="查看本人身份",
        description="通过确定性 /ai whoami 命令查看 {bot_name} 识别到的本人资料。",
        minimum_level=PermissionLevel.USER,
        mutating=False,
        target_scopes=("self",),
    ),
    CapabilityDescriptor(
        id="command:identity.forgetme:self",
        kind=CapabilityKind.COMMAND,
        category="identity",
        display_name="删除本人数据",
        description="通过确定性 /ai forgetme 命令删除发送者本人可归属数据。",
        minimum_level=PermissionLevel.USER,
        mutating=True,
        target_scopes=("self",),
    ),
    CapabilityDescriptor(
        id="command:automation.create:self",
        kind=CapabilityKind.COMMAND,
        category="automation",
        display_name="创建本人自动化",
        description=(
            "通过普通文本聊天中的 automation_create 工具提交 TaskSpec，由后端编译并持久化任务。"
        ),
        minimum_level=PermissionLevel.USER,
        mutating=True,
        target_scopes=("self", "current_group"),
    ),
    CapabilityDescriptor(
        id="command:automation.list:self",
        kind=CapabilityKind.COMMAND,
        category="automation",
        display_name="列出本人自动化",
        description="列出当前发送者仍在运行或暂停的任务，并显示稳定的自动化 ID。",
        minimum_level=PermissionLevel.USER,
        mutating=False,
        target_scopes=("self",),
    ),
    CapabilityDescriptor(
        id="command:automation.list_history:self",
        kind=CapabilityKind.COMMAND,
        category="automation",
        display_name="查看自动化完成历史",
        description=(
            "通过 automation_list_history 或 /ai automation completed 单独列出已结束任务。"
        ),
        minimum_level=PermissionLevel.USER,
        mutating=False,
        target_scopes=("self",),
    ),
    CapabilityDescriptor(
        id="command:automation.get:self",
        kind=CapabilityKind.COMMAND,
        category="automation",
        display_name="查看本人自动化",
        description="通过自然语言工具或 /ai automation show 查看当前发送者自己的任务。",
        minimum_level=PermissionLevel.USER,
        mutating=False,
        target_scopes=("self",),
    ),
    CapabilityDescriptor(
        id="command:automation.update:self",
        kind=CapabilityKind.COMMAND,
        category="automation",
        display_name="更新本人自动化",
        description="通过 automation_update 编译 TaskSpec，为本人任务创建新脚本版本。",
        minimum_level=PermissionLevel.USER,
        mutating=True,
        target_scopes=("self", "current_group"),
    ),
    CapabilityDescriptor(
        id="command:automation.diagnose:self",
        kind=CapabilityKind.COMMAND,
        category="automation",
        display_name="诊断自动化创建",
        description="读取本人最近的脱敏创建结果，核实任务是否真正持久化。",
        minimum_level=PermissionLevel.USER,
        mutating=False,
        target_scopes=("self",),
    ),
    *(
        CapabilityDescriptor(
            id=f"command:automation.{operation}:self",
            kind=CapabilityKind.COMMAND,
            category="automation",
            display_name=display_name,
            description=f"通过自然语言工具或 /ai automation 命令{description}本人任务。",
            minimum_level=PermissionLevel.USER,
            mutating=mutating,
            target_scopes=("self",),
        )
        for operation, display_name, description, mutating in (
            ("pause", "暂停本人自动化", "暂停", True),
            ("resume", "恢复本人自动化", "恢复", True),
            ("cancel", "取消本人自动化", "取消", True),
            ("run_now", "立即运行本人自动化", "立即调度", True),
            ("history", "查看自动化历史", "查看执行历史", False),
        )
    ),
    CapabilityDescriptor(
        id="command:time.get_current:self",
        kind=CapabilityKind.COMMAND,
        category="time",
        display_name="查看可信当前时间",
        description="读取后端可信 UTC、本地时间、日期、星期和当前时区。",
        minimum_level=PermissionLevel.USER,
        mutating=False,
        target_scopes=("self",),
    ),
    CapabilityDescriptor(
        id="command:time.get_timezone:self",
        kind=CapabilityKind.COMMAND,
        category="time",
        display_name="查看本人时区",
        description="读取当前真实发送者保存的 IANA 时区。",
        minimum_level=PermissionLevel.USER,
        mutating=False,
        target_scopes=("self",),
    ),
    CapabilityDescriptor(
        id="command:time.set_timezone:self",
        kind=CapabilityKind.COMMAND,
        category="time",
        display_name="设置本人时区",
        description="验证并设置当前真实发送者自己的 IANA 时区。",
        minimum_level=PermissionLevel.USER,
        mutating=True,
        target_scopes=("self",),
    ),
)


_MEMORY_LIFECYCLE_CAPABILITIES = (
    *(
        CapabilityDescriptor(
            id=f"command:memory.{operation}:self",
            kind=CapabilityKind.COMMAND,
            category="memory",
            display_name=display_name,
            description=description,
            minimum_level=PermissionLevel.USER,
            mutating=mutating,
            target_scopes=("self", "current_group"),
        )
        for operation, display_name, description, mutating in (
            ("show", "查看记忆事实", "查看本人可访问的单条记忆事实。", False),
            ("explain", "解释记忆事实", "查看本人可访问事实的证据摘要。", False),
            ("history", "查看记忆版本", "查看本人可访问事实的状态和版本历史。", False),
            ("conflicts", "查看记忆冲突", "查看与本人有关的争议事实。", False),
            ("correct", "修正记忆事实", "通过新版本修正本人事实，不原地改写正文。", True),
            ("invalidate", "撤回记忆事实", "将本人事实标记为失效，不物理删除。", True),
            ("restore", "恢复记忆事实", "恢复本人有权限恢复的失效事实。", True),
        )
    ),
    *(
        CapabilityDescriptor(
            id=f"command:memory.{operation}:admin",
            kind=CapabilityKind.COMMAND,
            category="memory",
            display_name=display_name,
            description=description,
            minimum_level=PermissionLevel.SUPERUSER,
            mutating=mutating,
            target_scopes=("explicit_user_id", "explicit_group_id", "global"),
        )
        for operation, display_name, description, mutating in (
            ("merge", "合并记忆事实", "合并同一身份目标下的重复事实与证据。", True),
            ("resolve", "解决记忆冲突", "明确选择争议事实并失效其余冲突版本。", True),
            ("doctor", "诊断记忆一致性", "运行只读的记忆一致性检查。", False),
            ("maintenance.status", "查看记忆维护", "查看本地记忆维护任务状态。", False),
            ("maintenance.run", "运行记忆维护", "立即运行一次有界的本地生命周期维护。", True),
            (
                "self-reflection.run",
                "运行 {bot_name} 自省",
                "立即运行一次有界的 {bot_name} Self Reflection。",
                True,
            ),
            ("dream.plan", "规划 Memory Dream", "只读规划一次全库 Dream 快照。", False),
            ("dream.start", "启动 Memory Dream", "后台启动一个已规划的 Dream。", True),
            ("dream.list", "查看 Memory Dream", "查看 Dream 运行与簇状态。", False),
            ("dream.cancel", "取消 Memory Dream", "取消尚未完成的 Dream。", True),
            ("dream.retry", "重试 Memory Dream", "重试失败或过期的 Dream 簇。", True),
            ("dream.rollback", "回滚 Memory Dream", "回滚 Dream operation 或整轮运行。", True),
        )
    ),
)


class PermissionCatalogService:
    """Build capability reports from the two existing allowlist registries."""

    def __init__(
        self,
        *,
        settings: Settings,
        config_registry: ConfigRegistry | None = None,
        action_registry: ActionRegistry | None = None,
    ) -> None:
        self._resolver = PermissionResolver(settings)
        self._bot_display_name = settings.bot_display_name
        self._config_registry = config_registry or ConfigRegistry()
        self._action_registry = action_registry or ActionRegistry()
        self._descriptors = self._build_descriptors()
        ids = [descriptor.id for descriptor in self._descriptors]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate permission capability id")

    def report_for_message(
        self,
        message: InboundMessage,
        *,
        category: str | None = None,
        query: str | None = None,
    ) -> CapabilityReport:
        """Query the sender's catalog without accepting a caller-supplied QQ or role."""

        level = self._resolver.resolve(message)
        normalized_category = category.strip().casefold() if category else None
        normalized_query = query.strip().casefold() if query else None
        if normalized_query is not None and len(normalized_query) > 64:
            raise ValueError("capability query must not exceed 64 characters")
        capabilities = tuple(
            descriptor
            for descriptor in self._descriptors
            if descriptor.minimum_level <= level
            and (
                normalized_category is None or descriptor.category.casefold() == normalized_category
            )
            and (
                normalized_query is None
                or any(
                    normalized_query in candidate.casefold()
                    for candidate in (
                        descriptor.id,
                        descriptor.category,
                        descriptor.display_name,
                        descriptor.description,
                        *descriptor.search_terms,
                    )
                )
            )
        )
        return CapabilityReport(
            actor_user_id=message.sender.user_id,
            permission_level=level,
            permission_source=self._resolver.source(message),
            capabilities=capabilities,
        )

    def _build_descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        descriptors = [
            replace(
                descriptor,
                display_name=descriptor.display_name.format(bot_name=self._bot_display_name),
                description=descriptor.description.format(bot_name=self._bot_display_name),
            )
            for descriptor in (
                *_BASE_SELF_SERVICE_CAPABILITIES,
                *_MEMORY_LIFECYCLE_CAPABILITIES,
            )
        ]
        descriptors.extend(self._self_service_descriptors())
        descriptors.extend(
            self._configuration_descriptor(spec) for spec in self._config_registry.list()
        )
        descriptors.extend(self._action_descriptor(spec) for spec in self._action_registry.list())
        descriptors.append(
            CapabilityDescriptor(
                id="onebot:call_onebot_api:any_public_action",
                kind=CapabilityKind.ONEBOT,
                category="onebot",
                display_name="调用全部 NapCat/OneBot 公开接口",
                description=(
                    "当前真实消息发送者属于 SUPERUSERS 的直接普通聊天轮可使用 "
                    "call_onebot_api(action, params)，action 不设 denylist，也不需要二次确认。"
                    "自主群聊轮不开放；使用网页工具后本轮会撤销该网关。"
                ),
                minimum_level=PermissionLevel.SUPERUSER,
                mutating=True,
                target_scopes=("current_direct_superuser_event",),
            )
        )
        return tuple(
            sorted(
                descriptors,
                key=lambda descriptor: (
                    descriptor.category,
                    descriptor.kind.value,
                    descriptor.id,
                ),
            )
        )

    def _self_service_descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(
            CapabilityDescriptor(
                id=f"command:{spec.name}:self",
                kind=CapabilityKind.COMMAND,
                category=spec.name.partition(".")[0],
                display_name=spec.display_name,
                description=(
                    f"{spec.description} "
                    + (
                        "可通过确定性 /ai 命令全局查询已认识人物；"
                        if "global_person" in spec.self_service_scopes
                        else "仅可通过确定性 /ai 命令操作发送者本人；"
                    )
                    + "不表示普通聊天 Agent 获得管理员工具。"
                ),
                minimum_level=PermissionLevel.USER,
                mutating=spec.mutating,
                target_scopes=spec.self_service_scopes,
            )
            for spec in self._action_registry.list()
            if spec.self_service
        )

    @staticmethod
    def _configuration_descriptor(spec: ConfigSpec) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            id=f"config:{spec.key}",
            kind=CapabilityKind.CONFIGURATION,
            category=spec.category,
            display_name=spec.display_name,
            description=spec.description,
            minimum_level=_permission_level(spec.permission),
            mutating=spec.mutable,
            target_scopes=tuple(scope.value for scope in spec.allowed_scopes),
            apply_mode=spec.apply_mode.value,
            value_type=spec.value_type,
            minimum=spec.minimum,
            maximum=spec.maximum,
            choices=spec.choices,
            search_terms=spec.aliases,
        )

    @staticmethod
    def _action_descriptor(spec: ActionSpec) -> CapabilityDescriptor:
        target_scopes = (
            ("self", "mentioned_user", "explicit_user_id")
            if spec.target_kind == "user"
            else ("current_group", "explicit_group_id")
        )
        return CapabilityDescriptor(
            id=f"action:{spec.name}:any_{spec.target_kind}",
            kind=CapabilityKind.ACTION,
            category=spec.name.partition(".")[0],
            display_name=spec.display_name,
            description=spec.description,
            minimum_level=PermissionLevel.SUPERUSER,
            mutating=spec.mutating,
            target_scopes=target_scopes,
        )


def _permission_level(permission: str) -> PermissionLevel:
    normalized = permission.strip().casefold()
    levels = {
        "user": PermissionLevel.USER,
        "trusted": PermissionLevel.TRUSTED,
        "moderator": PermissionLevel.MODERATOR,
        "superuser": PermissionLevel.SUPERUSER,
    }
    try:
        return levels[normalized]
    except KeyError as exc:
        raise ValueError(f"unknown permission level: {permission}") from exc


__all__ = [
    "CapabilityDescriptor",
    "CapabilityKind",
    "CapabilityReport",
    "PermissionCatalogService",
    "PermissionLevel",
    "PermissionResolver",
]
