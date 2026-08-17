"""Deterministic mutation reply text (R2 §7 / findings Path C).

Sentences are copied from the 3.5.3 Chat finalizer so reply wording does not
drift.  Chat still decides *when* to finalize until C5; this module owns the
catalog.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from qq_ai_bot.memory.mutation.models import (
    MemoryMutationAppliedOperation,
    MemoryMutationOutcome,
    MemoryMutationResult,
)


@dataclass(frozen=True, slots=True)
class MutationCandidateView:
    """Locator candidate shown in the ambiguous-target reply."""

    fact_id: object = None
    memory_ref: object = None
    key: object = None
    category: object = None
    content: object = None
    status: object = None


@dataclass(frozen=True, slots=True)
class MutationFinalizationInput:
    """Receipt-shaped view.  Quantity and identity stay out of this object."""

    attempted: bool
    ok: bool = False
    applied_operation: str = ""
    outcome: str = ""
    reason_code: str = ""
    error: str = ""
    public_message: str = ""
    candidates: tuple[MutationCandidateView, ...] = ()


def mutation_view_from_tool_result(
    result: Mapping[str, object] | None,
    *,
    attempted: bool,
) -> MutationFinalizationInput:
    """Adapt the Agent-tool JSON envelope Chat already stores."""

    if not attempted or result is None:
        return MutationFinalizationInput(attempted=False)
    data = result.get("data")
    payload = data if isinstance(data, Mapping) else {}
    raw_candidates = payload.get("candidates")
    candidates: list[MutationCandidateView] = []
    if isinstance(raw_candidates, list):
        for item in raw_candidates:
            if not isinstance(item, Mapping):
                continue
            candidates.append(
                MutationCandidateView(
                    fact_id=item.get("fact_id"),
                    memory_ref=item.get("memory_ref"),
                    key=item.get("key"),
                    category=item.get("category"),
                    content=item.get("content"),
                    status=item.get("status"),
                )
            )
    return MutationFinalizationInput(
        attempted=True,
        ok=bool(result.get("ok")),
        applied_operation=str(payload.get("applied_operation") or ""),
        outcome=str(payload.get("outcome") or ""),
        reason_code=str(payload.get("reason_code") or ""),
        error=str(result.get("error") or result.get("error_code") or ""),
        public_message=str(result.get("public_message") or "").strip(),
        candidates=tuple(candidates),
    )


def mutation_view_from_result(
    result: MemoryMutationResult,
    *,
    attempted: bool = True,
) -> MutationFinalizationInput:
    """Adapt the durable mutation service result."""

    if not attempted:
        return MutationFinalizationInput(attempted=False)
    error = "" if result.ok else (result.reason_code or "memory_change_rejected")
    return MutationFinalizationInput(
        attempted=True,
        ok=result.ok,
        applied_operation=result.applied_operation.value,
        outcome=result.outcome.value,
        reason_code=result.reason_code,
        error=error,
        candidates=tuple(
            MutationCandidateView(
                fact_id=candidate.fact_id,
                memory_ref=candidate.memory_ref,
                key=candidate.memory_key,
                category=candidate.category,
                content=candidate.content,
                status=candidate.status.value,
            )
            for candidate in result.candidates
        ),
    )


def finalize_mutation_text(view: MutationFinalizationInput) -> str:
    """Render the deterministic mutation reply.  Do not ask a model."""

    if not view.attempted:
        return "记忆变更未执行，本轮没有取得任何有效的记忆写入回执。"
    if view.error == "memory_candidate_ambiguous":
        lines = ["记忆变更尚未执行：当前条件不能唯一定位目标。"]
        for candidate in view.candidates[:3]:
            memory_ref = candidate.memory_ref or (
                f"M{candidate.fact_id}" if isinstance(candidate.fact_id, int) else "未知引用"
            )
            summary = "｜".join(
                str(value)
                for value in (
                    memory_ref,
                    candidate.key,
                    candidate.category,
                    candidate.content,
                    candidate.status,
                )
                if value not in (None, "")
            )
            if summary:
                lines.append(f"- {summary}")
        lines.append("请明确选择其中一条后再试。")
        return "\n".join(lines)
    if view.error == "memory_candidate_not_found":
        return "记忆变更未执行：在当前合法作用域内没有找到可唯一定位的目标。"
    if not view.ok:
        detail = view.public_message or "记忆变更未执行"
        return f"{detail}（reason_code={view.error or view.reason_code or 'unknown'}）"
    if (
        view.applied_operation == MemoryMutationAppliedOperation.NOOP.value
        or view.outcome == MemoryMutationOutcome.NO_CHANGE.value
    ):
        return "记忆状态没有发生变化，本轮没有完成新的覆盖、撤回或恢复。"
    if (
        view.outcome == MemoryMutationOutcome.COMMITTED_AS_CONTESTED.value
        or view.applied_operation == MemoryMutationAppliedOperation.CONTEST.value
    ):
        return "该记忆存在冲突，已标记为有争议；没有按原请求直接覆盖或删除。"
    if (
        view.applied_operation == MemoryMutationAppliedOperation.MERGE_EVIDENCE.value
        or view.outcome == MemoryMutationOutcome.DEDUPLICATED.value
    ):
        return "这条信息已经存在，本轮只合并了证据，没有创建重复记忆。"
    if view.applied_operation == MemoryMutationAppliedOperation.CREATE.value:
        return "已将这条信息写入长期记忆。"
    if view.applied_operation == MemoryMutationAppliedOperation.CORRECT.value:
        return "记忆已按你的要求纠正，旧版本不再作为当前有效答案。"
    if view.applied_operation == MemoryMutationAppliedOperation.INVALIDATE.value:
        return "这条记忆已撤回并失效；审计记录仍保留，但不会再作为有效记忆使用。"
    if view.applied_operation == MemoryMutationAppliedOperation.RESTORE.value:
        return "这条记忆已恢复为有效状态。"
    if view.applied_operation == MemoryMutationAppliedOperation.MERGE.value:
        return "相关记忆已经合并，重复版本不再分别作为有效记忆使用。"
    if view.applied_operation == MemoryMutationAppliedOperation.REASSIGN.value:
        return "记忆归属已经按真实回执更新。"
    if view.applied_operation == MemoryMutationAppliedOperation.UPDATE_METADATA.value:
        return "记忆元数据已经按真实回执更新。"
    return f"记忆变更已完成（outcome={view.outcome or 'committed'}）。"
