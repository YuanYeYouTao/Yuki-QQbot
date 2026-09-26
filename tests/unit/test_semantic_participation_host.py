"""Real DB/route host contracts with synthetic semantics, never a real Jev/QQ call."""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, update
from tests.unit.test_memory_mutation import _event
from tests.unit.test_self_initiative_memory_quality import reflection_fact
from yuki_participation.models import (
    CandidateKind,
    Choice,
    Observation,
    Proposal,
    Snapshot,
)
from yuki_participation.rubric import CRITERIA, REVISION
from yuki_participation.store import SnapshotStore

from qq_ai_bot.conversation.autonomy_binding import AutonomyOwner
from qq_ai_bot.conversation.autonomy_db_models import InitiativeRunModel
from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    SpaceActiveRouteModel,
)
from qq_ai_bot.conversation.hydrate import bump_canonical_generation
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.identity.canonical_repository import ensure_presence
from qq_ai_bot.identity.db_models import CanonicalSpaceModel, SpaceBindingModel
from qq_ai_bot.memory.enums import MemoryScopeType, MemorySourceType
from qq_ai_bot.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.runtime.work_schema_v1 import work
from qq_ai_bot.services.policies import EffectiveGroupPolicy, evaluate_message
from qq_ai_bot.services.semantic_participation import SemanticParticipationService

pytestmark = pytest.mark.asyncio


def _choice(dimension, option):
    return Choice(
        choice=option,
        probabilities={key: float(key == option) for key in CRITERIA[dimension]},
    )


def _observation(snapshot, *, unknown=False, act="invite_yuki", unit=None):
    answers = {
        "interaction_mark": _choice("interaction_mark", "unknown" if unknown else act),
        "information_state": _choice("information_state", "unknown" if unknown else "refine"),
        "floor_state": _choice("floor_state", "unknown" if unknown else "yuki"),
        "boundary_scope": _choice("boundary_scope", "unknown" if unknown else "target_thread"),
    }
    if snapshot.kind != CandidateKind.CONVERSATION:
        answers["seed_fit"] = _choice("seed_fit", "unknown" if unknown else "appropriate")
    resolved = None
    if snapshot.focus.unit_ambiguous:
        selected = "unknown" if unknown else unit or snapshot.focus.unit_options[0].key
        answers["unit_selection"] = Choice(
            choice=selected,
            probabilities={
                **{
                    option.key: float(option.key == selected)
                    for option in snapshot.focus.unit_options
                },
                "unknown": float(selected == "unknown"),
            },
        )
        resolved = next((o for o in snapshot.focus.unit_options if o.key == selected), None)
    return Observation(
        observation_id=f"fixture:{snapshot.focus.ref.event_id}:{snapshot.sequence}",
        snapshot=snapshot,
        provider="synthetic_fixture",
        model_revision="test",
        rubric_revision=REVISION,
        received_at=max(time.time(), snapshot.issued_at),
        answers=answers,
        resolved_unit=resolved,
    )


class Observer:
    def __init__(self, *, unknown=False):
        self.unknown = unknown
        self.calls = []
        self.aclose = AsyncMock()

    async def evaluate(self, snapshot):
        self.calls.append(snapshot)
        return _observation(snapshot, unknown=self.unknown)


