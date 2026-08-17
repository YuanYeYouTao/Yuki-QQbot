"""Small model-facing gateway for loading omitted Tool Kernel capabilities."""

from __future__ import annotations

import re
from dataclasses import dataclass

from qq_ai_bot.capabilities.catalog import UnifiedToolCatalog, UnifiedToolCatalogEntry
from qq_ai_bot.domain.messages import ChatTool

REQUEST_TOOLS_NAME = "request_tools"
_ASCII_TERM = re.compile(r"[a-z0-9_.-]{2,}")
_CJK_RUN = re.compile(r"[\u3400-\u9fff]{2,}")


@dataclass(frozen=True, slots=True)
class ToolRequestMatch:
    entry: UnifiedToolCatalogEntry
    score: int


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


def match_requestable_tools(
    catalog: UnifiedToolCatalog,
    *,
    query: str,
    limit: int = 4,
    excluded_names: frozenset[str] = frozenset(),
) -> tuple[ToolRequestMatch, ...]:
    """Rank only positive catalog matches without inventing a fallback tool."""

    cleaned = query.strip().casefold()
    if len(cleaned) < 2 or not 1 <= limit <= 8:
        return ()
    terms = _query_terms(cleaned)
    ranked: list[ToolRequestMatch] = []
    for entry in catalog.entries:
        name = entry.descriptor.model_name
        if name in excluded_names:
            continue
        score = _match_score(cleaned, terms, entry)
        if score > 0:
            ranked.append(ToolRequestMatch(entry=entry, score=score))
    ranked.sort(key=lambda item: (-item.score, item.entry.descriptor.model_name))
    return tuple(ranked[:limit])


def _query_terms(query: str) -> tuple[str, ...]:
    terms: list[str] = _ASCII_TERM.findall(query)
    for run in _CJK_RUN.findall(query):
        terms.append(run)
        for size in (2, 3, 4):
            if len(run) < size:
                continue
            terms.extend(run[index : index + size] for index in range(len(run) - size + 1))
    return tuple(dict.fromkeys(terms))


def _match_score(
    query: str,
    terms: tuple[str, ...],
    entry: UnifiedToolCatalogEntry,
) -> int:
    searchable = entry.searchable_text
    compact_query = _compact(query)
    compact_name = _compact(entry.descriptor.model_name.casefold())
    score = 0
    if compact_query and compact_query == compact_name:
        score += 1_000
    elif compact_query and (compact_query in compact_name or compact_name in compact_query):
        score += 200
    for term in terms:
        if term in searchable:
            score += min(40, len(term) * len(term))
    return score


def _compact(value: str) -> str:
    return "".join(character for character in value if character.isalnum())


__all__ = [
    "REQUEST_TOOLS_NAME",
    "ToolRequestMatch",
    "match_requestable_tools",
    "request_tools_definition",
]
