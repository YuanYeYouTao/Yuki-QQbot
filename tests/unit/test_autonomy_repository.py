"""The dormant host protocol must survive competing proposers and process restarts."""

import asyncio
from dataclasses import dataclass

import pytest
from sqlalchemy import func, select, update

from qq_ai_bot.conversation.autonomy_binding import (
    AutonomyBinding,
    AutonomyOwner,
    InitiativeSource,
    InitiativeSourceKind,
)
from qq_ai_bot.conversation.autonomy_db_models import (
    InitiativeFeedbackModel,
    InitiativeRunModel,
    InitiativeSourceClaimModel,
)
from qq_ai_bot.conversation.autonomy_repository import AutonomyConflict, AutonomyRepository
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.hydrate import ensure_canonical_conversation
from qq_ai_bot.identity.canonical_repository import ensure_presence, ensure_space
from qq_ai_bot.persistence.database import Database


@dataclass(frozen=True)
class Scene:
    conversation: str
    space: str
    presence: str


async def _scene(database: Database) -> Scene:
    async with database.immediate_session() as session:
        space = await ensure_space(session, "2001")
        presence = await ensure_presence(session, "8000")
        conversation = await ensure_canonical_conversation(
            session, kind="space", primary_scope_key="group:8000:2001", space_id=space
        )
    return Scene(conversation.conversation_id, space, presence)


def _event(number: int = 1, revision: str = "v1") -> tuple[InitiativeSource, ...]:
    # Fixtures stand in for already host-resolved focus references. The repository is
    # deliberately not the future semantic/source authorization adapter.
    return (InitiativeSource(InitiativeSourceKind.EVENT, str(number), revision),)


async def _enable(repository: AutonomyRepository, scene: Scene) -> AutonomyBinding:
    current = await repository.ensure_binding(scene.conversation, 1)
    return await repository.transition(current, master_enabled=True, external_enabled=True)


async def _accept(repository, scene, binding, proposal="p1", sources=None):
    return await repository.accept_host_proposal(
        proposal_id=proposal,
        binding=binding,
        owner=binding.effective_owner,
        space_id=scene.space,
        presence_id=scene.presence,
        sources=sources or _event(),
        support_refs=("observation:1",),
    )


async def test_selector_defaults_off_and_cas_fences_stale_provider_recovery(database):
    scene = await _scene(database)
    repository = AutonomyRepository(database)
    initial = await repository.ensure_binding(scene.conversation, 1)
    assert initial.effective_owner is AutonomyOwner.OFF
    assert not initial.master_enabled and not initial.external_enabled
    semantic = await _enable(repository, scene)
    off = await repository.transition(semantic, master_enabled=False, external_enabled=True)
    with pytest.raises(AutonomyConflict, match="autonomy_binding_changed"):
        await repository.transition(semantic, master_enabled=True, external_enabled=True)
    assert await repository.get_binding(scene.conversation, 1) == off
    recovered = await repository.transition(off, master_enabled=False, external_enabled=True)
    assert recovered == off
    assert (await _accept(repository, scene, semantic)).outcome == "disabled"


async def test_parallel_selector_updates_have_one_winner(database):
    scene = await _scene(database)
    repository = AutonomyRepository(database)
    initial = await repository.ensure_binding(scene.conversation, 1)
    results = await asyncio.gather(
        repository.transition(initial, master_enabled=True, external_enabled=True),
        repository.transition(initial, master_enabled=True, external_enabled=False),
        return_exceptions=True,
    )
    assert sum(isinstance(result, AutonomyBinding) for result in results) == 1
    assert sum(isinstance(result, AutonomyConflict) for result in results) == 1


async def test_concurrent_proposal_replay_returns_one_run_and_conflicts_are_rejected(database):
    scene = await _scene(database)
    repository = AutonomyRepository(database)
    binding = await _enable(repository, scene)
    results = await asyncio.gather(*(_accept(repository, scene, binding) for _ in range(4)))
    assert sorted(result.outcome for result in results) == [
        "accepted",
        "duplicate",
        "duplicate",
        "duplicate",
    ]
    assert len({result.run.run_id for result in results}) == 1
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(InitiativeRunModel)) == 1
        assert (
            await session.scalar(select(func.count()).select_from(InitiativeSourceClaimModel)) == 1
        )
    with pytest.raises(AutonomyConflict, match="initiative_proposal_replay_conflict"):
        await _accept(repository, scene, binding, sources=_event(2))


async def test_racing_proposals_consume_focus_once_and_busy_does_not_consume_new_source(database):
    scene = await _scene(database)
    repository = AutonomyRepository(database)
    binding = await _enable(repository, scene)
    results = await asyncio.gather(
        _accept(repository, scene, binding, "one"),
        _accept(repository, scene, binding, "two"),
    )
    assert {result.outcome for result in results} == {"accepted", "source_considered"}
    winner = next(result.run for result in results if result.outcome == "accepted")
    assert (await _accept(repository, scene, binding, "new", _event(2))).outcome == "busy"
    await repository.record_feedback(winner.run_id, sequence=1, outcome="no_reply")
    assert (await _accept(repository, scene, binding, "new", _event(2))).outcome == "accepted"


