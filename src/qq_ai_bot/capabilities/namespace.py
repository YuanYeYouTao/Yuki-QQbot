"""Capability namespace model (frozen by R3 §2.2).

A namespace is a semantic discovery category — not a provider, not a
permission and not a hard routing gate.  Search results may come from the
whole catalog but must always be intersected with the turn's
authority-filtered requestable capability set.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

NAMESPACE_ID_MAX_LENGTH = 64
NAMESPACE_MAX_DEPTH = 5
_SEGMENT_PATTERN = r"[a-z][a-z0-9_]*"
NAMESPACE_ID_REGEX = re.compile(rf"^{_SEGMENT_PATTERN}(\.{_SEGMENT_PATTERN})*$")


def is_valid_namespace_id(value: str) -> bool:
    """Lowercase dot-separated hierarchy, e.g. ``memory.person.read``."""

    if not value or len(value) > NAMESPACE_ID_MAX_LENGTH:
        return False
    if value.count(".") + 1 > NAMESPACE_MAX_DEPTH:
        return False
    return NAMESPACE_ID_REGEX.fullmatch(value) is not None


class CapabilityNamespace(BaseModel):
    """One semantic category in the capability catalog."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    parent: str | None = None
    display_name: str
    description: str = ""
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not is_valid_namespace_id(value):
            raise ValueError(f"invalid namespace id: {value!r}")
        return value

    @field_validator("aliases", "tags")
    @classmethod
    def _valid_labels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for label in value:
            if not label or label != label.lower():
                raise ValueError(f"namespace aliases/tags must be lowercase, got {label!r}")
        if len(set(value)) != len(value):
            raise ValueError("namespace aliases/tags must be unique")
        return value

    @model_validator(mode="after")
    def _valid_parent(self) -> CapabilityNamespace:
        if self.parent is not None:
            if not is_valid_namespace_id(self.parent):
                raise ValueError(f"invalid parent namespace id: {self.parent!r}")
            if not self.id.startswith(f"{self.parent}."):
                raise ValueError(
                    f"namespace {self.id!r} must be nested under its parent {self.parent!r}"
                )
        return self

    @property
    def path(self) -> tuple[str, ...]:
        return tuple(self.id.split("."))

    @property
    def depth(self) -> int:
        return len(self.path)


def _ns(
    namespace_id: str,
    display_name: str,
    *,
    description: str = "",
    aliases: tuple[str, ...] = (),
    tags: tuple[str, ...] = (),
) -> CapabilityNamespace:
    parent = namespace_id.rpartition(".")[0] or None
    return CapabilityNamespace(
        id=namespace_id,
        parent=parent,
        display_name=display_name,
        description=description,
        aliases=aliases,
        tags=tags,
    )


