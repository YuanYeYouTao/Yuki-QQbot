"""Real durable Host feedback; no API calls or QQ sends are performed."""

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from tests.unit.test_autonomy_repository import _accept, _enable, _scene
from yuki_participation.controller import Controller
from yuki_participation.models import CandidateKind, Proposal, Scope, SourceRef, Support

from qq_ai_bot.conversation.autonomy_binding import AutonomyOwner
from qq_ai_bot.conversation.autonomy_db_models import InitiativeFeedbackModel
from qq_ai_bot.conversation.autonomy_repository import AutonomyRepository
from qq_ai_bot.runtime.subagent_schema import budgets, children
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import journal, work
from qq_ai_bot.services import participation_feedback
from qq_ai_bot.services.participation_feedback import reconcile_run, sync_scope_effects
from qq_ai_bot.social.db_models import SocialOperationModel


async def setup(database, *, owner=AutonomyOwner.SEMANTIC):
    scene = await _scene(database)
    repository = AutonomyRepository(database)
    binding = await _enable(repository, scene)
    if owner is AutonomyOwner.LEGACY:
        binding = await repository.transition(binding, master_enabled=True, external_enabled=False)
    run = (await _accept(repository, scene, binding)).run
    now = time.time()
    scope = Scope(conversation_id=scene.conversation, generation=1)
    controller = Controller(scope, now)
    ref = SourceRef(event_id="event:1", revision=1)
    support = Support(
        kind="observed",
        scope=scope,
        thread="topic",
        target="group",
        observation_id="synthetic",
        covered=(ref,),
        basis=ref,
        issued_at=now,
        valid_until=now + 90,
    )
    controller.state.proposals[run.proposal_id] = Proposal(
        proposal_id=run.proposal_id,
        scope=scope,
        controller_epoch=1,
        kind=CandidateKind.CONVERSATION,
        thread="topic",
        target_hint="group",
        sources=(ref,),
        support=support,
        created_at=now,
        expires_at=now + 90,
    )
    item = SimpleNamespace(
        controller=controller,
        scene=SimpleNamespace(
            conversation_id=scene.conversation,
            generation=1,
            space_id=scene.space,
        ),
    )
    service = SimpleNamespace(
        database=database,
        repository=repository,
        work=WorkRepository(database),
        _sessions={(scene.conversation, 1): item},
        _dispatch=AsyncMock(),
        _save=Mock(),
    )
    lease = await service.work.acquire(scene.conversation, 1)
    task = await service.work.accept(
        lease,
        source_key=f"initiative:{run.run_id}",
        source={
            "origin": "self_initiative",
            "principal_kind": "self",
            "initiative_run_id": run.run_id,
            "conversation_id": run.conversation_id,
            "generation": run.generation,
            "space_id": run.space_id,
            "presence_id": run.presence_id,
        },
        goal="synthetic feedback check",
    )
    await service.work.release(lease)
    return service, item, run, task


async def set_work(database, task, **values):
    async with database.sessions() as session, session.begin():
        await session.execute(update(work).where(work.c.id == task["id"]).values(**values))


async def social(database, run, call, *, turn=None, action="send_message", seconds=0):
    at = datetime.now(UTC) + timedelta(seconds=seconds)
    row = SocialOperationModel(
        id=str(uuid4()),
        source_turn_id=turn or f"{run.conversation_id}:initiative:{run.run_id}",
        tool_call_id=call,
        source_conversation_id=run.conversation_id,
        action=action,
        payload_hash="0" * 64,
        target_kind="space",
        target_id=run.space_id,
        presence_id=run.presence_id,
        status="succeeded",
        created_at=at,
        updated_at=at,
    )
    async with database.sessions() as session, session.begin():
        session.add(row)
    return row


@pytest.mark.asyncio
async def test_all_120_compute_charges_page_and_replay_after_checkpoint_loss(database):
    service, item, run, task = await setup(database)
    original = item.controller.state.model_copy(deep=True)
    await set_work(database, task, state="completed", model_requests=120)
    await reconcile_run(service, run)
    assert len(item.controller.state.effects) == 120
    assert all(record.effect.kind == "compute" for record in item.controller.state.effects.values())
    async with database.sessions() as session:
        rows = list(await session.scalars(select(InitiativeFeedbackModel)))
        assert len(rows) == 2
        assert sorted(len(json.loads(row.payload_json)["effects"]) for row in rows) == [56, 64]
    await reconcile_run(service, run)  # stale caller snapshot cannot write the same feedback again
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(InitiativeFeedbackModel)) == 2
    effects = item.controller.state.effects.copy()
    item.controller = Controller.restore(original, time.time())
    await reconcile_run(service, run)
    assert item.controller.state.effects == effects
    assert item.controller.state.feedback[run.run_id].outcome == "no_reply"
    # Terminal outbox history cannot recreate an already cleaned-up Work row.
    service.work.by_source = AsyncMock(return_value=None)
    item.controller = Controller.restore(original, time.time())
    await reconcile_run(service, run)
    assert item.controller.state.effects == effects
    service._dispatch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["suspended", "waiting_user"])