async def test_mode_switch_preserves_accepted_work_and_cross_owner_source_dedup(database):
    scene = await _scene(database)
    repository = AutonomyRepository(database)
    binding = await _enable(repository, scene)
    accepted = await _accept(repository, scene, binding)
    legacy = await repository.transition(binding, master_enabled=True, external_enabled=False)
    assert (await _accept(repository, scene, binding, "late", _event(2))).outcome == "stale_binding"
    assert (await _accept(repository, scene, binding)).run.run_id == accepted.run.run_id
    assert (await _accept(repository, scene, legacy, "fresh", _event(2))).outcome == "busy"
    await repository.record_feedback(
        accepted.run.run_id, sequence=1, outcome="completed", considered_sources=_event(2)
    )
    assert (await _accept(repository, scene, legacy, "old-source")).outcome == "source_considered"
    assert (
        await _accept(repository, scene, legacy, "actually-considered", _event(2))
    ).outcome == "source_considered"
    assert (
        await _accept(repository, scene, legacy, "new-revision", _event(1, "v2"))
    ).outcome == "accepted"


async def test_memory_only_source_and_feedback_survive_connection_restart_and_master_off(database):
    scene = await _scene(database)
    repository = AutonomyRepository(database)
    binding = await _enable(repository, scene)
    memory = (InitiativeSource(InitiativeSourceKind.MEMORY, "42", "fact-revision:3"),)
    accepted = await _accept(repository, scene, binding, sources=memory)
    await repository.transition(binding, master_enabled=False, external_enabled=False)
    await database.engine.dispose()
    reopened = Database(database.url)
    try:
        recovered = AutonomyRepository(reopened)
        assert (await recovered.get_run(accepted.run.run_id)).sources == memory
        assert (
            await recovered.query_proposal(
                conversation_id=scene.conversation,
                generation=1,
                owner=AutonomyOwner.SEMANTIC,
                controller_epoch=binding.controller_epoch,
                proposal_id="p1",
            )
        ).run_id == accepted.run.run_id
        completed = await recovered.record_feedback(
            accepted.run.run_id, sequence=1, outcome="completed", effect_refs=("send-receipt:1",)
        )
        assert completed.state == "completed"
        assert (
            await recovered.record_feedback(
                accepted.run.run_id,
                sequence=1,
                outcome="completed",
                effect_refs=("send-receipt:1",),
            )
            == completed
        )
        with pytest.raises(AutonomyConflict, match="initiative_feedback_replay_conflict"):
            await recovered.record_feedback(accepted.run.run_id, sequence=1, outcome="failed")
        late = await recovered.record_feedback(
            accepted.run.run_id, sequence=2, outcome="interrupted", effect_refs=("late-receipt:2",)
        )
        assert late.state == "completed" and late.feedback_sequence == 2
        with pytest.raises(AutonomyConflict, match="initiative_run_terminal"):
            await recovered.record_feedback(accepted.run.run_id, sequence=3, outcome="running")
        async with reopened.sessions() as session:
            assert (
                await session.scalar(select(func.count()).select_from(InitiativeFeedbackModel)) == 2
            )
    finally:
        await reopened.close()


async def test_new_generation_refuses_old_proposals_but_retains_factual_late_feedback(database):
    scene = await _scene(database)
    repository = AutonomyRepository(database)
    binding = await _enable(repository, scene)
    accepted = await _accept(repository, scene, binding)
    async with database.immediate_session() as session:
        await session.execute(
            update(CanonicalConversationModel)
            .where(CanonicalConversationModel.id == scene.conversation)
            .values(generation=2)
        )
    assert (
        await _accept(repository, scene, binding, "stale", _event(2))
    ).outcome == "stale_binding"
    next_binding = await repository.ensure_binding(scene.conversation, 2)
    assert next_binding.effective_owner is AutonomyOwner.OFF
    with pytest.raises(AutonomyConflict, match="initiative_feedback_sequence_gap"):
        await repository.record_feedback(accepted.run.run_id, sequence=2, outcome="completed")
    late = await repository.record_feedback(
        accepted.run.run_id,
        sequence=1,
        outcome="interrupted",
        effect_refs=("terminal:already-done",),
    )
    assert late.generation == 1 and late.feedback_sequence == 1


@pytest.mark.parametrize(
    "kind,source_id,revision",
    [
        ("event", "1", "v1"),
        (InitiativeSourceKind.EVENT, "qq:1", "v1"),
        (InitiativeSourceKind.MEMORY, "01", "v1"),
        (InitiativeSourceKind.EVENT, "1", ""),
    ],
)
def test_sources_reject_platform_or_ambiguous_ids(kind, source_id, revision):
    with pytest.raises(ValueError):
        InitiativeSource(kind, source_id, revision)
