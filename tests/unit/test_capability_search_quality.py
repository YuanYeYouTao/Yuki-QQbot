"""R3 search quality and warm-index latency gates (synthetic corpus only)."""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass

from qq_ai_bot.capabilities.models import CapabilityEffect, CapabilityRisk, CapabilityTrustSource
from qq_ai_bot.capabilities.provider import _CORE_METADATA, _CORE_SEARCH_TAGS, _CORE_USE_WHEN
from qq_ai_bot.capabilities.search_document import CapabilitySearchDocument
from qq_ai_bot.capabilities.search_index import FtsCapabilitySearchIndex, _query_terms

K = 8
TUNE_FRACTION = 0.8
COMMON_RECALL_MIN = 0.95
OVERALL_RECALL_MIN = 0.90
P50_MS_MAX = 10.0
P95_MS_MAX = 25.0


@dataclass(frozen=True, slots=True)
class SearchCase:
    query: str
    required: frozenset[str]
    bucket: str
    common: bool = True


def _document(
    *,
    capability_id: str,
    namespace_id: str,
    description: str,
    aliases: tuple[str, ...] = (),
    tags: tuple[str, ...] = (),
    use_when: tuple[str, ...] = (),
    trust_source: CapabilityTrustSource,
    synthetic: bool = False,
) -> CapabilitySearchDocument:
    return CapabilitySearchDocument(
        capability_id=capability_id,
        model_name=capability_id,
        canonical_name=capability_id,
        namespace_id=namespace_id,
        description=description,
        aliases=aliases,
        tags=tags,
        use_when=use_when,
        trust_source=trust_source,
        effect=CapabilityEffect.READ_STATE,
        risk=CapabilityRisk.READ,
    )


def _core_documents() -> tuple[CapabilitySearchDocument, ...]:
    documents: list[CapabilitySearchDocument] = []
    for name, (namespace, _effect, _risk) in _CORE_METADATA.items():
        documents.append(
            _document(
                capability_id=name,
                namespace_id=namespace,
                description=_CORE_USE_WHEN.get(name, (name,))[0],
                aliases=_CORE_SEARCH_TAGS.get(name, ()),
                tags=(namespace.split(".")[0],),
                use_when=_CORE_USE_WHEN.get(name, ()),
                trust_source=CapabilityTrustSource.CORE,
            )
        )
    return tuple(documents)


def _plugin_documents() -> tuple[CapabilitySearchDocument, ...]:
    families = (
        ("music", "search_song", "搜索歌曲", ("搜歌", "网易云", "点歌"), "music.search"),
        ("music", "play_album", "播放专辑", ("专辑", "album"), "music.play"),
        ("github", "list_issues", "列出仓库议题", ("issue", "议题"), "github.issues"),
        ("github", "create_issue", "创建仓库议题", ("开issue", "报bug"), "github.issues"),
        ("weather", "forecast", "查询天气预报", ("天气", "forecast"), "weather.read"),
        ("translate", "translate_text", "翻译文本", ("翻译", "translate"), "translate.run"),
        ("calendar", "create_event", "创建日历事件", ("日程", "calendar"), "calendar.write"),
        ("rss", "fetch_feed", "读取订阅源", ("rss", "订阅"), "rss.read"),
    )
    documents: list[CapabilitySearchDocument] = []
    for index in range(80):
        family, local, description, aliases, namespace = families[index % len(families)]
        suffix = index // len(families)
        name = f"plugin__{family}{suffix}__{local}"
        documents.append(
            _document(
                capability_id=name,
                namespace_id=namespace,
                description=f"{description} {suffix}",
                aliases=(*aliases, f"{aliases[0]}{suffix}" if suffix else aliases[0]),
                tags=(family, "plugin"),
                use_when=(description, aliases[0]),
                trust_source=CapabilityTrustSource.PLUGIN,
            )
        )
    return tuple(documents)


def _mcp_documents() -> tuple[CapabilitySearchDocument, ...]:
    documents: list[CapabilitySearchDocument] = []
    for index in range(80):
        server = f"server{index // 4}"
        if index % 4 == 0:
            documents.append(
                _document(
                    capability_id=f"mcp__{server}__discover",
                    namespace_id=f"mcp.{server}",
                    description=f"discover tools on {server}",
                    aliases=(server, f"{server} mcp"),
                    tags=("mcp", "discover"),
                    use_when=(f"连接 {server}", f"use {server} tools"),
                    trust_source=CapabilityTrustSource.MCP,
                    synthetic=True,
                )
            )
            continue
        tool = f"remote_tool_{index}"
        documents.append(
            _document(
                capability_id=f"mcp__{server}__{tool}",
                namespace_id=f"mcp.{server}",
                description=f"{server} {tool} fetches remote records",
                aliases=(tool.replace("_", " "), f"{server} {tool}"),
                tags=("mcp", server),
                use_when=(f"调用 {tool}", f"call {tool} on {server}"),
                trust_source=CapabilityTrustSource.MCP,
            )
        )
    return tuple(documents)


