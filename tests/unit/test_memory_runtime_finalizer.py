"""Unit tests for deterministic mutation finalization (R2 commit 4)."""

from __future__ import annotations

from qq_ai_bot.memory.enums import MemoryKind, MemoryStatus
from qq_ai_bot.memory.mutation.models import (
    MemoryMutationAppliedOperation,
    MemoryMutationCandidate,
    MemoryMutationOperation,
    MemoryMutationOutcome,
    MemoryMutationResult,
)
from qq_ai_bot.memory.runtime.finalizer import (
    MutationFinalizationInput,
    finalize_mutation_text,
    mutation_view_from_result,
    mutation_view_from_tool_result,
)


class TestFinalizeMutationText:
    def test_not_attempted(self) -> None:
        assert (
            finalize_mutation_text(MutationFinalizationInput(attempted=False))
            == "记忆变更未执行，本轮没有取得任何有效的记忆写入回执。"
        )

    def test_ambiguous_lists_up_to_three_candidates(self) -> None:
        text = finalize_mutation_text(
            mutation_view_from_tool_result(
                {
                    "ok": False,
                    "error": "memory_candidate_ambiguous",
                    "data": {
                        "candidates": [
                            {
                                "fact_id": 11,
                                "memory_ref": "M11",
                                "key": "hobby",
                                "category": "pref",
                                "content": "咖啡",
                                "status": "active",
                            },
                            {
                                "fact_id": 12,
                                "memory_ref": "M12",
                                "key": "hobby",
                                "content": "茶",
                            },
                        ]
                    },
                },
                attempted=True,
            )
        )
        assert text.startswith("记忆变更尚未执行：当前条件不能唯一定位目标。")
        assert "- M11｜hobby｜pref｜咖啡｜active" in text
        assert "- M12｜hobby｜茶" in text
        assert text.endswith("请明确选择其中一条后再试。")

    def test_not_found(self) -> None:
        text = finalize_mutation_text(
            mutation_view_from_tool_result(
                {"ok": False, "error": "memory_candidate_not_found", "data": {}},
                attempted=True,
            )
        )
        assert text == "记忆变更未执行：在当前合法作用域内没有找到可唯一定位的目标。"

    def test_rejected_uses_public_message_and_reason(self) -> None:
        text = finalize_mutation_text(
            mutation_view_from_tool_result(
                {
                    "ok": False,
                    "error": "permission_denied",
                    "public_message": "当前轮不能变更记忆",
                    "data": {"reason_code": "permission_denied"},
                },
                attempted=True,
            )
        )
        assert text == "当前轮不能变更记忆（reason_code=permission_denied）"

    def test_operation_sentences(self) -> None:
        cases = {
            "noop": "记忆状态没有发生变化，本轮没有完成新的覆盖、撤回或恢复。",
            "contest": "该记忆存在冲突，已标记为有争议；没有按原请求直接覆盖或删除。",
            "merge_evidence": "这条信息已经存在，本轮只合并了证据，没有创建重复记忆。",
            "create": "已将这条信息写入长期记忆。",
            "correct": "记忆已按你的要求纠正，旧版本不再作为当前有效答案。",
            "invalidate": "这条记忆已撤回并失效；审计记录仍保留，但不会再作为有效记忆使用。",
            "restore": "这条记忆已恢复为有效状态。",
            "merge": "相关记忆已经合并，重复版本不再分别作为有效记忆使用。",
            "reassign": "记忆归属已经按真实回执更新。",
            "update_metadata": "记忆元数据已经按真实回执更新。",
        }
        for applied, expected in cases.items():
            outcome = (
                "no_change"
                if applied == "noop"
                else "committed_as_contested"
                if applied == "contest"
                else "deduplicated"
                if applied == "merge_evidence"
                else "committed"
            )
            text = finalize_mutation_text(
                mutation_view_from_tool_result(
                    {
                        "ok": True,
                        "data": {"applied_operation": applied, "outcome": outcome},
                    },
                    attempted=True,
                )
            )
            assert text == expected, applied

    def test_service_result_create_matches_tool_envelope(self) -> None:
        result = MemoryMutationResult(
            ok=True,
            mutation_id="m-1",
            requested_operation=MemoryMutationOperation.CREATE,
            applied_operation=MemoryMutationAppliedOperation.CREATE,
            outcome=MemoryMutationOutcome.COMMITTED,
        )
        assert finalize_mutation_text(mutation_view_from_result(result)) == (
            "已将这条信息写入长期记忆。"
        )

    def test_service_result_ambiguous_uses_candidates(self) -> None:
        result = MemoryMutationResult(
            ok=False,
            mutation_id=None,
            requested_operation=MemoryMutationOperation.CORRECT,
            applied_operation=MemoryMutationAppliedOperation.NOOP,
            outcome=MemoryMutationOutcome.REJECTED,
            reason_code="memory_candidate_ambiguous",
            candidates=(
                MemoryMutationCandidate(
                    fact_id=7,
                    memory_ref="M7",
                    memory_key="drink",
                    category="pref",
                    kind=MemoryKind.PREFERENCE,
                    content="拿铁",
                    status=MemoryStatus.ACTIVE,
                ),
            ),
        )
        text = finalize_mutation_text(mutation_view_from_result(result))
        assert "M7｜drink｜pref｜拿铁｜active" in text