async def _event_and_route(database, ledger, *, group="2001", content="Yuki也来说说吧"):
    event, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id=str(uuid4()),
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content=content,
        group_id=group,
        occurred_at=datetime.now(UTC) - timedelta(seconds=40),
    )
    async with database.immediate_session() as db:
        conv = await db.get(CanonicalConversationModel, event.canonical_conversation_id)
        space = await db.get(CanonicalSpaceModel, conv.space_id)
        space.enabled = space.autonomous_enabled = True
        binding = await db.scalar(
            select(SpaceBindingModel).where(
                SpaceBindingModel.space_id == conv.space_id,
                SpaceBindingModel.status == "active",
            )
        )
        route = await db.get(SpaceActiveRouteModel, conv.space_id)
        if route is None:
            db.add(
                SpaceActiveRouteModel(
                    space_id=conv.space_id,
                    space_binding_id=binding.id,
                    presence_id=event.ingress_presence_id,
                    route_generation=1,
                    paused=False,
                    revision=1,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
    return event


def _message(event):
    return InboundMessage(
        message_id=event.platform_message_id,
        event_type="message",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id=event.sender_user_id),
        text=event.content,
        bot_user_id=event.bot_user_id,
        group_id=event.group_id,
        conversation_id=event.canonical_conversation_id,
        presence_id=event.ingress_presence_id,
        source_event_id=event.id,
    )


async def _host(database, tmp_path, *, observer=True):
    policy = SimpleNamespace(autonomous_enabled=True, semantic_participation_enabled=True)
    app = SimpleNamespace(
        database=database,
        ledger=EventLedgerRepository(database),
        settings=SimpleNamespace(bot_aliases=("Yuki", "由纪")),
        runtime_config=SimpleNamespace(
            snapshot=AsyncMock(
                return_value=SimpleNamespace(
                    conversation_policy=lambda: policy,
                )
            )
        ),
    )
    host = SemanticParticipationService(app)
    host._store = SnapshotStore(tmp_path / f"participation-{uuid4()}.db")
    host._observer = Observer() if observer else None
    return host, policy


async def test_model_profile_hot_reload_preserves_state_and_last_good_value(database, tmp_path):
    event = await _event_and_route(database, EventLedgerRepository(database))
    host, _ = await _host(database, tmp_path)
    try:
        item = await _item(host, event)
        profile = tmp_path / "autonomous-model.json"
        host._model_config_path = profile
        before_state = item.controller.state.model_dump_json()
        before_rate = item.controller.intrinsic_opportunity(item.controller.state.now)

        interval = item.controller.parameters.intrinsic_interval_seconds / 2
        profile.write_text(json.dumps({"intrinsic_interval_seconds": interval}), encoding="utf-8")
        host._refresh_model_parameters()
        assert item.controller.intrinsic_opportunity(item.controller.state.now) == pytest.approx(
            before_rate * 2
        )
        assert item.controller.state.model_dump_json() == before_state

        profile.write_text('{"intrinsic_interval_seconds":0}', encoding="utf-8")
        host._refresh_model_parameters()
        assert host._model_config_error is not None
        assert item.controller.intrinsic_opportunity(item.controller.state.now) == pytest.approx(
            before_rate * 2
        )

        profile.unlink()
        host._refresh_model_parameters()
        assert host._model_config_error is None
        assert item.controller.intrinsic_opportunity(item.controller.state.now) == pytest.approx(
            before_rate
        )
    finally:
        host._store.close()


async def test_human_activity_bootstraps_from_current_generation_without_replaying_work(
    database, tmp_path
):
    event = await _event_and_route(database, EventLedgerRepository(database))
    old_at = datetime.now(UTC) - timedelta(hours=2)
    async with database.immediate_session() as session:
        await session.execute(
            update(ChatEventModel).where(ChatEventModel.id == event.id).values(occurred_at=old_at)
        )
    await EventLedgerRepository(database).append(
        bot_user_id="8000",
        platform_message_id=str(uuid4()),
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content="之前的群聊",
        group_id="2001",
        occurred_at=old_at + timedelta(seconds=5),
    )
    await EventLedgerRepository(database).append(
        bot_user_id="8000",
        platform_message_id=str(uuid4()),
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content="继续聊",
        group_id="2001",
        occurred_at=datetime.now(UTC) - timedelta(seconds=40),
    )
    host, _ = await _host(database, tmp_path)
    try:
        item = await _item(host, event)
        trace = item.controller._human_activity(time.time())
        assert item.controller.state.human_activity_initialized
        assert 2.8 < trace < 3.1  # two seeded old events plus one recent event
        assert item.controller.intrinsic_opportunity(time.time()) > 0
        assert not item.controller.state.proposals
        await host._hydrate(item)
        assert item.controller._human_activity(time.time()) == pytest.approx(trace, rel=0.001)
    finally:
        host._store.close()


async def _item(host, event):
    scene = await host._scene(event.canonical_conversation_id)
    assert scene is not None, "fixture must use the real route resolver"
    item = host._session(scene)
    await host._hydrate(item)
    return item


def _proposal(item, binding, *sources, kind=CandidateKind.CONVERSATION):
    """Register a complete synthetic proposal; host must still revalidate every DB source."""
    now = time.time()
    for source in sources:
        if source.ref.event_id not in item.controller.state.candidates:
            _score(item, source, kind=kind)
    supports = tuple(item.controller.state.candidates[s.ref.event_id].support for s in sources)
    result = Proposal(
        proposal_id=str(uuid4()),
        scope=item.scene.scope,
        controller_epoch=binding.controller_epoch,
        kind=kind,
        thread=sources[0].thread,
        target_hint=sources[0].target,
        sources=tuple(source.ref for source in sources),
        support=supports[0],
        supports=supports,
        created_at=now,
        expires_at=now + 30,
    )
    item.controller.state.proposals[result.proposal_id] = result
    item.controller._set(pending=result.proposal_id)
    return result


def _score(item, source, *, act="invite_yuki", unit=None, kind=CandidateKind.CONVERSATION):
    snapshot = Snapshot(
        scope=item.scene.scope,
        focus=source,
        context=(),
        sequence=len(item.controller.state.observations) + 1,
        issued_at=time.time(),
        kind=kind,
    )
    assert item.controller.apply_semantic_observation(_observation(snapshot, act=act, unit=unit))


async def _runs(host):
    async with host.database.sessions() as db:
        return list(await db.scalars(select(InitiativeRunModel)))


async def test_real_route_admission_dispatch_and_reconcile_create_one_actorless_work(
    database, tmp_path
):
    host, _ = await _host(database, tmp_path)
    try:
        event = await _event_and_route(database, host.app.ledger)
        item = await _item(host, event)
        binding = await host._binding(item)
        source = item.controller.state.events[f"event:{event.id}"]
        assert source.observation_priority
        direct = evaluate_message(
            _message(event),
            SimpleNamespace(ai_prefix="!ai", superusers=frozenset()),
            group_policy=EffectiveGroupPolicy(enabled=True, require_mention=True),
        )
        assert not direct.should_respond
        proposal = _proposal(item, binding, source)
        await host._admit(item, binding, proposal)
        (run,) = await host.repository.list_active()
        await host._reconcile(run)
        await host._reconcile(run)
        await host._dispatch(run)
        saved = await host.work.by_source(f"initiative:{run.run_id}")
        assert saved is not None
        payload = json.loads(saved["source_json"])
        assert payload["principal_kind"] == "self" and payload["actor_user_id"] == ""
        assert payload["initiative_run_id"] == run.run_id
        assert "source_event_id" not in payload and "trigger_event_id" not in payload
        assert event.content in payload["instruction"]
        async with database.sessions() as db:
            assert await db.scalar(select(func.count()).select_from(work)) == 1
        assert len(await _runs(host)) == 1
    finally:
        await host.close()


@pytest.mark.parametrize("master", [False, True])
async def test_missing_key_retains_explicit_semantic_policy(database, tmp_path, master):
    host, policy = await _host(database, tmp_path, observer=False)
    policy.autonomous_enabled = master
    try:
        event = await _event_and_route(database, host.app.ledger)
        assert not await host.legacy_allowed(_message(event))
        assert not await host.accept_legacy(_message(event))
        binding = await host.repository.get_binding(event.canonical_conversation_id, 1)
        assert binding.effective_owner is (AutonomyOwner.SEMANTIC if master else AutonomyOwner.OFF)
        assert not await _runs(host)
    finally:
        await host.close()


async def test_valid_unknown_is_semantic_health_not_legacy_fallback(database, tmp_path):
    host, _ = await _host(database, tmp_path)
    host._observer.unknown = True
    try:
        event = await _event_and_route(database, host.app.ledger)
        item = await _item(host, event)
        await host._advance_scene(item)
        assert len(host._observer.calls) == 1
        assert item.observation.health.failures == 0
        assert not item.controller.state.candidates
        assert not await host.legacy_allowed(_message(event))
        assert not await host.accept_legacy(_message(event))
        assert not await _runs(host)
    finally:
        await host.close()


async def test_observer_outage_does_not_disable_intrinsic_or_switch_owner(
    database, tmp_path, monkeypatch
):
    host, _ = await _host(database, tmp_path)
    try:
        event = await _event_and_route(database, host.app.ledger)
        item = await _item(host, event)

        async def unavailable(snapshot):
            raise httpx.ConnectError("synthetic")

        host._observer.evaluate = unavailable
        item.observation.health.failures = 3
        item.observation.health.degraded = True
        state = item.controller.state.model_dump_json()
        binding = await host._binding(item)
        assert binding.effective_owner is AutonomyOwner.SEMANTIC
        assert item.controller.state.model_dump_json() == state
        epoch = binding.controller_epoch

        monkeypatch.setattr(item.controller, "_sample", lambda _sequence, _stream: 0.0)
        # First advance synchronizes the binding epoch; second samples a real
        # intrinsic proposal and exercises Host admission during the outage.
        await host._advance_scene(item)
        await host._advance_scene(item)
        (run,) = await _runs(host)
        assert run.trigger_kind == "intrinsic" and run.owner == AutonomyOwner.SEMANTIC.value
        assert run.sources_json == "[]"
        assert item.observation.last_error == "transport"
        assert (await host._binding(item)).controller_epoch == epoch
        assert not await host.legacy_allowed(_message(event))
        assert not await host.accept_legacy(_message(event))
    finally:
        await host.close()


async def test_existing_human_work_is_busy_without_consuming_semantic_source(database, tmp_path):
    host, _ = await _host(database, tmp_path)
    try:
        event = await _event_and_route(database, host.app.ledger)
        item = await _item(host, event)
        lease = await host.work.acquire(item.scene.conversation_id, item.scene.generation)
        try:
            await host.work.accept(
                lease,
                source_key=f"event:{event.id}",
                source={"origin": "user_message"},
                goal="继续用户工作",
            )
        finally:
            await host.work.release(lease)
        binding = await host._binding(item)
        source = item.controller.state.events[f"event:{event.id}"]
        await host._admit(item, binding, _proposal(item, binding, source))
        assert not await _runs(host)
        assert source.ref.event_id not in item.controller.state.consumed
        assert any(f.outcome == "busy" for f in item.controller.state.feedback.values())
    finally:
        await host.close()


@pytest.mark.parametrize("change", ["content", "suppressed", "generation"])
async def test_source_or_generation_change_rejects_unaccepted_proposal(database, tmp_path, change):
    host, _ = await _host(database, tmp_path)
    try:
        event = await _event_and_route(database, host.app.ledger)
        item = await _item(host, event)
        binding = await host._binding(item)
        source = item.controller.state.events[f"event:{event.id}"]
        proposal = _proposal(item, binding, source)
        async with database.immediate_session() as db:
            if change == "generation":
                await db.execute(
                    update(CanonicalConversationModel)
                    .where(
                        CanonicalConversationModel.id == event.canonical_conversation_id,
                    )
                    .values(generation=2)
                )
            elif change == "content":
                await db.execute(
                    update(ChatEventModel)
                    .where(ChatEventModel.id == event.id)
                    .values(content="撤回原来的参与线索")
                )
            else:
                await db.execute(
                    update(ChatEventModel)
                    .where(ChatEventModel.id == event.id)
                    .values(suppression_status="duplicate", utterance_fingerprint="a" * 64)
                )
        await host._admit(item, binding, proposal)
        assert not await _runs(host)
    finally:
        await host.close()


@pytest.mark.parametrize("complete", [False, True])
async def test_multisource_proposal_requires_complete_support(database, tmp_path, complete):
    host, _ = await _host(database, tmp_path)
    try:
        first = await _event_and_route(database, host.app.ledger)
        second = await _event_and_route(database, host.app.ledger, content="我还有一个相关细节")
        item = await _item(host, second)
        binding = await host._binding(item)
        sources = tuple(item.controller.state.events[f"event:{e.id}"] for e in (first, second))
        _score(item, sources[0])
        continuation = next(o for o in sources[1].unit_options if o.thread == sources[0].thread)
        _score(item, sources[1], unit=continuation.key)
        proposal = _proposal(item, binding, *sources)
        offered = (
            proposal
            if complete
            else proposal.model_copy(
                update={"supports": (proposal.supports[0],)},
            )
        )
        await host._admit(item, binding, offered)
        runs = await _runs(host)
        assert len(runs) == int(complete), "every offered source needs its own live support"
        if complete:
            assert {row["source_id"] for row in json.loads(runs[0].sources_json)} == {
                str(first.id),
                str(second.id),
            }
            assert len(json.loads(runs[0].support_refs_json)) == 2
    finally:
        await host.close()


async def test_source_change_after_preflight_is_rejected_in_admission_transaction(
    database,
    tmp_path,
    monkeypatch,
):
    host, _ = await _host(database, tmp_path)
    try:
        event = await _event_and_route(database, host.app.ledger)
        item = await _item(host, event)
        binding = await host._binding(item)
        source = item.controller.state.events[f"event:{event.id}"]
        proposal = _proposal(item, binding, source)
        accept = host.repository.accept_host_proposal

        async def change_before_write(**kwargs):
            async with database.immediate_session() as db:
                await db.execute(
                    update(ChatEventModel)
                    .where(ChatEventModel.id == event.id)
                    .values(content="预检已完成之后更改来源")
                )
            return await accept(**kwargs)

        monkeypatch.setattr(host.repository, "accept_host_proposal", change_before_write)
        await host._admit(item, binding, proposal)
        assert not await _runs(host)
        assert source.ref.event_id not in item.controller.state.consumed
    finally:
        await host.close()


async def test_observer_to_proposal_to_outbox_uses_the_same_self_work_path(
    database,
    tmp_path,
    monkeypatch,
):
    host, _ = await _host(database, tmp_path)
    try:
        event = await _event_and_route(database, host.app.ledger)
        item = await _item(host, event)
        binding = await host._binding(item)
        item.controller.advance(
            time.time(), controller_epoch=binding.controller_epoch, host_available=True
        )
        # Deterministic crossing of the stochastic admission threshold, without sleeping.
        item.controller._set(threshold=1e-12)
        await host.tick()
        # The V6 attention ramp intentionally does not jump on the HTTP response instant.
        future = time.time() + 8
        monkeypatch.setattr(
            "qq_ai_bot.services.semantic_participation.time", SimpleNamespace(time=lambda: future)
        )
        await host.tick()
        assert len(host._observer.calls) == 1
        runs = await host.repository.list_active()
        assert len(runs) == 1
        (run,) = runs
        assert run.owner is AutonomyOwner.SEMANTIC
        saved = await host.work.by_source(f"initiative:{run.run_id}")
        assert saved is not None
        assert json.loads(saved["source_json"])["principal_kind"] == "self"
        assert host._failures == 0
    finally:
        await host.close()


async def test_generation_reset_before_dispatch_interrupts_accepted_run(database, tmp_path):
    host, _ = await _host(database, tmp_path)
    try:
        event = await _event_and_route(database, host.app.ledger)
        item = await _item(host, event)
        binding = await host._binding(item)
        source = item.controller.state.events[f"event:{event.id}"]
        await host._admit(item, binding, _proposal(item, binding, source))
        (run,) = await host.repository.list_active()
        async with database.immediate_session() as db:
            await db.execute(
                update(CanonicalConversationModel)
                .where(
                    CanonicalConversationModel.id == item.scene.conversation_id,
                )
                .values(generation=2)
            )
        await host._dispatch(run)
        assert await host.work.by_source(f"initiative:{run.run_id}") is None
        assert (await host.repository.get_run(run.run_id)).state == "interrupted"
    finally:
        await host.close()


async def test_generation_reset_without_new_message_restores_semantic_sampling(database, tmp_path):
    host, _ = await _host(database, tmp_path)
    try:
        event = await _event_and_route(database, host.app.ledger)
        item = await _item(host, event)
        prior = await host._binding(item)
        assert prior.effective_owner is AutonomyOwner.SEMANTIC
        async with database.immediate_session() as db:
            assert (
                await bump_canonical_generation(
                    db, event.canonical_conversation_id, event_id=event.id, force=True
                )
                == 2
            )
        assert await host.repository.get_binding(event.canonical_conversation_id, 2) is None
        assert (event.canonical_conversation_id, 2) in (
            await host.repository.list_current_autonomous_scopes()
        )

        await host.tick()  # No inbound message after the reset.
        current = await host.repository.get_binding(event.canonical_conversation_id, 2)
        assert current is not None and current.effective_owner is AutonomyOwner.SEMANTIC
        assert (event.canonical_conversation_id, 2) in host._sessions
        assert (event.canonical_conversation_id, 1) not in host._sessions
        assert (
            host._sessions[(event.canonical_conversation_id, 2)].controller.state.last_human_at
            is None
        )
        assert (
            host._sessions[(event.canonical_conversation_id, 2)].controller.intrinsic_opportunity(
                time.time()
            )
            == 0
        )

        await host.tick()
        assert (
            host._sessions[(event.canonical_conversation_id, 2)].controller.state.sample_sequence
            > 0
        )
        assert not await _runs(host)
    finally:
        await host.close()


async def test_stop_from_same_person_with_resolved_thread_revokes_pending_opportunity(
    database, tmp_path
):
    host, _ = await _host(database, tmp_path)
    try:
        invitation = await _event_and_route(database, host.app.ledger)
        item = await _item(host, invitation)
        binding = await host._binding(item)
        source = item.controller.state.events[f"event:{invitation.id}"]
        _score(item, source)
        proposal = _proposal(item, binding, source)
        stop = await _event_and_route(database, host.app.ledger, content="这件事请Yuki停止参与")
        await host._hydrate(item)
        stopped = item.controller.state.events[f"event:{stop.id}"]
        option = next(o for o in stopped.unit_options if o.thread == source.thread)
        _score(item, stopped, act="ask_yuki_stop", unit=option.key)
        await host._admit(item, binding, proposal)
        assert not await _runs(host)
        assert not item.controller.source_allowed(source)
    finally:
        await host.close()


async def test_memory_seed_can_be_only_focus_and_never_reads_person_memory(database, tmp_path):
    facts, reflected_event, _, _, fact_id = await reflection_fact(database)
    private_event = await _event(
        EventLedgerRepository(database),
        message_id=str(uuid4()),
        sender_user_id="1001",
        content="只有我私聊说的秘密",
    )
    private_fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            memory_key="test.private",
            category="preference",
            content=private_event.content,
            source_type=MemorySourceType.EXPLICIT,
        ),
        evidence=MemoryEvidenceCreate(
            event_id=private_event.id,
            source_speaker_user_id="1001",
            excerpt=private_event.content,
            relation="self_statement",
        ),
    )
    host, _ = await _host(database, tmp_path)
    try:
        current = await _event_and_route(database, host.app.ledger, group="3001")
        item = await _item(host, current)
        # Real scene context is allowed; the resulting proposal has only a Memory source.
        await host._seeds(item)
        seeds = [e for e in item.controller.state.events.values() if e.kind == "seed"]
        assert [e.ref.event_id for e in seeds] == [f"memory:{fact_id}"]
        assert all(private_event.content not in e.text for e in seeds)
        assert f"memory:{private_fact.id}" not in item.controller.state.events
        binding = await host._binding(item)
        proposal = _proposal(item, binding, seeds[0], kind=CandidateKind.RECALL)
        await host._admit(item, binding, proposal)
        (run,) = await host.repository.list_active()
        assert [(s.kind.value, s.source_id) for s in run.sources] == [("memory", str(fact_id))]
        await host._dispatch(run)
        saved = await host.work.by_source(f"initiative:{run.run_id}")
        assert saved is not None and private_event.content not in saved["source_json"]
        assert reflected_event.canonical_conversation_id == item.scene.conversation_id
    finally:
        await host.close()


