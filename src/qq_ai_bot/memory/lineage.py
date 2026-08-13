"""Paged evidence lineage expansion for administration, audits, and Dream inspection."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.memory.dream.db_models import (
    MemoryDreamOperationModel,
    MemoryDreamOperationResultModel,
    MemoryDreamOperationSourceModel,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryEvidenceModel,
    MemorySelfReflectionResultModel,
    MemorySelfReflectionRunModel,
    MemoryToolReceiptModel,
)


class MemoryLineageItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    fact_id: int = Field(gt=0)
    source_fact_id: int | None = Field(default=None, gt=0)
    event_id: int | None = Field(default=None, gt=0)
    tool_receipt_id: int | None = Field(default=None, gt=0)
    operation_public_id: str | None = None
    excerpt: str | None = None


class MemoryLineagePage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[MemoryLineageItem, ...]
    next_cursor: str | None = None


class MemoryLineageService:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_evidence_lineage(
        self,
        fact_id: int,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> MemoryLineagePage:
        bounded = max(1, min(50, limit))
        try:
            offset = max(0, int(cursor or "0"))
        except ValueError as exc:
            raise ValueError("memory lineage cursor is invalid") from exc
        async with self._database.sessions() as session:
            items = await self._items(fact_id, session=session, visited=set(), depth=0)
        page = items[offset : offset + bounded]
        next_offset = offset + len(page)
        return MemoryLineagePage(
            items=tuple(page),
            next_cursor=str(next_offset) if next_offset < len(items) else None,
        )

    async def _items(
        self,
        fact_id: int,
        *,
        session: AsyncSession,
        visited: set[int],
        depth: int,
    ) -> list[MemoryLineageItem]:
        if fact_id in visited or depth > 2:
            return []
        visited.add(fact_id)
        rows: list[MemoryLineageItem] = []
        direct = tuple(
            (
                await session.scalars(
                    select(MemoryEvidenceModel)
                    .where(MemoryEvidenceModel.fact_id == fact_id)
                    .order_by(MemoryEvidenceModel.created_at, MemoryEvidenceModel.id)
                )
            ).all()
        )
        rows.extend(
            MemoryLineageItem(
                kind="direct_evidence",
                fact_id=fact_id,
                event_id=item.event_id,
                tool_receipt_id=item.tool_receipt_id,
                excerpt=item.excerpt,
            )
            for item in direct
        )
        mappings = tuple(
            (
                await session.scalars(
                    select(MemorySelfReflectionResultModel).where(
                        MemorySelfReflectionResultModel.fact_id == fact_id
                    )
                )
            ).all()
        )
        for mapping in mappings:
            run = await session.get(MemorySelfReflectionRunModel, mapping.run_id)
            first_event = None
            if run is not None:
                first_event = await session.get(ChatEventModel, run.first_event_id)
            if run is not None and first_event is not None:
                query = select(ChatEventModel).where(
                    ChatEventModel.bot_user_id == run.bot_user_id,
                    ChatEventModel.id >= run.first_event_id,
                    ChatEventModel.id <= run.last_event_id,
                )
                if first_event.scope_type == "group":
                    query = query.where(ChatEventModel.group_id == first_event.group_id)
                else:
                    query = query.where(
                        ChatEventModel.private_peer_user_id == first_event.private_peer_user_id
                    )
                events = tuple((await session.scalars(query.order_by(ChatEventModel.id))).all())
                rows.extend(
                    MemoryLineageItem(
                        kind="reflection_window",
                        fact_id=fact_id,
                        event_id=event.id,
                        excerpt=event.content,
                    )
                    for event in events
                )
                tool_rows = tuple(
                    (
                        await session.scalars(
                            select(MemoryToolReceiptModel)
                            .where(
                                MemoryToolReceiptModel.bot_user_id == run.bot_user_id,
                                MemoryToolReceiptModel.conversation_key_hash
                                == run.conversation_key_hash,
                                MemoryToolReceiptModel.trigger_event_id >= run.first_event_id,
                                MemoryToolReceiptModel.trigger_event_id <= run.last_event_id,
                            )
                            .order_by(MemoryToolReceiptModel.id)
                        )
                    ).all()
                )
                rows.extend(
                    MemoryLineageItem(
                        kind="reflection_tool_window",
                        fact_id=fact_id,
                        tool_receipt_id=tool.id,
                        excerpt=tool.result_excerpt,
                    )
                    for tool in tool_rows
                )
        result = await session.scalar(
            select(MemoryDreamOperationResultModel)
            .where(MemoryDreamOperationResultModel.fact_id == fact_id)
            .order_by(MemoryDreamOperationResultModel.id.desc())
            .limit(1)
        )
        if result is not None:
            operation = await session.get(MemoryDreamOperationModel, result.operation_id)
            sources = tuple(
                await session.scalars(
                    select(MemoryDreamOperationSourceModel.fact_id)
                    .where(MemoryDreamOperationSourceModel.operation_id == result.operation_id)
                    .order_by(MemoryDreamOperationSourceModel.position)
                )
            )
            for source_id in sources:
                rows.append(
                    MemoryLineageItem(
                        kind="dream_source_fact",
                        fact_id=fact_id,
                        source_fact_id=int(source_id),
                        operation_public_id=(
                            operation.public_id if operation is not None else None
                        ),
                    )
                )
                rows.extend(
                    await self._items(
                        int(source_id),
                        session=session,
                        visited=visited,
                        depth=depth + 1,
                    )
                )
        return rows
