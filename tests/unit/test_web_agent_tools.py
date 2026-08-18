"""Agent web tools, source persistence, retention, and isolation tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, update
from tests.conftest import make_settings
from tests.fakes import FakeWebSearchProvider

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import WebSearchRunModel
from qq_ai_bot.persistence.repositories import (
    AgentActionRepository,
    EventLedgerRepository,
    WebSearchSourceRepository,
)
from qq_ai_bot.services.agent_tools import AgentToolService, ToolRuntime
from qq_ai_bot.web.base import WebSearchError
from qq_ai_bot.web.models import WebMode, WebSearchResponse, WebSearchSource


def source(
    url: str = "https://example.com/article",
    *,
    content: str = "查询相关正文",
) -> WebSearchSource:
    return WebSearchSource(
        source_id="source-1",
        title="真实页面",
        url=url,
        domain="example.com",
        snippet="搜索摘要",
        relevant_content=content,
        provider_score=0.9,
    )


def inbound(text: str = "请联网查询") -> InboundMessage:
    return InboundMessage(
        message_id="trigger-1",
        bot_user_id="8000",
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001", nickname="用户"),
        text=text,
        raw_text=text,
        segments=({"type": "text", "data": {"text": text}},),
    )


def build_tools(
    database: Database,
    *,
    provider: FakeWebSearchProvider | None,
    enabled: bool = True,
    result_limit: int = 16000,
) -> tuple[AgentToolService, WebSearchSourceRepository]:
    settings = make_settings(
        database.url,
        web_enabled=enabled,
        web_mode=(WebMode.TAVILY if enabled else WebMode.DISABLED),
        tavily_api_key="test-placeholder" if enabled else "",
        web_tool_result_max_characters=result_limit,
    )
    sources = WebSearchSourceRepository(database)
    return (
        AgentToolService(
            settings=settings,
            ledger=EventLedgerRepository(database),
            memories=MemoryFactService(MemoryFactRepository(database)),
            actions=AgentActionRepository(database),
            web_provider=provider,
            web_sources=sources,
        ),
        sources,
    )


def runtime(
    message: InboundMessage | None = None,
    *,
    conversation: str = "private:1001",
    native_web_fallback: bool = False,
) -> ToolRuntime:
    event = message or inbound()
    return ToolRuntime(
        inbound=event,
        gateway=None,
        allow_generic_onebot=False,
        conversation_key=conversation,
        trigger_message_id=event.message_id,
        source_display_requested=False,
        native_web_fallback=native_web_fallback,
    )


@pytest.mark.asyncio
async def test_web_search_persists_current_sources_with_strict_isolation(
    database: Database,
) -> None:
    provider = FakeWebSearchProvider(
        response=WebSearchResponse(
            query="ignored",
            sources=(source(),),
            provider_request_id="request-1",
            latency_seconds=0.1,
        )
    )
    tools, repository = build_tools(database, provider=provider)

    result = json.loads(
        await tools.execute(
            "web_search",
            json.dumps({"query": "当前软件版本", "topic": "general"}),
            runtime(),
        )
    )

    assert result["ok"]
    assert result["data"]["external_untrusted"] is True
    stored = await repository.for_trigger(
        conversation_key="private:1001",
        trigger_message_id="trigger-1",
    )
    assert [item.url for item in stored] == ["https://example.com/article"]
    assert not await repository.for_trigger(
        conversation_key="private:1002",
        trigger_message_id="trigger-1",
    )


@pytest.mark.asyncio
async def test_get_my_capabilities_is_event_bound_and_accepts_no_target(
    database: Database,
) -> None:
    tools, _repository = build_tools(database, provider=None, enabled=False)
    event = inbound("我能修改什么？")
    bound_runtime = ToolRuntime(
        inbound=event,
        gateway=None,
        allow_generic_onebot=False,
        conversation_key="private:1001",
        trigger_message_id=event.message_id,
        actor_user_id="1001",
        actor_is_superuser=False,
        current_group_id=None,
        mentioned_user_ids=(),
    )
    assert "get_my_capabilities" in {
        definition.name for definition in tools.definitions(bound_runtime)
    }

    report = json.loads(await tools.execute("get_my_capabilities", "{}", bound_runtime))
    assert report["ok"]
    assert report["data"]["permission_level"] == "user"
    assert report["data"]["counts"]["mutable_configurations"] == 0
    assert report["data"]["counts"]["self_service_operations"] == 37

    targeted = json.loads(
        await tools.execute(
            "get_my_capabilities",
            json.dumps({"user_id": "9000"}),
            bound_runtime,
        )
    )
    assert targeted["error"] == "invalid_arguments"

    forged_runtime = ToolRuntime(
        inbound=event,
        gateway=None,
        allow_generic_onebot=False,
        conversation_key="private:1001",
        trigger_message_id=event.message_id,
        actor_user_id="9000",
        actor_is_superuser=True,
        current_group_id=None,
        mentioned_user_ids=(),
    )
    forged = json.loads(await tools.execute("get_my_capabilities", "{}", forged_runtime))
    assert forged["error"] == "permission_context_mismatch"


def test_web_enabled_false_does_not_register_web_tools(database: Database) -> None:
    tools, _repository = build_tools(database, provider=None, enabled=False)
    names = {definition.name for definition in tools.definitions(runtime())}
    assert "web_search" not in names
    assert "read_webpage" not in names


@pytest.mark.asyncio
async def test_read_webpage_requires_explicit_or_current_search_url(database: Database) -> None:
    page = source()
    provider = FakeWebSearchProvider(extracted={page.url: page})
    tools, repository = build_tools(database, provider=provider)

    denied = json.loads(
        await tools.execute(
            "read_webpage",
            json.dumps({"url": page.url}),
            runtime(inbound("没有发送网址")),
        )
    )
    assert denied["error"] == "url_not_authorized"

    message = inbound(f"请阅读 {page.url}")
    allowed = json.loads(
        await tools.execute(
            "read_webpage",
            json.dumps({"url": page.url, "question": "页面说了什么"}),
            runtime(message),
        )
    )
    assert allowed["ok"]
    assert await repository.for_trigger(
        conversation_key="private:1001",
        trigger_message_id="trigger-1",
    )


@pytest.mark.asyncio
async def test_web_tool_returns_structured_error_without_raising(database: Database) -> None:
    tools, _repository = build_tools(
        database,
        provider=FakeWebSearchProvider(
            error=WebSearchError("rate_limited", "联网服务请求过于频繁")
        ),
    )
    result = json.loads(
        await tools.execute(
            "web_search",
            json.dumps({"query": "最新消息"}),
            runtime(),
        )
    )
    assert result == {
        "ok": False,
        "error": "rate_limited",
        "detail": "联网服务请求过于频繁",
    }


@pytest.mark.asyncio
async def test_web_tool_result_never_exceeds_configured_limit(database: Database) -> None:
    provider = FakeWebSearchProvider(
        response=WebSearchResponse(
            query="ignored",
            sources=tuple(
                source(f"https://example.com/{index}", content="正文" * 2000) for index in range(3)
            ),
            provider_request_id=None,
            latency_seconds=0,
        )
    )
    tools, _repository = build_tools(database, provider=provider, result_limit=2000)
    rendered = await tools.execute(
        "web_search",
        json.dumps({"query": "长度测试"}),
        runtime(),
    )
    assert len(rendered) <= 2000
    assert json.loads(rendered)["ok"] in {True, False}


@pytest.mark.asyncio
async def test_run_limit_retention_cleanup_and_database_restart(tmp_path: Path) -> None:
    path = (tmp_path / "web-sources.db").as_posix()
    database = Database(f"sqlite+aiosqlite:///{path}")
    await database.create_schema()
    repository = WebSearchSourceRepository(database)
    for index in range(11):
        await repository.save_response(
            conversation_key="private:1001",
            trigger_message_id=f"trigger-{index}",
            provider="tavily",
            response=WebSearchResponse(
                query=f"query-{index}",
                sources=(source(f"https://example.com/{index}"),),
                provider_request_id=None,
                latency_seconds=0,
            ),
            max_runs=10,
        )
    async with database.sessions() as session:
        count = await session.scalar(select(func.count(WebSearchRunModel.id)))
        assert count == 10
        await session.execute(
            update(WebSearchRunModel).values(created_at=datetime.now(UTC) - timedelta(days=8))
        )
        await session.commit()
    await database.close()

    reopened = Database(f"sqlite+aiosqlite:///{path}")
    repository = WebSearchSourceRepository(reopened)
    assert await repository.latest("private:1001")
    assert await repository.cleanup_expired(retention_days=7) == 10
    assert not await repository.latest("private:1001")
    await reopened.close()


def test_native_first_mode_catalogs_web_search_before_tavily_fallback(database: Database) -> None:
    settings = make_settings(
        database.url,
        web_enabled=True,
        web_mode=WebMode.NATIVE_WITH_TAVILY_FALLBACK,
        tavily_api_key="test-placeholder",
    )
    sources = WebSearchSourceRepository(database)
    tools = AgentToolService(
        settings=settings,
        ledger=EventLedgerRepository(database),
        memories=MemoryFactService(MemoryFactRepository(database)),
        actions=AgentActionRepository(database),
        web_provider=FakeWebSearchProvider(),
        web_sources=sources,
    )
    names = {tool.name for tool in tools.definitions(runtime(native_web_fallback=False))}
    assert {"web_search", "read_webpage"} <= names


def test_disabled_web_mode_omits_web_tools_from_catalog(database: Database) -> None:
    tools, _sources = build_tools(database, provider=FakeWebSearchProvider(), enabled=False)
    names = {tool.name for tool in tools.definitions(runtime())}
    assert "web_search" not in names
    assert "read_webpage" not in names
