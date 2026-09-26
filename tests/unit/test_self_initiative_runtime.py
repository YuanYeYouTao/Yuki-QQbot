"""SELF recovery preserves identity, original work and append-only request history."""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from qq_ai_bot.conversation.autonomy_binding import InitiativeSource, InitiativeSourceKind
from qq_ai_bot.conversation.autonomy_repository import AutonomyRepository
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.hydrate import ensure_canonical_conversation
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.messages import ChatMessage, ProviderContinuation, ToolCall, ToolFunction
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.identity.canonical_repository import ensure_presence, ensure_space
from qq_ai_bot.identity.db_models import CanonicalSpaceModel, PresenceModel
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.authority import TurnAuthority
from qq_ai_bot.runtime.errors import InvalidTurnContextError
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.subagent_repository import SubagentRepository
from qq_ai_bot.runtime.work_activation import activate_work, current_work_control
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects, inputs
from qq_ai_bot.runtime.work_session import WorkSession
from qq_ai_bot.sandbox.source_recovery import recover_execution_source, recover_self_source
from qq_ai_bot.services.turn_transcript import TurnTranscript


async def self_source(database):
    async with database.immediate_session() as db:
        space = await ensure_space(db, "2001")
        presence = await ensure_presence(db, "8000")
        conversation = await ensure_canonical_conversation(
            db, kind="space", primary_scope_key="bot:8000:group:2001", space_id=space
        )
        await db.execute(
            update(CanonicalSpaceModel).where(CanonicalSpaceModel.id == space).values(enabled=True)
        )
    repository = AutonomyRepository(database)
    binding = await repository.ensure_binding(conversation.conversation_id, 1)
    binding = await repository.transition(binding, master_enabled=True, external_enabled=True)
    result = await repository.accept_host_proposal(
        proposal_id="self-memory",
        binding=binding,
        owner=binding.effective_owner,
        space_id=space,
        presence_id=presence,
        sources=(InitiativeSource(InitiativeSourceKind.MEMORY, "1", "v1"),),
        support_refs=("observation:1",),
    )
    assert result.run is not None
    source = dict(
        principal_kind="self",
        initiative_run_id=result.run.run_id,
        origin="self_initiative",
        conversation_id=conversation.conversation_id,
        generation=1,
        space_id=space,
        presence_id=presence,
        actor_user_id="",
        bot_user_id="8000",
        group_id="2001",
        instruction="核对记忆中的计划",
        delivery_contract="return_to_caller",
    )
    return source, repository, binding


def actor():
    return ToolActor(
        user_id="",
        bot_user_id="8000",
        group_id="2001",
        origin=TurnOrigin.SELF_INITIATIVE,
        instruction="observe",
        execution_id="work",
        conversation_id="conversation",
        presence_id="presence",
        principal_kind="self",
        initiative_run_id="initiative",
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"user_id": "1001"},
        {"person_id": "person"},
        {"event_id": 1},
        {"platform_message_id": "qq-message"},
        {"origin": TurnOrigin.USER_MESSAGE},
        {"initiative_run_id": None},
        {"principal_kind": "person"},
    ],
)
def test_self_actor_cannot_borrow_person_or_message(fields):
    value = actor()
    assert value.source_key == "initiative:initiative"
    with pytest.raises(ValueError):
        replace(value, **fields)


def test_snapshot_and_authority_require_exactly_one_real_principal():
    snap = ConversationTurnSnapshot(1, "group", 1, None, 1, initiative_run_id="run")
    assert snap.trigger_event_id is None
    with pytest.raises(ValueError):
        replace(snap, trigger_event_id=9)
    with pytest.raises(ValueError):
        replace(snap, initiative_run_id=None)
    authority = TurnAuthority(
        "",
        "8000",
        TurnOrigin.SELF_INITIATIVE,
        frozenset(),
        None,
        1,
        principal_kind="self",
        initiative_run_id="run",
    )
    with pytest.raises(InvalidTurnContextError):
        replace(authority, actor_user_id="1001")


async def test_memory_origin_recovers_without_any_chat_event_and_survives_master_off(database):
    source, admissions, binding = await self_source(database)
    before = await recover_self_source(
        database, source["conversation_id"], source, request_id="execution"
    )
    assert before.actor_person_id is None and before.event_id is None
    assert before.actor_user_id == "" and before.trigger().run_id == source["initiative_run_id"]
    async with database.sessions() as db:
        assert not list(await db.scalars(select(ChatEventModel.id)))
    await admissions.transition(binding, master_enabled=False, external_enabled=True)
    assert (
        await recover_execution_source(
            database, source["conversation_id"], source, request_id="execution"
        )
        == before
    )
    for changed in ({"actor_user_id": "1001"}, {"group_id": "2002"}, {"trigger_event_id": 1}):
        with pytest.raises(ValueError):
            await recover_self_source(
                database, source["conversation_id"], {**source, **changed}, request_id="execution"
            )
    async with database.immediate_session() as db:
        await db.execute(
            update(CanonicalConversationModel)
            .where(CanonicalConversationModel.id == source["conversation_id"])
            .values(generation=2)
        )
    with pytest.raises(ValueError, match="self_task_source_changed"):
        await recover_self_source(
            database, source["conversation_id"], source, request_id="execution"
        )