def _core_cases() -> list[SearchCase]:
    cases: list[SearchCase] = []
    for name, (_namespace, _effect, _risk) in _CORE_METADATA.items():
        cases.append(SearchCase(name, frozenset({name}), "core", common=True))
        seen: set[str] = {name.casefold()}
        for phrase in (*_CORE_USE_WHEN.get(name, ()), *_CORE_SEARCH_TAGS.get(name, ())):
            key = phrase.casefold()
            if key in seen or len(phrase) < 2:
                continue
            seen.add(key)
            cases.append(SearchCase(phrase, frozenset({name}), "core", common=True))
    unique: list[SearchCase] = []
    used_queries: set[str] = set()
    for case in cases:
        if case.query in used_queries:
            continue
        used_queries.add(case.query)
        unique.append(case)
    names = tuple(_CORE_METADATA)
    cursor = 0
    while len(unique) < 100:
        name = names[cursor % len(names)]
        unique.append(SearchCase(name, frozenset({name}), "core", common=True))
        cursor += 1
    return unique[:100]


def _plugin_cases(documents: tuple[CapabilitySearchDocument, ...]) -> list[SearchCase]:
    cases: list[SearchCase] = []
    for document in documents:
        cases.append(
            SearchCase(document.capability_id, frozenset({document.capability_id}), "plugin")
        )
    for document in documents:
        if len(cases) >= 80:
            break
        unique_alias = f"{document.aliases[0]} {document.capability_id}"
        cases.append(SearchCase(unique_alias, frozenset({document.capability_id}), "plugin"))
    return cases[:80]


def _mcp_cases(documents: tuple[CapabilitySearchDocument, ...]) -> list[SearchCase]:
    cases: list[SearchCase] = []
    for document in documents:
        cases.append(SearchCase(document.capability_id, frozenset({document.capability_id}), "mcp"))
    for document in documents:
        if len(cases) >= 80:
            break
        cases.append(
            SearchCase(
                f"{document.capability_id} {document.namespace_id}",
                frozenset({document.capability_id}),
                "mcp",
            )
        )
    return cases[:80]


def _bilingual_cases() -> list[SearchCase]:
    pairs = (
        ("web_search 联网搜索", "web_search"),
        ("read_webpage 打开网页", "read_webpage"),
        ("memory_change 记住这件事", "memory_change"),
        ("get_person_memories person memory", "get_person_memories"),
        ("get_group_memories 群记忆", "get_group_memories"),
        ("get_relationship 好感度", "get_relationship"),
        ("get_recent_chat_history recent chat", "get_recent_chat_history"),
        ("search_chat_history old messages", "search_chat_history"),
        ("send_voice 语音回复", "send_voice"),
        ("set_reply_target quote message", "set_reply_target"),
        ("get_my_capabilities permissions", "get_my_capabilities"),
        ("call_onebot_api 禁言 mute", "call_onebot_api"),
        ("plugin__weather0__forecast 天气", "plugin__weather0__forecast"),
        ("plugin__translate0__translate_text 翻译", "plugin__translate0__translate_text"),
        ("plugin__music0__search_song 网易云", "plugin__music0__search_song"),
        ("plugin__github0__list_issues github", "plugin__github0__list_issues"),
        ("mcp__server0__remote_tool_1 call", "mcp__server0__remote_tool_1"),
        ("mcp__server1__discover use server1", "mcp__server1__discover"),
        ("plugin__rss0__fetch_feed rss 订阅", "plugin__rss0__fetch_feed"),
        ("plugin__calendar0__create_event 日程", "plugin__calendar0__create_event"),
        ("read_webpage URL link", "read_webpage"),
        ("memory_change forget 忘记", "memory_change"),
        ("get_self_memories 你记得", "get_self_memories"),
        ("read_tool_artifact artifact", "read_tool_artifact"),
        ("plugin__music0__play_album album 专辑", "plugin__music0__play_album"),
        ("plugin__github0__create_issue 报bug", "plugin__github0__create_issue"),
        ("mcp__server2__discover mcp server2", "mcp__server2__discover"),
        ("mcp__server1__remote_tool_5 fetches", "mcp__server1__remote_tool_5"),
        ("plugin__music1__search_song 点歌", "plugin__music1__search_song"),
        ("plugin__weather1__forecast forecast", "plugin__weather1__forecast"),
        ("plugin__translate1__translate_text translate", "plugin__translate1__translate_text"),
        ("plugin__github1__list_issues issues", "plugin__github1__list_issues"),
        ("web_search search the web", "web_search"),
        ("send_voice voice 朗读", "send_voice"),
        ("get_relationship relationship score", "get_relationship"),
        ("get_group_memories group memory", "get_group_memories"),
        ("get_recent_chat_history 刚才说了什么", "get_recent_chat_history"),
        ("get_person_memories 关于她", "get_person_memories"),
        ("get_my_capabilities 权限范围", "get_my_capabilities"),
        ("call_onebot_api 踢人", "call_onebot_api"),
    )
    return [
        SearchCase(query, frozenset({required}), "bilingual", common=True)
        for query, required in pairs
    ]


