"""End-to-end controlled web search and backend source display tests."""

from __future__ import annotations

import json

import pytest
from tests.conftest import MemorySender, build_harness, make_settings
from tests.fakes import FakeWebSearchProvider

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    ChatRequest,
    ChatResponse,
    CitationOrigin,
    InboundMessage,
    NativeToolEvent,
    NativeToolStatus,
    NativeToolType,
    OutboundMessage,
    OutboundSendReceipt,
    ResponseCitation,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.llm.base import LLMProvider
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.web_repository import WebSearchSourceRepository
from qq_ai_bot.web.base import WebSearchError
from qq_ai_bot.web.models import WebMode, WebSearchResponse, WebSearchSource


def event(
    text: str,
    *,
    message_id: str,
    user_id: str = "1001",
    group_id: str | None = None,
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        bot_user_id="8000",
        event_type="message:test",
        scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
        sender=SenderIdentity(user_id=user_id, nickname=f"用户{user_id}"),
        text=text,
        raw_text=text,
        group_id=group_id,
        mentions_bot=group_id is not None,
        segments=({"type": "text", "data": {"text": text}},),
    )


def _request_missing_tool(request: ChatRequest, name: str) -> ChatResponse | None:
    if name in {tool.name for tool in request.tools}:
        return None
    return ChatResponse(
        content="",
        latency_seconds=0,
        tool_calls=(
            ToolCall(
                id=f"request-{name}",
                function=ToolFunction(
                    name="request_tools",
                    arguments=json.dumps(
                        {"query": name, "max_results": 2},
                        ensure_ascii=False,
                    ),
                ),
            ),
        ),
    )


def web_response() -> WebSearchResponse:
    return WebSearchResponse(
        query="最新 DeepSeek 更新",
        sources=(
            WebSearchSource(
                source_id="source-1",
                title="DeepSeek 官方更新",
                url="https://example.com/deepseek-update",
                domain="example.com",
                snippet="官方发布了新版本。",
                relevant_content="官方发布了新版本，并改进了工具调用。",
                provider_score=0.95,
            ),
        ),
        provider_request_id="request-1",
        latency_seconds=0.1,
    )


class WebToolLLM(LLMProvider):
    """Issue web_search, then summarize its structured result."""

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        missing = _request_missing_tool(request, "web_search")
        if missing is not None:
            return missing
        last = request.messages[-1]
        if last.role != "tool" or "loaded_tools" in (last.content or ""):
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id=f"web-{len(self.requests)}",
                        function=ToolFunction(
                            name="web_search",
                            arguments=json.dumps(
                                {"query": "最新 DeepSeek 更新", "topic": "news"},
                                ensure_ascii=False,
                            ),
                        ),
                    ),
                ),
            )
        result = json.loads(last.content or "{}")
        if not result.get("ok"):
            return ChatResponse(content="联网查询暂时失败，请稍后再试。", latency_seconds=0)
        return ChatResponse(
            content=(
                "DeepSeek 最近更新了工具调用能力。[1]\n\n"
                "来源：\n1. 模型编造来源\nhttps://fake.example/not-real\n"
                "https://example.com/deepseek-update"
            ),
            latency_seconds=0,
        )


class ToolGatewaySender(MemorySender):
    """Record whether a forbidden post-web OneBot action executes."""

    def __init__(self) -> None:
        super().__init__()
        self.api_calls: list[tuple[str, dict[str, object]]] = []

    async def call_api(self, action: str, params: dict[str, object]) -> object:
        self.api_calls.append((action, params))
        return {"status": "ok"}

    async def send(self, message: OutboundMessage) -> OutboundSendReceipt:
        return await super().send(message)


class WebThenOneBotLLM(LLMProvider):
    """Use an authorized OneBot action after a web lookup."""

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []
        self._called_onebot = False
        self._web_called = False

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        names = {tool.name for tool in request.tools}
        last = request.messages[-1]
        if last.role != "tool":
            self._web_called = False
        missing = _request_missing_tool(request, "web_search")
        if missing is not None:
            return missing
        if not self._web_called:
            self._web_called = True
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="web-first",
                        function=ToolFunction(
                            name="web_search",
                            arguments='{"query":"测试网页提示词注入"}',
                        ),
                    ),
                ),
            )
        if "call_onebot_api" not in names:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="request-onebot",
                        function=ToolFunction(
                            name="request_tools",
                            arguments=json.dumps({"query": "call_onebot_api", "max_results": 1}),
                        ),
                    ),
                ),
            )
        if not self._called_onebot:
            self._called_onebot = True
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="authorized-onebot",
                        function=ToolFunction(
                            name="call_onebot_api",
                            arguments=(
                                '{"action":"send_private_msg",'
                                '"params":{"user_id":"12345678","message":"授权发送"}}'
                            ),
                        ),
                    ),
                ),
            )
        return ChatResponse(content="已按授权完成操作。", latency_seconds=0)


