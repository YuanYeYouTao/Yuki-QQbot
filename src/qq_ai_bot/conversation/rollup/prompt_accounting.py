"""Prompt-renderer character accounting for rollup watermarks.

Compression still uses ``rollup_source_projection``. Watermarks, uncovered
counters, and batch cuts use the same grouped ``main_agent_history`` ruler as
the foreground assembler.
"""

from __future__ import annotations

from collections.abc import Iterable

from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.persistence.repository_records import EventRecord

DEFAULT_BOT_DISPLAY_NAME = "Yuki"
DEFAULT_TIMEZONE = "Asia/Shanghai"


def prompt_accounting_characters(
    events: Iterable[EventRecord],
    *,
    bot_display_name: str = DEFAULT_BOT_DISPLAY_NAME,
    timezone: str = DEFAULT_TIMEZONE,
) -> int:
    """Return grouped main-agent history characters for one event suffix."""

    rows = tuple(events)
    renderer = ChatEventPromptRenderer(
        rows,
        bot_display_name=bot_display_name,
        timezone=timezone,
    )
    rendered = renderer.main_agent_history(rows)
    return sum(len(item.content or "") for _, _, item in rendered)


def prompt_accounting_event_characters(
    event: EventRecord,
    *,
    events: Iterable[EventRecord] = (),
    bot_display_name: str = DEFAULT_BOT_DISPLAY_NAME,
    timezone: str = DEFAULT_TIMEZONE,
) -> int:
    """Return ungrouped reference-event characters as an append increment bound."""

    context = tuple(events)
    if all(row.id != event.id for row in context):
        context = (*context, event)
    renderer = ChatEventPromptRenderer(
        context,
        bot_display_name=bot_display_name,
        timezone=timezone,
    )
    return len(renderer.render_reference_event(event))