def _corpus() -> tuple[tuple[CapabilitySearchDocument, ...], list[SearchCase]]:
    core = _core_documents()
    plugins = _plugin_documents()
    mcp = _mcp_documents()
    documents = (*core, *plugins, *mcp)
    cases = [
        *_core_cases(),
        *_plugin_cases(plugins),
        *_mcp_cases(mcp),
        *_bilingual_cases(),
    ]
    assert len(cases) >= 300, len(cases)
    cases = cases[:300]
    assert sum(item.bucket == "core" for item in cases) == 100
    assert sum(item.bucket == "plugin" for item in cases) == 80
    assert sum(item.bucket == "mcp" for item in cases) == 80
    assert sum(item.bucket == "bilingual" for item in cases) == 40
    return documents, cases


def _hit(index: FtsCapabilitySearchIndex, case: SearchCase) -> bool:
    hits = index.search(case.query, limit=K)
    found = {hit.capability_id for hit in hits}
    return case.required.issubset(found)


def test_capability_search_quality_and_warm_latency() -> None:
    documents, cases = _corpus()
    index = FtsCapabilitySearchIndex()
    index.rebuild(revision="search-quality-v1", documents=documents)
    # Warm the connection with one discarded query before measuring.
    index.search("web_search", limit=K)

    split = int(len(cases) * TUNE_FRACTION)
    tune, holdout = cases[:split], cases[split:]
    latencies_ms: list[float] = []
    hits = 0
    common_total = 0
    common_hits = 0
    for case in cases:
        started = time.perf_counter()
        matched = _hit(index, case)
        latencies_ms.append((time.perf_counter() - started) * 1000)
        hits += int(matched)
        if case.common:
            common_total += 1
            common_hits += int(matched)

    overall = hits / len(cases)
    common = common_hits / common_total
    holdout_hits = sum(int(_hit(index, case)) for case in holdout)
    tune_hits = sum(int(_hit(index, case)) for case in tune)
    assert common >= COMMON_RECALL_MIN, f"common recall {common:.3f}"
    assert overall >= OVERALL_RECALL_MIN, f"overall recall {overall:.3f}"
    assert tune_hits / len(tune) >= OVERALL_RECALL_MIN
    assert holdout_hits / len(holdout) >= OVERALL_RECALL_MIN

    ordered = sorted(latencies_ms)
    p50 = statistics.median(ordered)
    p95 = ordered[max(0, round(0.95 * (len(ordered) - 1)))]
    assert p50 < P50_MS_MAX, f"p50 {p50:.3f}ms"
    assert p95 < P95_MS_MAX, f"p95 {p95:.3f}ms"


def test_wrong_namespace_does_not_block_correct_tool() -> None:
    documents, _cases = _corpus()
    index = FtsCapabilitySearchIndex()
    index.rebuild(revision="ns-hard-negative", documents=documents)
    hits = index.search("网易云 搜歌", limit=K)
    assert any(hit.capability_id.startswith("plugin__music") for hit in hits)
    assert all(hit.namespace_id != "web.search" or hit.score >= 0 for hit in hits)


def test_zero_result_queries_are_evaluated_separately() -> None:
    documents, _cases = _corpus()
    index = FtsCapabilitySearchIndex()
    index.rebuild(revision="zero-result", documents=documents)
    misses = (
        "xyzzyplugh unrelated widget",
        "qqqqzzzz not a real capability",
        "foobar-unindexed-token-aa",
    )
    zero = sum(1 for query in misses if not index.search(query, limit=K))
    assert zero == len(misses)


def test_long_chinese_order_query_hits_mcp_bundle_summary() -> None:
    query = "帮我点麦辣鸡腿堡，到店取餐，创建待支付订单，把链接发给我。"
    terms = _query_terms(query)
    assert "创建待支付订单" in terms
    assert "订单" in terms
    document = _document(
        capability_id="mcp__mcd__discover",
        namespace_id="mcp.mcd",
        description="MCP Server mcd 完整查询、校价和创建待支付订单",
        aliases=("mcd", "create-order"),
        use_when=("完整查询、校价和创建待支付订单",),
        trust_source=CapabilityTrustSource.MCP,
        synthetic=True,
    )
    index = FtsCapabilitySearchIndex()
    index.rebuild(revision="mcd-bundle", documents=(_core_documents()[0], document))
    hits = index.search(query, limit=K)
    assert any(hit.capability_id == "mcp__mcd__discover" for hit in hits)
