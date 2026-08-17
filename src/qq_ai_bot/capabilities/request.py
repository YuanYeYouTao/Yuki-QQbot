"""Model-facing schema for loading omitted capabilities via local search."""

from __future__ import annotations

from qq_ai_bot.domain.messages import ChatTool

REQUEST_TOOLS_NAME = "request_tools"


def request_tools_definition() -> ChatTool:
    """Return the stable schema used to ask the Host for omitted tools."""

    return ChatTool(
        name=REQUEST_TOOLS_NAME,
        description=(
            "当完成当前请求所需的工具没有出现在本轮工具列表中时，按自然语言能力描述"
            "向后端请求加载。它只加载当前真实用户、来源和场景原本有权调用、但因"
            "Schema 预算未预载的工具；不能越过真实权限。返回后应在"
            "下一步直接调用 loaded_tools 中的真实工具，不要猜测、改写或虚构工具名。"
            "已有合适工具时不要调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 200,
                    "description": "所需能力，例如：搜索并发送网易云单曲",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 8,
                    "default": 4,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )


__all__ = [
    "REQUEST_TOOLS_NAME",
    "request_tools_definition",
]