async def test_paused_work_is_terminal_and_late_send_does_not_restart_it(database, state):
    service, item, run, task = await setup(database)
    await set_work(database, task, state=state)
    await reconcile_run(service, run)
    assert (await service.repository.get_run(run.run_id)).state == "interrupted"
    await social(database, run, "late-send")
    await reconcile_run(service, run)
    assert (await service.repository.get_run(run.run_id)).state == "interrupted"
    assert item.controller.state.feedback[run.run_id].outcome == "interrupted"
    assert len(item.controller.state.effects) == 1
    service._dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_sequence_caption_direct_and_semantic_paths_share_one_logical_effect(database):
    service, item, run, task = await setup(database)
    prefix = hashlib.sha256(b"original-send").hexdigest()[:24]
    first = await social(database, run, f"seq:{prefix}:0", seconds=-2)
    await social(database, run, f"seq:{prefix}:1", seconds=-1)
    await social(
        database, run, "caption", turn=f"social-caption:{first.id}", action="send_file_caption"
    )
    await sync_scope_effects(service, item)
    assert len(item.controller.state.effects) == 1
    await set_work(database, task, state="completed")
    await reconcile_run(service, run)
    await sync_scope_effects(service, item)
    assert len(item.controller.state.effects) == 1
    assert item.controller.state.feedback[run.run_id].outcome == "completed"
    await social(database, run, "another-message", turn=f"{run.conversation_id}:event:999")
    await sync_scope_effects(service, item)
    assert len(item.controller.state.effects) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [AutonomyOwner.SEMANTIC, AutonomyOwner.LEGACY])
async def test_only_semantic_run_binds_confirmed_internal_outbound_anchor(
    database, monkeypatch, owner
):
    service, item, run, _ = await setup(database, owner=owner)
    row = await social(database, run, "confirmed-send")
    row.event_id = 123  # Synthetic internal event ID; no platform message ID is used.
    monkeypatch.setattr(participation_feedback, "_scope_social_rows", AsyncMock(return_value=[row]))
    await sync_scope_effects(service, item)
    if owner is AutonomyOwner.SEMANTIC:
        assert item.controller.state.outbound_anchors["event:123"].run_ref == run.run_id
    else:
        assert not item.controller.state.outbound_anchors


@pytest.mark.asyncio
async def test_legacy_self_report_uses_real_charged_run_without_semantic_proposal(database):
    service, item, run, task = await setup(database, owner=AutonomyOwner.LEGACY)
    item.controller.state.proposals.clear()
    await set_work(database, task, state="completed", model_requests=1)
    report = {
        "run_ref": run.run_id,
        "sequence": 1,
        "response_id": "real-response-id",
        "at": time.time(),
        "delta": {"engage": "quiet", "mood": "平静"},
    }
    async with database.sessions() as session, session.begin():
        await session.execute(
            journal.insert().values(
                work_id=task["id"],
                chain_id=str(uuid4()),
                contract="synthetic",
                source_revision=1,
                phase="delivered",
                updated=time.time(),
                payload_json=json.dumps(
                    {
                        "metadata": {
                            "progress": {
                                "self_reports": [
                                    report,
                                    {"bad": "record"},
                                    {**report, "run_ref": "unrelated"},
                                ],
                            }
                        }
                    }
                ),
            )
        )
    await reconcile_run(service, run)
    assert item.controller.state.engagement_report.run_ref == run.run_id
    assert item.controller._willingness(time.time()) < 0
    assert not item.controller.state.proposals
    assert not item.controller.state.feedback
    await reconcile_run(service, run)
    assert len(item.controller.state.self_reports) == 1


@pytest.mark.asyncio
async def test_worker_and_root_model_charges_are_paged_without_summing_budget_twice(database):
    service, item, run, task = await setup(database)
    child_id = str(uuid4())
    await set_work(database, task, model_requests=26, state="completed")
    async with database.sessions() as session, session.begin():
        child = {
            **task,
            "id": child_id,
            "source_key": f"child:{child_id}",
            "state": "completed",
            "model_requests": 94,
        }
        await session.execute(work.insert().values(**child))
        await session.execute(
            children.insert().values(
                work_id=child_id,
                root_id=task["id"],
                source_key=child["source_key"],
                brief_json="{}",
            )
        )
        await session.execute(budgets.insert().values(root_id=task["id"], models=120, tools=0))
    await reconcile_run(service, run)
    assert len(item.controller.state.effects) == 120
    assert (
        sum(key.startswith(f"work-model:{task['id']}:") for key in item.controller.state.effects)
        == 26
    )
    assert (
        sum(key.startswith(f"work-model:{child_id}:") for key in item.controller.state.effects)
        == 94
    )
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(InitiativeFeedbackModel)) == 2