async def test_self_recovery_revalidates_presence_and_terminal_state(database):
    source, admissions, _ = await self_source(database)
    async with database.immediate_session() as db:
        await db.execute(
            update(PresenceModel)
            .where(PresenceModel.id == source["presence_id"])
            .values(enabled=False)
        )
    with pytest.raises(ValueError, match="task_presence_disabled"):
        await recover_self_source(database, source["conversation_id"], source, request_id="w")
    await admissions.record_feedback(source["initiative_run_id"], sequence=1, outcome="no_reply")
    with pytest.raises(ValueError, match="self_task_terminal"):
        await recover_self_source(database, source["conversation_id"], source, request_id="w")


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
async def test_self_work_restart_retains_id_budget_prefix_pending_receipt_and_delivery(
    database,
    protocol,
):
    source, _, _ = await self_source(database)
    repo = WorkRepository(database)
    key = f"initiative:{source['initiative_run_id']}"
    lease = await repo.acquire(source["conversation_id"], 1)
    item = await repo.accept(lease, source_key=key, source=source, goal=source["instruction"])
    repeated = await repo.accept(lease, source_key=key, source=source, goal=source["instruction"])
    assert repeated["id"] == item["id"]
    with pytest.raises(WorkConflict, match="invalid_self_work_admission"):
        await repo.accept(lease, source_key="other-boundary", source=source, goal="other")
    validate = AsyncMock()
    control = WorkControl(repo, lease, key, source, validate)
    control.current = item
    control.current_message = ChatMessage("user", "initial brief")
    first = WorkSession(control, "fixed-contract")
    transcript = await first.restore(
        TurnTranscript(
            (
                ChatMessage("system", "fixed contract"),
                control.current_message,
            )
        )
    )
    transcript.accept(
        ProviderContinuation(
            provider="deepseek", protocol=protocol, payload={"original": "continuation"}
        )
    )
    call = ToolCall("call-1", ToolFunction("terminal_exec", '{"command":"render"}'))
    await repo.prepare_effect(lease, item["id"], first.call_key(call.id), "tool")
    await repo.record_effect(first.call_key(call.id), "accepted", {"result": '{"run_id":"run"}'})
    await first.save("model", (call,))
    control.current = await repo.checkpoint(lease, item["id"], {}, models=24, tools=3)
    await repo.release(lease)

    next_lease = await WorkRepository(database).acquire(source["conversation_id"], 1)
    resumed = WorkControl(repo, next_lease, key, dict(source), validate)
    resumed.current = await repo.get(item["id"])
    resumed.current_message = ChatMessage("user", "MUST NOT replace or append original brief")
    second = WorkSession(resumed, "fixed-contract")
    restored = await second.restore(TurnTranscript((resumed.current_message,)))
    assert restored.request().messages[:2] == transcript.request().messages
    assert restored.request().items[-1].call_id == call.id
    assert restored.request().continuation == transcript.request().continuation
    assert resumed.current["model_requests"] == 24 and resumed.current["tool_calls"] == 3
    invoke = AsyncMock(return_value="unexpected")
    assert await second.execute(call, invoke) == '{"run_id":"run"}'
    invoke.assert_not_awaited()
    await second.save("delivered")
    third = WorkSession(resumed, "fixed-contract")
    await third.restore(TurnTranscript(()))
    assert third.recovered_delivery == "delivered"
    await repo.cancel(source["conversation_id"])
    assert not await repo.valid(next_lease)
    with pytest.raises(WorkConflict):
        await third.save("paired")