async def test_slow_observer_does_not_hold_global_lock_or_block_another_scope(database, tmp_path):
    host, _ = await _host(database, tmp_path)
    entered, release, other_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    try:
        first = await _event_and_route(database, host.app.ledger)
        other = await _event_and_route(database, host.app.ledger, group="2002")
        await _item(host, first)
        await _item(host, other)

        async def evaluate(snapshot):
            if snapshot.scope.conversation_id == first.canonical_conversation_id:
                entered.set()
                await release.wait()
            else:
                other_finished.set()
            return _observation(snapshot, unknown=True)

        host._observer.evaluate = evaluate
        task = asyncio.create_task(host.tick())
        await asyncio.wait_for(entered.wait(), timeout=3)
        try:
            await asyncio.wait_for(other_finished.wait(), timeout=3)
            assert not host._lock.locked()
            assert not await asyncio.wait_for(host.legacy_allowed(_message(other)), timeout=3)
            # A DB writer is also independent of the in-flight semantic HTTP request.
            async with asyncio.timeout(3):
                async with database.immediate_session() as db:
                    await db.execute(update(CanonicalSpaceModel).values(name="write-during-http"))
        finally:
            release.set()
            await asyncio.wait_for(task, timeout=3)
        assert host._failures == 0
    finally:
        release.set()
        await host.close()