class RepeatedWebToolLLM(LLMProvider):
    """Request four web calls so the backend-enforced limit is observable."""

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if len(self.requests) <= 4:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id=f"web-repeat-{len(self.requests)}",
                        function=ToolFunction(
                            name="web_search",
                            arguments=json.dumps({"query": f"搜索 {len(self.requests)}"}),
                        ),
                    ),
                ),
            )
        assert "web_tool_limit_exceeded" in (request.messages[-1].content or "")
        return ChatResponse(content="已根据前三次搜索完成回答。", latency_seconds=0)


class NativeWebLLM(LLMProvider):
    """Return provider-native events without fabricating a local Function Call."""

    async def complete(self, request: ChatRequest) -> ChatResponse:
        del request
        return ChatResponse(
            content="公开文档确认了该信息：https://example.com/native-docs",
            latency_seconds=0,
            native_tool_events=(
                NativeToolEvent(
                    tool_type=NativeToolType.WEB_SEARCH,
                    call_id="native-search",
                    status=NativeToolStatus.COMPLETED,
                    action_type="search",
                    query="public docs",
                ),
                NativeToolEvent(
                    tool_type=NativeToolType.WEB_SEARCH,
                    call_id="native-open",
                    status=NativeToolStatus.COMPLETED,
                    action_type="open_page",
                    url="https://example.com/native-docs#ws_call_id=test",
                ),
            ),
            citations=(
                ResponseCitation(
                    url="https://example.com/native-docs",
                    title="Native docs",
                    origin=CitationOrigin.ANNOTATION,
                ),
            ),
        )


class NativeSourceFailureThenTavilyLLM(LLMProvider):
    """Use Tavily immediately when the profile cannot expose native tools."""

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        missing = _request_missing_tool(request, "web_search")
        if missing is not None:
            return missing
        last = request.messages[-1]
        if last.role != "tool" or "loaded_tools" in (last.content or ""):
            assert "web_search" in {tool.name for tool in request.tools}
            assert not request.native_tools
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="tavily-direct",
                        function=ToolFunction(
                            name="web_search",
                            arguments='{"query":"最新 DeepSeek 更新"}',
                        ),
                    ),
                ),
            )
        assert request.messages[-1].role == "tool"
        return ChatResponse(content="已通过备用搜索核验。", latency_seconds=0)


class DomainRoutedTavilyLLM(LLMProvider):
    """Use Tavily immediately when an explicit URL matches a routing rule."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        missing = _request_missing_tool(request, "read_webpage")
        if missing is not None:
            return missing
        last = request.messages[-1]
        if last.role != "tool" or "loaded_tools" in (last.content or ""):
            assert not request.native_tools
            assert "read_webpage" in {tool.name for tool in request.tools}
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="domain-routed-read",
                        function=ToolFunction(
                            name="read_webpage",
                            arguments=json.dumps(
                                {"url": self.url, "question": "这个项目是什么"},
                                ensure_ascii=False,
                            ),
                        ),
                    ),
                ),
            )
        payload = json.loads(request.messages[-1].content or "{}")
        assert payload["ok"] is True
        return ChatResponse(content="这个仓库是 Yuki QQ 机器人项目。", latency_seconds=0)


class TargetMissThenTavilyLLM(LLMProvider):
    """Read through Tavily when native tools are unavailable for the profile."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        missing = _request_missing_tool(request, "read_webpage")
        if missing is not None:
            return missing
        last = request.messages[-1]
        if last.role != "tool" or "loaded_tools" in (last.content or ""):
            assert not request.native_tools
            assert "read_webpage" in {tool.name for tool in request.tools}
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="target-miss-read",
                        function=ToolFunction(
                            name="read_webpage",
                            arguments=json.dumps({"url": self.url}, ensure_ascii=False),
                        ),
                    ),
                ),
            )
        return ChatResponse(content="Tavily 已经读取到指定页面。", latency_seconds=0)


def web_settings(database: Database):
    return make_settings(
        database.url,
        web_enabled=True,
        web_mode=WebMode.TAVILY,
        tavily_api_key="test-placeholder",
    )