async def test_self_owned_sandbox_completion_routes_once_to_original_work(database):
    from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository

    source, _, _ = await self_source(database)
    repo = WorkRepository(database)
    lease = await repo.acquire(source["conversation_id"], 1)
    item = await repo.accept(
        lease, source_key=f"initiative:{source['initiative_run_id']}", source=source, goal="render"
    )
    tasks = SandboxTaskRepository(database)
    control = WorkControl(repo, lease, item["source_key"], source, AsyncMock())
    control.current = item
    token = current_work_control.set(control)
    try:
        await tasks.prepare("command", {"command": "render"}, {**source, "work_id": item["id"]})
    finally:
        current_work_control.reset(token)
    run_id = "d890684c-1436-41b8-a44f-273315a99545"
    await tasks.receive(
        {
            "request_id": "command",
            "run_id": run_id,
            "result": {"run_id": run_id, "status": "succeeded", "pending": False},
        }
    )
    await repo.transition(lease, item["id"], item["revision"], "waiting_external")
    await repo.release(lease)
    await repo.route_child_completion("command")
    await repo.route_child_completion("command")
    async with database.sessions() as db:
        records = (await db.execute(select(inputs).where(inputs.c.work_id == item["id"]))).all()
        assert len(records) == 1
    assert (await repo.get(item["id"]))["state"] == "queued"
    await repo.cancel(source["conversation_id"])
    await repo.route_child_completion("command")
    assert (await repo.get(item["id"]))["state"] == "cancelled"


async def test_self_child_preserves_run_without_adopting_person(database):
    source, _, _ = await self_source(database)
    repo = WorkRepository(database)
    lease = await repo.acquire(source["conversation_id"], 1)
    item = await repo.accept(
        lease,
        source_key=f"initiative:{source['initiative_run_id']}",
        source=source,
        goal="inspect",
        output_kind="answer",
        deliver_artifacts=False,
    )
    children = SubagentRepository(repo)
    child_id = await children.start(
        lease,
        item["id"],
        "spawn-once",
        {
            "goal": "inspect independently",
            "output_kind": "answer",
        },
    )
    child = await repo.get(child_id)
    child_source = json.loads(child["source_json"])
    recovered = await recover_execution_source(
        database, source["conversation_id"], child_source, request_id=child_id
    )
    assert recovered.run_id == source["initiative_run_id"]
    assert recovered.actor_user_id == "" and recovered.event_id is None
    child_lease = await children.acquire(child_id)
    assert child_lease is not None
    await repo.checkpoint(child_lease, child_id, {}, models=24, tools=2)
    await repo.cancel(source["conversation_id"])
    assert not await repo.valid(child_lease)
    with pytest.raises(WorkConflict):
        await children.finish(child_lease, "late result")
    assert (await repo.get(child_id))["model_requests"] == 24
    async with database.sessions() as db:
        assert not list(await db.scalars(select(effects.c.effect_key)))


async def test_activate_self_work_rejects_other_initiative_source(database):
    source, _, _ = await self_source(database)
    repo = WorkRepository(database)
    lease = await repo.acquire(source["conversation_id"], 1)
    key = f"initiative:{source['initiative_run_id']}"
    item = await repo.accept(lease, source_key=key, source=source, goal="inspect")
    await repo.release(lease)
    async with activate_work(
        repo,
        source["conversation_id"],
        1,
        key,
        {**source, "initiative_run_id": "other"},
        AsyncMock(),
        work_id=item["id"],
    ) as control:
        assert control.current is None
    assert (await repo.get(item["id"]))["state"] == "running"


async def test_scheduler_resumes_self_without_reading_a_person_event_or_sending_text(database):
    from contextlib import asynccontextmanager

    from qq_ai_bot.runtime.work_scheduler import WorkScheduler

    source, _, _ = await self_source(database)
    repo = WorkRepository(database)
    lease = await repo.acquire(source["conversation_id"], 1)
    item = await repo.accept(
        lease,
        source_key=f"initiative:{source['initiative_run_id']}",
        source=source,
        goal="inspect",
        output_kind="answer",
        deliver_artifacts=False,
    )
    await repo.release(lease)

    @asynccontextmanager
    async def background(_key):
        yield SimpleNamespace(version=1)

    async def generate(**kwargs):
        assert kwargs["trigger"].run_id == source["initiative_run_id"]
        assert kwargs["turn_snapshot"].trigger_event_id is None
        runtime = kwargs["source_runtime"]
        assert runtime.inbound is None and runtime.actor_context.principal_kind == "self"
        assert runtime.execution_id == item["id"] and runtime.actor_user_id == ""
        await kwargs["before_model_request"]()
        control = current_work_control.get()
        assert control.current["id"] == item["id"]
        result = await control.execute("task_control", {"action": "complete"}, "finish-silently")
        assert json.loads(result)["ok"]
        assert not control.known_effects
        return SimpleNamespace(text="NO_REPLY", outcome=None)

    chat = SimpleNamespace(
        _active_work={},
        _validate_turn_snapshot=AsyncMock(return_value=True),
        generate_self_initiative=AsyncMock(side_effect=generate),
    )
    bot = SimpleNamespace(call_api=AsyncMock())
    app = SimpleNamespace(
        database=database,
        chat=chat,
        conversation_scopes=SimpleNamespace(
            get=AsyncMock(
                return_value=SimpleNamespace(
                    id=1, generation=1, runtime_scope_key="bot:8000:group:2001"
                )
            )
        ),
        presence_router=SimpleNamespace(
            resolve_presence=AsyncMock(
                return_value=SimpleNamespace(
                    connection=SimpleNamespace(bot=bot, snapshot="connection")
                )
            )
        ),
        runtime_config=SimpleNamespace(snapshot=AsyncMock(return_value=SimpleNamespace())),
        turn_coordinator=SimpleNamespace(background_turn=background),
    )
    await WorkScheduler(app)._resume_self(item, source)
    chat.generate_self_initiative.assert_awaited_once()
    app.runtime_config.snapshot.assert_awaited_once_with(group_id="2001")
    bot.call_api.assert_not_awaited()
    assert (await repo.get(item["id"]))["state"] == "completed"