CORE_NAMESPACES: tuple[CapabilityNamespace, ...] = (
    _ns("web", "联网", description="公开网页检索与阅读", tags=("web",)),
    _ns(
        "web.search",
        "网页搜索",
        description="受控联网搜索",
        aliases=("search", "联网"),
        tags=("web",),
    ),
    _ns(
        "web.read",
        "网页阅读",
        description="读取公开网页",
        aliases=("webpage", "url"),
        tags=("web",),
    ),
    _ns("memory", "记忆", description="长期记忆与聊天记录", tags=("memory",)),
    _ns("memory.history", "聊天记录", description="近期与历史消息", tags=("memory", "history")),
    _ns(
        "memory.history.recent",
        "最近消息",
        description="当前会话最近消息",
        aliases=("刚才", "刚刚"),
        tags=("memory", "history"),
    ),
    _ns(
        "memory.history.search",
        "搜索记录",
        description="按关键词搜索聊天账本",
        aliases=("以前", "查记录"),
        tags=("memory", "history"),
    ),
    _ns("memory.person", "人物记忆", description="人物结构记忆", tags=("memory",)),
    _ns(
        "memory.person.read",
        "人物记忆读取",
        description="读取人物长期记忆",
        aliases=("person_memory", "群友记忆"),
        tags=("memory",),
    ),
    _ns("memory.self", "自我记忆", description="助手自身长期记忆", tags=("memory",)),
    _ns(
        "memory.self.read",
        "自我记忆读取",
        description="读取助手自身记忆",
        aliases=("self_memory",),
        tags=("memory",),
    ),
    _ns("memory.group", "群记忆", description="群整体结构记忆", tags=("memory",)),
    _ns(
        "memory.group.read",
        "群记忆读取",
        description="读取当前群共同记忆",
        aliases=("group_memory",),
        tags=("memory",),
    ),
    _ns("memory.fact", "记忆事实", description="按 id 读取一条记忆", tags=("memory",)),
    _ns("memory.fact.read", "记忆事实读取", description="按 fact_id 读取事实", tags=("memory",)),
    _ns("memory.evidence", "记忆证据", description="有界证据摘要", tags=("memory",)),
    _ns("memory.evidence.read", "记忆证据读取", description="读取本人证据摘要", tags=("memory",)),
    _ns("memory.state", "记忆状态", description="长期记忆变更", tags=("memory",)),
    _ns(
        "memory.state.write",
        "记忆写入",
        description="创建、纠正或撤回长期记忆",
        aliases=("记住", "忘记", "纠正记忆"),
        tags=("memory", "write"),
    ),
    _ns("relationship", "关系", description="好感与关系阶段", tags=("relationship",)),
    _ns(
        "relationship.read",
        "关系读取",
        description="读取好感度、信任度和关系阶段",
        aliases=("好感度", "亲密度"),
        tags=("relationship",),
    ),
    _ns("automation", "自动化", description="定时与周期任务", tags=("automation",)),
    _ns(
        "automation.read",
        "自动化读取",
        description="列举或查看自动化任务",
        aliases=("任务列表",),
        tags=("automation",),
    ),
    _ns(
        "automation.write",
        "自动化写入",
        description="创建或修改自动化任务",
        aliases=("提醒", "定时"),
        tags=("automation", "write"),
    ),
    _ns("qq", "QQ 平台", description="OneBot/NapCat 平台操作", tags=("qq",)),
    _ns("qq.platform", "QQ 平台操作", description="好友与群管理", tags=("qq",)),
    _ns(
        "qq.platform.mutate",
        "QQ 平台变更",
        description="禁言、踢人等平台写操作",
        aliases=("禁言", "踢人"),
        tags=("qq", "write"),
    ),
    _ns("reply", "回复效果", description="语音、表情、引用与布局", tags=("reply",)),
    _ns(
        "reply.voice",
        "语音回复",
        description="生成本轮语音",
        aliases=("语音", "朗读"),
        tags=("reply",),
    ),
    _ns("reply.target", "回复引用", description="指定引用的可见事件", tags=("reply",)),
    _ns("kernel", "内核", description="权限目录与工具产物", tags=("kernel",)),
    _ns("kernel.artifact", "工具产物", description="短期 artifact", tags=("kernel",)),
    _ns(
        "kernel.artifact.read",
        "读取产物",
        description="读取本轮工具 artifact",
        tags=("kernel",),
    ),
    _ns("kernel.authority", "权限目录", description="当前发送者可管理的权限", tags=("kernel",)),
    _ns(
        "kernel.authority.read",
        "权限查询",
        description="查询本人能改什么",
        aliases=("权限", "能改什么"),
        tags=("kernel",),
    ),
    _ns("admin", "管理", description="超级管理员配置与动作", tags=("admin",)),
    _ns("admin.config", "运行时配置", description="配置读写", tags=("admin",)),
    _ns("admin.config.read", "读取配置", description="读取有效配置", tags=("admin",)),
    _ns("admin.config.write", "写入配置", description="修改运行时配置", tags=("admin", "write")),
    _ns("admin.history", "配置历史", description="配置变更历史", tags=("admin",)),
    _ns(
        "admin.history.read",
        "读取配置历史",
        description="读取配置变更记录",
        tags=("admin",),
    ),
    _ns("admin.action", "管理动作", description="已审核的管理员动作", tags=("admin",)),
    _ns(
        "admin.action.write",
        "执行管理动作",
        description="执行已审核动作",
        tags=("admin", "write"),
    ),
    _ns("admin.memory", "记忆重建", description="记忆重建流水线", tags=("admin", "memory")),
    _ns(
        "admin.memory.rebuild",
        "记忆重建",
        description="计划、审批与提交记忆重建",
        tags=("admin", "memory"),
    ),
)

NAMESPACE_BY_ID: dict[str, CapabilityNamespace] = {item.id: item for item in CORE_NAMESPACES}

RESERVED_PLUGIN_NAMESPACE_PREFIXES: frozenset[str] = frozenset(
    {
        "kernel",
        "memory",
        "web",
        "qq",
        "reply",
        "relationship",
        "automation",
        "admin",
        "core",
        "system",
        "yuki",
    }
)


def namespace_parent(namespace_id: str) -> str | None:
    parent = namespace_id.rpartition(".")[0]
    return parent or None


def lookup_namespace(namespace_id: str) -> CapabilityNamespace | None:
    return NAMESPACE_BY_ID.get(namespace_id)