@pytest.mark.asyncio
async def test_native_web_sources_are_persisted_before_backend_rendering(
    database: Database,
) -> None:
    settings = make_settings(
        database.url,
        web_enabled=False,
        web_mode=WebMode.NATIVE,
        tavily_api_key="",
    )
    harness = build_harness(database, settings, NativeWebLLM())
    sender = MemorySender()

    result = await harness.processor.handle(
        event("请联网确认并附上来源。", message_id="native-visible"),
        sender,
    )

    assert result.sent_messages == 1
    assert sender.messages[0].text == (
        "公开文档确认了该信息：\n\n来源：\n1. Native docs\n   https://example.com/native-docs"
    )
    stored = await WebSearchSourceRepository(database).for_trigger(
        conversation_key="bot:8000:private:1001",
        trigger_message_id="native-visible",
    )
    assert [source.url for source in stored] == ["https://example.com/native-docs"]


@pytest.mark.asyncio
async def test_chat_completions_profile_uses_tavily_fallback_before_request(
    database: Database,
) -> None:
    settings = make_settings(
        database.url,
        web_enabled=False,
        web_mode=WebMode.NATIVE_WITH_TAVILY_FALLBACK,
        tavily_api_key="test-placeholder",
        tooling_first_round_pin_ids_csv="",
    )
    llm = NativeSourceFailureThenTavilyLLM()
    harness = build_harness(
        database,
        settings,
        llm,
        web_provider=FakeWebSearchProvider(response=web_response()),
    )
    sender = MemorySender()

    result = await harness.processor.handle(
        event("请联网确认并附上来源。", message_id="native-fallback-visible"),
        sender,
    )

    assert result.sent_messages == 1
    assert sender.messages[0].text.startswith("已通过备用搜索核验。\n\n来源：")
    assert "https://example.com/deepseek-update" in sender.messages[0].text
    assert len(llm.requests) == 3