async def test_self_worker_uses_existing_runner_without_synthetic_inbound(database):
    from qq_ai_bot.runtime.subagent_scheduler import SubagentScheduler, WorkerBackend
    from qq_ai_bot.runtime.subagent_tools import WORKER_NAMES

    source, _, _ = await self_source(database)
    repo = WorkRepository(database)
    lease = await repo.acquire(source["conversation_id"], 1)
    item = await repo.accept(
        lease, source_key=f"initiative:{source['initiative_run_id']}", source=source, goal="inspect"
    )
    children = SubagentRepository(repo)
    child_id = await children.start(
        lease,
        item["id"],
        "spawn-once",
        {
            "goal": "inspect independently",
            "output_kind": "answer",
        },
    )
    await repo.release(lease)
    memory = SimpleNamespace(close=AsyncMock())

    async def run(messages, runtime, backend):
        assert isinstance(backend, WorkerBackend)
        assert runtime.execution_id == child_id
        assert runtime.actor_user_id == "" and runtime.origin is TurnOrigin.SELF_INITIATIVE
        inner = backend.delegate._runtime
        assert inner.inbound is None and inner.trigger_event_id is None
        assert inner.require_actor().initiative_run_id == source["initiative_run_id"]
        await runtime.before_model_request()
        assert runtime.work_control.current["model_requests"] == 0
        await runtime.work_control.reserve_request()
        await runtime.work_control.execute("task_control", {"action": "complete"}, "done")
        return SimpleNamespace(text="checked")

    runner = SimpleNamespace(run=AsyncMock(side_effect=run))
    chat = SimpleNamespace(
        _agent_runner=runner,
        _open_self_memory_session=AsyncMock(return_value=memory),
        _open_memory_session=AsyncMock(side_effect=AssertionError("no borrowed human")),
        _prefix_web_capabilities=lambda _: frozenset(),
    )
    app = SimpleNamespace(
        database=database,
        chat=chat,
        runtime_config=SimpleNamespace(snapshot=AsyncMock(return_value=SimpleNamespace())),
        settings=SimpleNamespace(subagent_context_token_limit=64000),
    )
    scheduler = SubagentScheduler(app)
    scheduler.definitions = tuple(SimpleNamespace(name=name) for name in sorted(WORKER_NAMES))
    await scheduler.run(child_id)
    assert scheduler.last_error is None
    runner.run.assert_awaited_once()
    chat._open_memory_session.assert_not_called()
    memory.close.assert_awaited_once()
    assert (await repo.get(child_id))["state"] == "completed"
    assert (await repo.get(child_id))["model_requests"] == 1
    async with database.sessions() as db:
        notifications = list(
            await db.scalars(
                select(inputs.c.id).where(
                    inputs.c.work_id == item["id"], inputs.c.kind == "subagent"
                )
            )
        )
        assert len(notifications) == 1


async def test_worker_recovery_starts_when_new_chat_and_child_admission_are_off(database):
    from qq_ai_bot.runtime.subagent_scheduler import SubagentScheduler

    assert not database.subagents_enabled
    contract = SimpleNamespace(definitions=AsyncMock(return_value=()))
    scheduler = SubagentScheduler(
        SimpleNamespace(
            database=database,
            main_agent_contract=contract,
            settings=SimpleNamespace(runtime_work_enabled=False, global_llm_concurrency=2),
        )
    )
    try:
        await scheduler.start()
        assert scheduler.task is not None
        assert not scheduler.task.done()
        contract.definitions.assert_awaited_once()
        original = scheduler.task
        await scheduler.start()
        assert scheduler.task is original
    finally:
        await scheduler.close()
