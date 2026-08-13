"""Commands for a person's memories, preferences, identity, and relationship."""

from __future__ import annotations

import json
import re

from qq_ai_bot.admin.models import AdminActor
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.memory.rebuild.models import MemoryRebuildSelection
from qq_ai_bot.memory.rebuild.service import MemoryRebuildService
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.repositories import PeopleRepository
from qq_ai_bot.services.admin.memory_admin import MemoryAdminService
from qq_ai_bot.services.admin.preference_admin import PreferenceAdminService
from qq_ai_bot.services.admin.relationship_admin import RelationshipAdminService

_NUMERIC_PLATFORM_ID = re.compile(r"[1-9][0-9]{4,19}")


class ProfileCommandHandler:
    """Handle deterministic commands scoped to one QQ person."""

    def __init__(
        self,
        *,
        people: PeopleRepository,
        memories: MemoryFactService,
        memory_admin: MemoryAdminService,
        preference_admin: PreferenceAdminService,
        relationship_admin: RelationshipAdminService,
        memory_rebuild: MemoryRebuildService | None = None,
        bot_display_name: str = "Yuki",
    ) -> None:
        self._people = people
        self._memories = memories
        self._memory_admin = memory_admin
        self._preference_admin = preference_admin
        self._relationship_admin = relationship_admin
        self._memory_rebuild = memory_rebuild
        self._bot_display_name = bot_display_name

    async def memory(self, *, actor: AdminActor, argument: str) -> str:
        if argument.strip().casefold().startswith("rebuild"):
            return await self._memory_rebuild_command(actor, argument.strip()[7:].strip())
        special = await self._memory_diagnostics(actor, argument)
        if special is not None:
            return special
        parsed = self._parse_scoped_operation(
            argument,
            actor.user_id,
            actor.is_superuser,
        )
        if isinstance(parsed, str):
            return parsed
        operation, target, rest = parsed
        if operation == "list":
            rows = await self._memory_admin.list_memories(actor, target)
            if not rows:
                return f"QQ {target} 暂无人物记忆。"
            return "\n".join(f"{row.id}. [{row.source_type}] {row.content}" for row in rows)
        if operation == "add":
            content = " ".join(rest).strip()
            if not content:
                return "格式：/ai memory add <内容>"
            try:
                row = await self._memory_admin.add_memory(actor, target, content)
            except ValueError as exc:
                return str(exc)
            return f"已添加人物记忆 {row.id}。"
        if operation == "update":
            if len(rest) < 2 or not rest[0].isdigit():
                return "格式：/ai memory update <记忆ID> <内容>"
            updated = await self._memory_admin.update_memory(
                actor,
                target,
                int(rest[0]),
                " ".join(rest[1:]),
            )
            return "记忆已更新。" if updated else "没有找到可修改的记忆。"
        if operation == "delete":
            if len(rest) != 1 or not rest[0].isdigit():
                return "格式：/ai memory delete <记忆ID>"
            deleted = await self._memory_admin.delete_memory(
                actor,
                target,
                int(rest[0]),
            )
            return "记忆已删除。" if deleted else "没有找到该记忆。"
        if operation == "evidence":
            if len(rest) != 1 or not rest[0].isdigit():
                return "格式：/ai memory evidence <记忆ID>"
            evidence_rows = await self._memory_admin.list_evidence(actor, target, int(rest[0]))
            if not evidence_rows:
                return "没有找到该记忆的证据。"
            return "\n".join(
                f"事件 {row.event_id} [{row.relation}] {row.excerpt}" for row in evidence_rows
            )
        return (
            "可用操作：list、add、update、delete、evidence、search、index、embedding、"
            "self-reflection。"
        )

    async def _memory_rebuild_command(self, actor: AdminActor, argument: str) -> str:
        if self._memory_rebuild is None:
            return "历史记忆重建服务未装配。"
        if not actor.is_superuser:
            raise PermissionError("仅当前真实超级管理员可使用历史记忆重建。")
        operation, _, rest = argument.partition(" ")
        operation = operation.casefold()
        rest = rest.strip()
        try:
            if operation == "list":
                rows = await self._memory_rebuild.list(actor_user_id=actor.user_id)
                return (
                    "\n".join(
                        f"{row.public_id} [{row.status.value}] "
                        f"{row.plan_statistics.eligible_events} events"
                        for row in rows
                    )
                    or "暂无历史记忆重建任务。"
                )
            if operation == "plan":
                selection = MemoryRebuildSelection.model_validate_json(rest)
                run = await self._memory_rebuild.plan(selection, actor_user_id=actor.user_id)
                return (
                    f"已规划 {run.public_id}：匹配 {run.plan_statistics.matched_events}，"
                    f"可提取 {run.plan_statistics.eligible_events}；plan 未调用模型。"
                )
            if operation in {
                "start",
                "status",
                "pause",
                "resume",
                "cancel",
                "commit",
                "retry",
                "purge",
            }:
                if not rest:
                    return f"格式：/ai memory rebuild {operation} <run_id>"
                if operation == "status":
                    data = await self._memory_rebuild.status(rest, actor_user_id=actor.user_id)
                    return json.dumps(data, ensure_ascii=False, indent=2)
                method = getattr(self._memory_rebuild, operation)
                result = await method(rest, actor_user_id=actor.user_id)
                if isinstance(result, bool):
                    return "重建暂存数据已清理。" if result else "没有找到该重建任务。"
                return f"{result.public_id} -> {result.status.value}"
            if operation == "review":
                run_id, _, raw_page = rest.partition(" ")
                review_entries = await self._memory_rebuild.review(
                    run_id,
                    actor_user_id=actor.user_id,
                    page=int(raw_page or "1"),
                )
                return json.dumps(
                    [row.model_dump(mode="json") for row in review_entries],
                    ensure_ascii=False,
                    indent=2,
                )
            if operation in {"approve", "reject"}:
                run_id, _, selector = rest.partition(" ")
                if not run_id or not selector:
                    return f"格式：/ai memory rebuild {operation} <run_id> <all|ids|filter-json>"
                count = await self._memory_rebuild.set_review(
                    run_id,
                    selector,
                    approved=operation == "approve",
                    actor_user_id=actor.user_id,
                )
                return f"已{('批准' if operation == 'approve' else '拒绝')} {count} 条 proposal。"
        except (ValueError, RuntimeError, PermissionError, json.JSONDecodeError) as exc:
            return f"操作未完成：{exc}"
        return (
            "可用操作：list、plan、start、status、pause、resume、cancel、review、"
            "approve、reject、commit、retry、purge。"
        )

    async def _memory_diagnostics(self, actor: AdminActor, argument: str) -> str | None:
        parts = argument.split()
        diagnostic_operations = {
            "search",
            "index",
            "embedding",
            "show",
            "explain",
            "history",
            "conflicts",
            "correct",
            "invalidate",
            "restore",
            "merge",
            "resolve",
            "maintenance",
            "self-reflection",
            "dream",
            "doctor",
        }
        if not parts or parts[0].casefold() not in diagnostic_operations:
            return None
        operation = parts.pop(0).casefold()
        try:
            if operation == "dream":
                return await self._memory_dream_command(actor, parts)
            if operation == "self-reflection":
                if parts != ["run"]:
                    return "格式：/ai memory self-reflection run"
                reflection_result = await self._memory_admin.self_reflection_run(actor)
                reflection_health = reflection_result.health
                usage = f"{reflection_health.calls_today}/{reflection_result.max_daily_calls}"
                if reflection_result.attempted_batches:
                    return (
                        "Self Reflection 本轮结束："
                        f"尝试 {reflection_result.attempted_batches} 个批次，"
                        f"成功 {reflection_result.completed_batches} 个，"
                        f"失败 {reflection_result.failed_batches} 个；"
                        f"生成 {reflection_result.proposal_count} 条 proposal，"
                        f"实际写入 {reflection_result.committed_count} 条；"
                        f"今日反思批次 {usage}；"
                        f"仍待处理 {reflection_health.pending_conversations} 个会话。"
                    )
                if reflection_health.calls_today >= reflection_result.max_daily_calls:
                    reason = "今日模型调用已达上限"
                elif reflection_health.pending_conversations == 0:
                    reason = "当前没有待处理会话"
                else:
                    reason = f"待处理会话尚无 {self._bot_display_name} 已发送回复或可信工具结果"
                return f"Self Reflection 本轮未处理会话：{reason}；今日反思批次 {usage}。"
            if operation in {"show", "explain", "history"}:
                if len(parts) != 1 or not parts[0].isdigit():
                    return f"格式：/ai memory {operation} <fact_id>"
                fact_id = int(parts[0])
                if operation == "show":
                    fact = await self._memory_admin.show_fact(actor, fact_id)
                    if fact is None:
                        return "没有找到该事实。"
                    return (
                        f"#{fact.id} [{fact.status.value}/{fact.conflict_state.value}] "
                        f"{fact.content}\n作用域：{fact.scope_type.value}；"
                        f"来源：{fact.authority.value}；证据：{fact.evidence_count} 条"
                    )
                if operation == "explain":
                    explanation = await self._memory_admin.explain_fact(actor, fact_id)
                    return (
                        json.dumps(explanation, ensure_ascii=False, indent=2)
                        if explanation is not None
                        else "没有找到该事实。"
                    )
                history = await self._memory_admin.fact_history(actor, fact_id)
                if not history:
                    return "该事实暂无状态历史。"
                return "\n".join(
                    f"{row.created_at:%Y-%m-%d %H:%M} {row.action.value} [{row.reason_code}]"
                    for row in history
                )
            if operation == "conflicts":
                target: str | None = None
                if parts:
                    if (
                        len(parts) != 2
                        or parts[0].casefold() != "user"
                        or _NUMERIC_PLATFORM_ID.fullmatch(parts[1]) is None
                    ):
                        return "格式：/ai memory conflicts [user <QQ号>]"
                    target = parts[1]
                rows = await self._memory_admin.list_conflicts(
                    actor,
                    target_user_id=target,
                )
                return (
                    "\n".join(
                        f"#{row.id} [{row.status.value}/{row.conflict_state.value}] {row.content}"
                        for row in rows
                    )
                    if rows
                    else "当前没有未解决的记忆冲突。"
                )
            if operation == "correct":
                if len(parts) < 2 or not parts[0].isdigit():
                    return "格式：/ai memory correct <fact_id> <new_content>"
                row = await self._memory_admin.correct_fact(
                    actor,
                    int(parts[0]),
                    " ".join(parts[1:]),
                )
                return (
                    f"已创建修正事实 #{row.id}。" if row is not None else "没有找到可修正的事实。"
                )
            if operation == "invalidate":
                if not 1 <= len(parts) <= 2 or not parts[0].isdigit():
                    return "格式：/ai memory invalidate <fact_id> [reason]"
                changed = await self._memory_admin.invalidate_fact(
                    actor,
                    int(parts[0]),
                    parts[1] if len(parts) == 2 else None,
                )
                return "事实已失效，历史和证据仍保留。" if changed else "事实不存在或已失效。"
            if operation == "restore":
                if len(parts) != 1 or not parts[0].isdigit():
                    return "格式：/ai memory restore <fact_id>"
                row = await self._memory_admin.restore_fact(actor, int(parts[0]))
                return "事实已恢复。" if row is not None else "该事实不能恢复或 active 槽位已占用。"
            if operation == "merge":
                if len(parts) != 2 or not all(item.isdigit() for item in parts):
                    return "格式：/ai memory merge <source_fact_id> <target_fact_id>"
                row = await self._memory_admin.merge_facts(actor, int(parts[0]), int(parts[1]))
                return f"已合并到事实 #{row.id}。" if row is not None else "没有找到待合并事实。"
            if operation == "resolve":
                if len(parts) < 2 or not all(item.isdigit() for item in parts):
                    return "格式：/ai memory resolve <preferred_fact_id> <contested_fact_id...>"
                count = await self._memory_admin.resolve_conflicts(
                    actor,
                    int(parts[0]),
                    tuple(int(item) for item in parts[1:]),
                )
                return f"已解决冲突并失效 {count} 条争议事实。"
            if operation == "maintenance":
                if len(parts) != 1 or parts[0].casefold() not in {"status", "run"}:
                    return "格式：/ai memory maintenance status|run"
                if parts[0].casefold() == "run":
                    count = await self._memory_admin.maintenance_run(actor)
                    return f"记忆生命周期维护完成，更新 {count} 条事实。"
                running, health = await self._memory_admin.maintenance_status(actor)
                return (
                    f"维护 Worker：{'运行中' if running else '未运行'}；"
                    f"过期待处理 {health.expired_active_count}；"
                    f"陈旧待处理 {health.stale_backlog_count}。"
                )
            if operation == "doctor":
                health = await self._memory_admin.consistency_health(actor)
                return (
                    f"Memory V2 一致性：{'正常' if health.healthy else '异常'}；"
                    f"active 槽冲突 {health.active_slot_conflicts}；"
                    f"争议 facts {health.contested_fact_count}；"
                    f"孤立关系 {health.orphan_relation_count}；"
                    f"跨目标关系 {health.cross_target_relation_count}；"
                    f"过期 active {health.expired_active_count}。"
                )
            if not actor.is_superuser:
                return "权限不足：该记忆检索诊断仅限超级管理员。"
            if operation == "search":
                if len(parts) < 3 or parts[0].casefold() not in {"person", "group"}:
                    return "格式：/ai memory search person <QQ号> <query> 或 group <群号> <query>"
                scope = parts.pop(0).casefold()
                target = parts.pop(0)
                if _NUMERIC_PLATFORM_ID.fullmatch(target) is None:
                    return "目标 QQ 号或群号格式错误。"
                query = " ".join(parts).strip()
                if not query:
                    return "检索 query 不能为空。"
                result = (
                    await self._memory_admin.search_person(actor, target, query)
                    if scope == "person"
                    else await self._memory_admin.search_group(actor, target, query)
                )
                if not result.hits:
                    return "没有检索到相关记忆。"
                return "\n".join(
                    f"{hit.fact.id}. [{hit.selection_reason}] {hit.fact.content}"
                    for hit in result.hits
                )
            if operation == "embedding":
                if len(parts) != 1 or parts[0].casefold() not in {
                    "status",
                    "doctor",
                    "retry",
                    "rebuild",
                    "purge-old",
                }:
                    return "格式：/ai memory embedding status|doctor|retry|rebuild|purge-old"
                action = parts[0].casefold()
                if action == "status":
                    embedding_health = await self._memory_admin.embedding_status(actor)
                    profile = (
                        embedding_health.current_profile[:12]
                        if embedding_health.current_profile
                        else "无"
                    )
                    return (
                        f"Embedding 状态：启用 {embedding_health.enabled}，"
                        f"配置 {embedding_health.provider_configured}，"
                        f"Profile {profile}，覆盖 {embedding_health.ready_embedding_count}/"
                        f"{embedding_health.active_fact_count} "
                        f"({embedding_health.coverage_ratio:.1%})，"
                        f"等待 {embedding_health.pending_job_count}，"
                        f"处理中 {embedding_health.processing_job_count}，"
                        f"失败 {embedding_health.failed_job_count}，"
                        f"旧 Profile {embedding_health.old_profile_count}。"
                    )
                if action == "doctor":
                    dimensions = await self._memory_admin.embedding_doctor(actor)
                    return f"Embedding Provider 检查通过：{dimensions} 维 dense vector。"
                if action == "retry":
                    count = await self._memory_admin.embedding_retry(actor)
                    return f"已重新激活 {count} 个失败任务。"
                if action == "rebuild":
                    count = await self._memory_admin.embedding_rebuild(actor)
                    return f"已为当前 Profile 重新排队 {count} 条 active facts。"
                count = await self._memory_admin.embedding_purge_old(actor)
                return f"已清理 {count} 个非当前 Embedding Profile。"
            if len(parts) != 1 or parts[0].casefold() not in {"status", "rebuild"}:
                return "格式：/ai memory index status|rebuild"
            index_health = (
                await self._memory_admin.index_status(actor)
                if parts[0].casefold() == "status"
                else await self._memory_admin.rebuild_index(actor)
            )
            prefix = "记忆索引已重建" if parts[0].casefold() == "rebuild" else "记忆索引状态"
            return (
                f"{prefix}：事实 {index_health.fact_count}，索引 {index_health.indexed_row_count}，"
                f"缺失 {index_health.missing_row_count}，"
                f"孤儿 {index_health.orphan_row_count}。"
            )
        except (PermissionError, RuntimeError, ValueError) as exc:
            return str(exc)

    async def _memory_dream_command(self, actor: AdminActor, parts: list[str]) -> str:
        if not actor.is_superuser:
            raise PermissionError("仅当前真实超级管理员可管理 Memory Dream。")
        if not parts:
            return (
                "可用操作：plan、start、list、status、show、cancel、resume、retry、"
                "preview、rollback run、rollback operation。"
            )
        operation = parts.pop(0).casefold()
        if operation == "plan" and not parts:
            run = await self._memory_admin.dream_plan(actor)
            stats = run.statistics
            return (
                f"Dream 计划 {run.public_id}：正式记忆 {stats.eligible_facts}，"
                f"候选簇 {stats.candidate_clusters}，预计最多调用 "
                f"{stats.estimated_model_calls} 次；使用 start 启动。"
            )
        if operation == "list" and not parts:
            rows = await self._memory_admin.dream_list(actor)
            return (
                "\n".join(
                    f"{row.public_id} [{row.mode.value}/{row.status.value}] "
                    f"clusters={row.statistics.candidate_clusters} calls={row.model_calls}"
                    for row in rows
                )
                or "暂无 Dream 任务。"
            )
        if operation == "show" and len(parts) in {1, 2}:
            page = int(parts[1]) if len(parts) == 2 else 1
            page_result = await self._memory_admin.dream_show(actor, parts[0], page=page)
            lines = [
                f"cluster={row.id} [{row.status.value}] kind={row.kind} "
                f"facts={','.join(map(str, row.fact_ids))} calls={row.model_calls} "
                f"operations={row.operation_count} error={row.error_category or '-'}"
                for row in page_result.clusters
            ]
            lines.extend(
                f"operation={row.public_id} [{row.operation.value}/{row.status.value}] "
                f"cluster={row.cluster_id} sources={','.join(map(str, row.source_fact_ids))} "
                f"outputs={','.join(map(str, row.output_fact_ids)) or row.output_fact_id or '-'}"
                for row in page_result.operations
            )
            return "\n".join(lines) or "该页没有 Dream 簇。"
        if operation == "preview" and len(parts) == 2:
            public_id, raw_cluster_id = parts
            preview = await self._memory_admin.dream_preview(
                actor,
                public_id,
                cluster_id=int(raw_cluster_id),
            )
            lines = [
                f"Dream 只读预览 cluster={preview.cluster_id} "
                f"facts={','.join(map(str, preview.fact_ids))}",
                f"preview={preview.preview_public_id or '-'}; execution will reuse this proposal",
                f"正文 {preview.source_characters} → {preview.output_characters} 字符 "
                f"({preview.compression_ratio:.1%})；尚未写入数据库。",
            ]
            for index, action in enumerate(preview.actions, start=1):
                lines.append(
                    f"[{index}] {action.operation.value} sources={','.join(action.source_refs)}"
                )
                if action.content:
                    lines.append(action.content)
                for output_index, output in enumerate(action.outputs, start=1):
                    lines.append(f"  focus={output.focus}")
                    lines.append(
                        f"  输出 {output_index} sources={','.join(output.source_refs)} "
                        f"importance={output.importance}"
                    )
                    lines.append(output.content)
            return "\n".join(lines)
        if operation in {"start", "status", "cancel", "resume", "retry"}:
            if len(parts) != 1:
                return f"格式：/ai memory dream {operation} <run_id>"
            public_id = parts[0]
            if operation == "status":
                row = await self._memory_admin.dream_status(actor, public_id)
                return (
                    json.dumps(row.model_dump(mode="json"), ensure_ascii=False, indent=2)
                    if row is not None
                    else "没有找到该 Dream 任务。"
                )
            if operation == "cancel":
                return (
                    "Dream 任务已取消。"
                    if await self._memory_admin.dream_cancel(actor, public_id)
                    else "Dream 任务不存在或当前不能取消。"
                )
            method = getattr(self._memory_admin, f"dream_{operation}")
            row = await method(actor, public_id)
            return f"Dream 任务 {row.public_id} 已进入 {row.status.value}。"
        if operation == "rollback" and len(parts) == 2:
            kind, public_id = parts
            if kind == "operation":
                changed = await self._memory_admin.dream_rollback_operation(actor, public_id)
                return "Dream operation 已回滚。" if changed else "没有可回滚的 operation。"
            if kind == "run":
                count = await self._memory_admin.dream_rollback_run(actor, public_id)
                return f"Dream run 已回滚 {count} 个 operation。"
        return (
            "格式：/ai memory dream "
            "<plan|start|list|status|show|preview|cancel|resume|retry|rollback>"
        )

    async def preference(self, *, actor: AdminActor, argument: str) -> str:
        parsed = self._parse_scoped_operation(
            argument,
            actor.user_id,
            actor.is_superuser,
        )
        if isinstance(parsed, str):
            return parsed
        operation, target, rest = parsed
        if operation == "list":
            rows = await self._preference_admin.list_preferences(actor, target)
            if not rows:
                return f"QQ {target} 暂无交互偏好。"
            return "\n".join(f"{row.key} = {row.value}" for row in rows)
        if operation == "set":
            if len(rest) < 2:
                return "格式：/ai preference set <键> <值>"
            await self._preference_admin.set_preference(
                actor,
                target,
                rest[0],
                " ".join(rest[1:]),
            )
            return f"偏好 {rest[0]} 已设置。"
        if operation == "delete":
            if len(rest) != 1:
                return "格式：/ai preference delete <键>"
            deleted = await self._preference_admin.delete_preference(
                actor,
                target,
                rest[0],
            )
            return "偏好已删除。" if deleted else "没有找到该偏好。"
        return "可用操作：list、set、delete。"

    async def affection(
        self,
        *,
        actor: AdminActor,
        argument: str,
    ) -> str:
        parts = argument.split()
        if not parts:
            return "格式：/ai affection show|history"
        operation = parts.pop(0).casefold()
        if operation in {"show", "history"}:
            target = actor.user_id
            if parts:
                if len(parts) != 2 or parts[0].casefold() != "user":
                    return f"格式：/ai affection {operation} [user <QQ号>]"
                if operation == "history" and not actor.is_superuser:
                    return "只有超级管理员可以查看其他 QQ 人物的关系变化历史。"
                if _NUMERIC_PLATFORM_ID.fullmatch(parts[1]) is None:
                    return "目标 QQ 号格式错误。"
                target = parts[1]
            if operation == "show":
                snapshot = await self._relationship_admin.get_relationship(actor, target)
                return (
                    f"好感度：{snapshot.affection_score}\n"
                    f"信任度：{snapshot.trust_score}\n"
                    f"有效信任度：{snapshot.effective_trust}\n"
                    f"当前关系阶段：{snapshot.stage.name}"
                )
            history = await self._relationship_admin.get_history(actor, target, limit=10)
            if not history:
                return "暂无关系变化记录。"
            return "\n".join(
                (
                    f"{row.created_at:%Y-%m-%d %H:%M} "
                    f"好感{row.affection_delta:+d} 信任{row.trust_delta:+d} "
                    f"[{row.change_type}/{row.reason_code}]"
                )
                for row in history
            )

        if operation not in {"set", "adjust", "trust"}:
            return "可用操作：show、history；超级管理员另可使用 set、adjust、trust。"
        if not actor.is_superuser:
            return "权限不足：只有超级管理员可以修改关系分数。"
        if (
            len(parts) != 3
            or parts[0].casefold() != "user"
            or _NUMERIC_PLATFORM_ID.fullmatch(parts[1]) is None
        ):
            return f"格式：/ai affection {operation} user <QQ号> <数值>"
        try:
            value = int(parts[2])
        except ValueError:
            return "分数必须是整数。"
        target = parts[1]
        try:
            if operation == "set":
                _, snapshot = await self._relationship_admin.set_affection(actor, target, value)
            elif operation == "adjust":
                _, snapshot = await self._relationship_admin.adjust_affection(
                    actor,
                    target,
                    value,
                )
            else:
                _, snapshot = await self._relationship_admin.set_trust(actor, target, value)
        except ValueError:
            return "好感度/信任度必须在 0～100；好感度单次调整必须在 -20～20。"
        return (
            f"已更新 QQ {target}：好感度 {snapshot.affection_score}，"
            f"信任度 {snapshot.trust_score}，阶段 {snapshot.stage.name}。"
        )

    @staticmethod
    def _parse_scoped_operation(
        argument: str, actor: str, is_superuser: bool
    ) -> tuple[str, str, list[str]] | str:
        parts = argument.split()
        if not parts:
            return "缺少操作名。"
        operation = parts.pop(0).casefold()
        target = actor
        if len(parts) >= 2 and parts[0].casefold() == "user":
            if not is_superuser:
                return "只有超级管理员可以管理其他 QQ 人物。"
            candidate = parts[1]
            if _NUMERIC_PLATFORM_ID.fullmatch(candidate) is None:
                return "目标 QQ 号格式错误。"
            target = candidate
            del parts[:2]
        return operation, target, parts

    async def whoami(
        self,
        message: InboundMessage,
        profile: UserProfileSnapshot,
        argument: str,
    ) -> str:
        if argument:
            return "该命令不接受参数，只能查看发送者本人。"
        aliases = await self._people.aliases(profile.user_id)
        memory_count = await self._memories.count_person(profile.user_id)
        membership_count = await self._people.membership_count(profile.user_id)
        lines = [
            f"QQ：{profile.user_id}",
            f"当前昵称：{profile.nickname or '未获取'}",
        ]
        if message.scope_type is ScopeType.GROUP:
            lines.extend(
                [
                    f"本群群名片：{profile.group_card or '未设置'}",
                    f"当前群：{profile.group_id}",
                ]
            )
        else:
            lines.append("当前场景：私聊")
        lines.extend(
            [
                f"已知别名：{'、'.join(aliases) if aliases else '无'}",
                f"个人记忆数：{memory_count}",
                f"群成员关系数：{membership_count}",
            ]
        )
        return "\n".join(lines)
