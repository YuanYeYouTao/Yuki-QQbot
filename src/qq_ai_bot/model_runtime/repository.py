"""Persistence for content-free model invocation telemetry."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from qq_ai_bot.model_runtime.db_models import ModelInvocationModel
from qq_ai_bot.model_runtime.models import ModelInvocationRecord, ModelStats, ModelTask
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.observability import claim_runtime_turn_id


class ModelInvocationRepository:
    """Store and aggregate only task/profile/usage/latency metadata."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def record(
        self,
        *,
        task: ModelTask,
        profile_id: str,
        provider: str,
        model: str,
        success: bool,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        cached_prompt_tokens: int | None,
        latency_seconds: float,
        error_category: str | None,
    ) -> ModelInvocationRecord:
        row = ModelInvocationModel(
            runtime_turn_id=claim_runtime_turn_id(),
            task=task.value,
            profile_id=profile_id,
            provider=provider,
            model=model,
            success=success,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cached_prompt_tokens=cached_prompt_tokens,
            latency_seconds=latency_seconds,
            error_category=error_category,
            created_at=datetime.now(UTC),
        )
        async with self._database.sessions() as session, session.begin():
            session.add(row)
            await session.flush()
            return self._record(row)

    async def stats(self, *, task: ModelTask | None = None) -> ModelStats:
        statement = self._stats_statement()
        if task is not None:
            statement = statement.where(ModelInvocationModel.task == task.value)
        async with self._database.sessions() as session:
            row = (await session.execute(statement)).one()
        return self._stats(row)

    async def stats_by_task(self) -> dict[ModelTask, ModelStats]:
        statement = self._stats_statement(ModelInvocationModel.task).group_by(
            ModelInvocationModel.task
        )
        async with self._database.sessions() as session:
            rows = (await session.execute(statement)).all()
        return {ModelTask(str(row[0])): self._stats(row[1:]) for row in rows}

    async def stats_by_profile(self) -> dict[str, ModelStats]:
        statement = self._stats_statement(ModelInvocationModel.profile_id).group_by(
            ModelInvocationModel.profile_id
        )
        async with self._database.sessions() as session:
            rows = (await session.execute(statement)).all()
        return {str(row[0]): self._stats(row[1:]) for row in rows}

    async def recent_errors(self, *, limit: int) -> tuple[ModelInvocationRecord, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        statement = (
            select(ModelInvocationModel)
            .where(ModelInvocationModel.success.is_(False))
            .order_by(ModelInvocationModel.created_at.desc(), ModelInvocationModel.id.desc())
            .limit(limit)
        )
        async with self._database.sessions() as session:
            rows = (await session.scalars(statement)).all()
        return tuple(self._record(row) for row in rows)

    @staticmethod
    def _stats_statement(group_column: Any | None = None) -> Any:
        columns: list[Any] = []
        if group_column is not None:
            columns.append(group_column)
        columns.extend(
            (
                func.count(ModelInvocationModel.id),
                func.sum(
                    func.cast(ModelInvocationModel.success, type_=ModelInvocationModel.id.type)
                ),
                func.coalesce(func.sum(ModelInvocationModel.prompt_tokens), 0),
                func.coalesce(func.sum(ModelInvocationModel.completion_tokens), 0),
                func.coalesce(func.sum(ModelInvocationModel.total_tokens), 0),
                func.coalesce(func.sum(ModelInvocationModel.cached_prompt_tokens), 0),
                func.sum(
                    func.cast(
                        ModelInvocationModel.total_tokens.is_(None),
                        type_=ModelInvocationModel.id.type,
                    )
                ),
                func.coalesce(func.avg(ModelInvocationModel.latency_seconds), 0.0),
            )
        )
        return select(*columns)

    @staticmethod
    def _stats(row: Sequence[Any]) -> ModelStats:
        values = tuple(row)  # SQLAlchemy Row and sliced Row are both tuple-compatible.
        invocations = int(values[0] or 0)
        successes = int(values[1] or 0)
        return ModelStats(
            invocations=invocations,
            successes=successes,
            failures=invocations - successes,
            prompt_tokens=int(values[2] or 0),
            completion_tokens=int(values[3] or 0),
            total_tokens=int(values[4] or 0),
            cached_prompt_tokens=int(values[5] or 0),
            unknown_usage=int(values[6] or 0),
            average_latency_seconds=float(values[7] or 0),
        )

    @staticmethod
    def _record(row: ModelInvocationModel) -> ModelInvocationRecord:
        return ModelInvocationRecord(
            id=row.id,
            task=ModelTask(row.task),
            profile_id=row.profile_id,
            provider=row.provider,
            model=row.model,
            success=row.success,
            prompt_tokens=row.prompt_tokens,
            completion_tokens=row.completion_tokens,
            total_tokens=row.total_tokens,
            cached_prompt_tokens=row.cached_prompt_tokens,
            latency_seconds=row.latency_seconds,
            error_category=row.error_category,
            created_at=row.created_at,
        )