@pytest.mark.asyncio
async def test_explicit_domain_rule_routes_directly_to_tavily(
    database: Database,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    target_url = "https://github.com/YuanYeYouTao/Yuki-QQbot"
    source = WebSearchSource(
        source_id="github-yuki",
        title="Yuki-QQbot",
        url=target_url,
        domain="github.com",
        snippet="Yuki QQ bot repository",
        relevant_content="Yuki-QQbot is a QQ AI Agent project.",
    )
    settings = make_settings(
        database.url,
        web_enabled=False,
        web_mode=WebMode.NATIVE_WITH_TAVILY_FALLBACK,
        tavily_api_key="test-placeholder",
        web_tavily_domains_csv="github.com",
        tooling_first_round_pin_ids_csv="",
    )
    llm = DomainRoutedTavilyLLM(target_url)
    web = FakeWebSearchProvider(extracted={target_url: source})
    harness = build_harness(database, settings, llm, web_provider=web)
    sender = MemorySender()

    result = await harness.processor.handle(
        event(f"请读取 {target_url} 并告诉我这个项目是什么。", message_id="domain-route"),
        sender,
    )

    assert result.reason == "chat"
    assert [message.text for message in sender.messages] == ["这个仓库是 Yuki QQ 机器人项目。"]
    assert web.extract_requests == [(target_url, "这个项目是什么")]
    assert len(llm.requests) == 3
    assert "provider=tavily reason=domain_rule matched_domain=github.com" in caplog.text


@pytest.mark.asyncio
async def test_tavily_keyword_without_verb_routes_directly_to_tavily(
    database: Database,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    settings = make_settings(
        database.url,
        web_enabled=False,
        web_mode=WebMode.NATIVE_WITH_TAVILY_FALLBACK,
        tavily_api_key="test-placeholder",
        tooling_first_round_pin_ids_csv="",
    )
    llm = WebToolLLM()
    web = FakeWebSearchProvider(response=web_response())
    harness = build_harness(database, settings, llm, web_provider=web)
    sender = MemorySender()

    result = await harness.processor.handle(
        event("Tavily搜索立党的最新推文", message_id="tavily-keyword-route"),
        sender,
    )

    assert result.reason == "chat"
    assert len(web.search_requests) == 1
    assert len(llm.requests) == 3
    assert not llm.requests[0].native_tools
    assert "web_search" not in {tool.name for tool in llm.requests[0].tools}
    assert "web_search" in {tool.name for tool in llm.requests[1].tools}
    assert "provider=tavily reason=user_override" in caplog.text


@pytest.mark.asyncio
async def test_chat_completions_url_read_uses_read_webpage(
    database: Database,
) -> None:
    target_url = "https://docs.example.org/required-page"
    source = WebSearchSource(
        source_id="required-page",
        title="Required page",
        url=target_url,
        domain="docs.example.org",
        snippet="Requested content",
        relevant_content="The requested page content.",
    )
    settings = make_settings(
        database.url,
        web_enabled=False,
        web_mode=WebMode.NATIVE_WITH_TAVILY_FALLBACK,
        tavily_api_key="test-placeholder",
        tooling_first_round_pin_ids_csv="",
    )
    llm = TargetMissThenTavilyLLM(target_url)
    web = FakeWebSearchProvider(extracted={target_url: source})
    harness = build_harness(database, settings, llm, web_provider=web)
    sender = MemorySender()

    result = await harness.processor.handle(
        event(f"读取 {target_url} 并总结。", message_id="target-miss-route"),
        sender,
    )

    assert result.reason == "chat"
    assert [message.text for message in sender.messages] == ["Tavily 已经读取到指定页面。"]
    assert web.extract_requests == [(target_url, "读取用户指定的网页")]
    assert len(llm.requests) == 3
    first_names = {tool.name for tool in llm.requests[0].tools}
    assert "read_webpage" not in first_names
    assert "web_search" not in first_names
    assert "read_webpage" in {tool.name for tool in llm.requests[1].tools}
    assert not llm.requests[0].native_tools


@pytest.mark.asyncio
async def test_normal_web_answer_hides_sources_and_model_generated_links(
    database: Database,
) -> None:
    llm = WebToolLLM()
    web = FakeWebSearchProvider(response=web_response())
    harness = build_harness(database, web_settings(database), llm, web_provider=web)
    sender = MemorySender()

    result = await harness.processor.handle(
        event("最近 DeepSeek 有什么更新？", message_id="web-hidden"),
        sender,
    )

    assert result.reason == "chat"
    assert [message.text for message in sender.messages] == ["DeepSeek 最近更新了工具调用能力。"]
    assert web.search_requests[0].query == "最新 DeepSeek 更新"


@pytest.mark.asyncio
async def test_explicit_request_sends_backend_rendered_real_sources(
    database: Database,
) -> None:
    llm = WebToolLLM()
    harness = build_harness(
        database,
        web_settings(database),
        llm,
        web_provider=FakeWebSearchProvider(response=web_response()),
    )
    sender = MemorySender()

    result = await harness.processor.handle(
        event(
            "最近 DeepSeek 有什么更新？请附上来源。",
            message_id="web-visible",
        ),
        sender,
    )

    assert result.sent_messages == 1
    assert sender.messages[0].text == (
        "DeepSeek 最近更新了工具调用能力。\n\n"
        "来源：\n1. DeepSeek 官方更新\n   https://example.com/deepseek-update"
    )
    assert "fake.example" not in "\n".join(message.text for message in sender.messages)


@pytest.mark.asyncio
async def test_source_followup_skips_llm_and_uses_previous_persisted_run(
    database: Database,
) -> None:
    llm = WebToolLLM()
    harness = build_harness(
        database,
        web_settings(database),
        llm,
        web_provider=FakeWebSearchProvider(response=web_response()),
    )
    await harness.processor.handle(
        event("最近 DeepSeek 有什么更新？", message_id="web-first"),
        MemorySender(),
    )
    request_count = len(llm.requests)
    followup_sender = MemorySender()

    result = await harness.processor.handle(
        event("来源呢？", message_id="web-followup"),
        followup_sender,
    )

    assert result.sent_messages == 1
    assert len(llm.requests) == request_count
    assert followup_sender.messages[0].text.startswith("来源：")


@pytest.mark.asyncio
async def test_private_sources_are_isolated_while_group_sources_are_shared(
    database: Database,
) -> None:
    llm = WebToolLLM()
    harness = build_harness(
        database,
        web_settings(database),
        llm,
        web_provider=FakeWebSearchProvider(response=web_response()),
    )
    await harness.processor.handle(
        event("查询更新", message_id="private-owner", user_id="1001"),
        MemorySender(),
    )
    private_other = MemorySender()
    await harness.processor.handle(
        event("来源呢", message_id="private-other", user_id="1002"),
        private_other,
    )
    assert private_other.messages[0].text == "当前对话中没有可提供的联网来源。"

    await harness.processor.handle(
        event("查询更新", message_id="group-owner", user_id="1001", group_id="2001"),
        MemorySender(),
    )
    group_other = MemorySender()
    await harness.processor.handle(
        event("来源呢", message_id="group-other", user_id="1002", group_id="2001"),
        group_other,
    )
    assert "DeepSeek 官方更新" in group_other.messages[0].text


@pytest.mark.asyncio
async def test_web_failure_is_returned_to_llm_for_a_natural_answer(database: Database) -> None:
    llm = WebToolLLM()
    harness = build_harness(
        database,
        web_settings(database),
        llm,
        web_provider=FakeWebSearchProvider(
            error=WebSearchError("provider_unavailable", "联网服务暂不可用")
        ),
    )
    sender = MemorySender()

    result = await harness.processor.handle(
        event("查询最新消息", message_id="web-failure"),
        sender,
    )

    assert result.reason == "chat"
    assert sender.messages[0].text == "联网查询暂时失败，请稍后再试。"


@pytest.mark.asyncio
async def test_web_lookup_can_be_followed_by_superuser_onebot_tool(database: Database) -> None:
    llm = WebThenOneBotLLM()
    harness = build_harness(
        database,
        web_settings(database),
        llm,
        web_provider=FakeWebSearchProvider(response=web_response()),
    )
    sender = ToolGatewaySender()

    result = await harness.processor.handle(
        event("联网查看后回答", message_id="web-admin", user_id="9000"),
        sender,
    )

    assert result.reason == "chat"
    assert sender.api_calls == [
        ("send_private_msg", {"user_id": "12345678", "message": "授权发送"})
    ]
    assert sender.messages[0].text == "已按授权完成操作。"


@pytest.mark.asyncio
async def test_each_turn_executes_at_most_three_web_tools(database: Database) -> None:
    llm = RepeatedWebToolLLM()
    web = FakeWebSearchProvider(response=web_response())
    harness = build_harness(database, web_settings(database), llm, web_provider=web)
    sender = MemorySender()

    result = await harness.processor.handle(
        event("做一个复杂联网研究", message_id="web-limit"),
        sender,
    )

    assert result.reason == "chat"
    assert len(web.search_requests) == 3
    assert sender.messages[0].text == "已根据前三次搜索完成回答。"


def _native_first_settings(database: Database):
    return make_settings(
        database.url,
        web_enabled=True,
        web_mode=WebMode.NATIVE_WITH_TAVILY_FALLBACK,
        tavily_api_key="test-placeholder",
        tooling_first_round_pin_ids_csv="",
    )


@pytest.mark.asyncio
async def test_spoken_search_phrase_exposes_web_search_in_native_first_mode(
    database: Database,
) -> None:
    llm = FakeLLMProvider()
    harness = build_harness(
        database,
        _native_first_settings(database),
        llm,
        web_provider=FakeWebSearchProvider(response=web_response()),
    )
    result = await harness.processor.handle(
        event("这个说法你搜下", message_id="spoken-search"),
        MemorySender(),
    )
    assert result.reason == "chat"
    assert llm.requests
    first = llm.requests[0]
    assert "web_search" not in {tool.name for tool in first.tools}
    assert not first.native_tools


@pytest.mark.asyncio
async def test_native_first_idle_turn_does_not_pin_web_search(database: Database) -> None:
    llm = FakeLLMProvider()
    harness = build_harness(
        database,
        _native_first_settings(database),
        llm,
        web_provider=FakeWebSearchProvider(response=web_response()),
    )
    result = await harness.processor.handle(
        event("在吗", message_id="idle-no-web"),
        MemorySender(),
    )
    assert result.reason == "chat"
    assert llm.requests
    first_names = {tool.name for tool in llm.requests[0].tools}
    assert "web_search" not in first_names
    assert "read_webpage" not in first_names
    assert not llm.requests[0].native_tools


@pytest.mark.asyncio
async def test_native_first_public_url_does_not_pin_read_webpage(
    database: Database,
) -> None:
    llm = FakeLLMProvider()
    harness = build_harness(
        database,
        _native_first_settings(database),
        llm,
        web_provider=FakeWebSearchProvider(response=web_response()),
    )
    result = await harness.processor.handle(
        event("https://docs.example.org/required-page", message_id="url-pin"),
        MemorySender(),
    )
    assert result.reason == "chat"
    assert llm.requests
    first = llm.requests[0]
    names = {tool.name for tool in first.tools}
    assert "read_webpage" not in names
    assert "web_search" not in names
    assert not first.native_tools