async def test_accepted_pending_recovery_after_mode_and_route_change_uses_original_presence(
    database, tmp_path
):
    host, policy = await _host(database, tmp_path)
    try:
        event = await _event_and_route(database, host.app.ledger)
        item = await _item(host, event)
        binding = await host._binding(item)
        source = item.controller.state.events[f"event:{event.id}"]
        proposal = _proposal(item, binding, source)
        host._save(item)
        # Simulate crash after host admission committed, before controller accepted feedback.
        result = await host.repository.accept_host_proposal(
            proposal_id=proposal.proposal_id,
            binding=binding,
            owner=AutonomyOwner.SEMANTIC,
            space_id=item.scene.space_id,
            presence_id=item.scene.presence_id,
            sources=(host._source(item, source.ref),),
            target_person_id=source.target,
            support_refs=(proposal.support.observation_id,),
        )
        assert result.run is not None
        old_presence = item.scene.presence_id
        policy.semantic_participation_enabled = False
        async with database.immediate_session() as db:
            new_presence = await ensure_presence(db, "8001")
            route = await db.get(SpaceActiveRouteModel, item.scene.space_id)
            route.presence_id = new_presence
            route.route_generation += 1
            route.revision += 1
        host._sessions.clear()
        recovered = host._session(await host._scene(event.canonical_conversation_id))
        await host._advance_scene(recovered)
        await host._reconcile(result.run)
        await host._reconcile(result.run)
        saved = await host.work.by_source(f"initiative:{result.run.run_id}")
        assert saved is not None
        assert json.loads(saved["source_json"])["presence_id"] == old_presence
        assert json.loads(saved["source_json"])["bot_user_id"] == "8000"
        assert recovered.controller.state.proposal_runs[proposal.proposal_id] == result.run.run_id
        assert len(await _runs(host)) == 1
        async with database.sessions() as db:
            assert await db.scalar(select(func.count()).select_from(work)) == 1
    finally:
        await host.close()
