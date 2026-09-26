"""Read-only aggregate evidence, with explicit window and unknown metric semantics."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.unit.test_semantic_participation_host import (
    _event_and_route,
    _host,
    _item,
    _score,
)

from qq_ai_bot.conversation.autonomy_db_models import (
    AutonomyBindingModel,
    InitiativeFeedbackModel,
    InitiativeRunModel,
)
from qq_ai_bot.services.participation_diagnostics import participation_diagnostics

pytestmark = pytest.mark.asyncio


async def _seed(database, tmp_path, *, count=2):
    host, _ = await _host(database, tmp_path)
    event = await _event_and_route(database, host.app.ledger, content="private-source-marker")
    item = await _item(host, event)
    await host._binding(item)
    now = datetime.now(UTC)
    identities = []
    async with database.immediate_session() as db:
        binding = await db.scalar(select(AutonomyBindingModel))
        binding.fallback_reason = "private-reason-marker"
        for index in range(count):
            identity = str(uuid4())
            identities.append(identity)
            db.add(
                InitiativeRunModel(
                    id=identity,
                    proposal_id=f"private-proposal-{index}",
                    conversation_id=item.scene.conversation_id,
                    generation=item.scene.generation,
                    owner="semantic",
                    controller_epoch=binding.controller_epoch,
                    space_id=item.scene.space_id,
                    presence_id=item.scene.presence_id,
                    payload_hash="a" * 64,
                    sources_json="[]",
                    support_refs_json="[]",
                    state="no_reply" if index == 0 else "completed",
                    feedback_sequence=2,
                    created_at=now + timedelta(seconds=index),
                    updated_at=now,
                )
            )
        await db.flush()
        for sequence in (1, 2):
            db.add(
                InitiativeFeedbackModel(
                    run_id=identities[-1],
                    sequence=sequence,
                    outcome="completed",
                    payload_json=json.dumps(
                        {
                            "effects": [
                                "social:secret-message-id",
                                "tool:42",
                                "work-model:secret-work:1",
                            ]
                        }
                    ),
                    created_at=now,
                )
            )
    return host, item, identities


async def test_health_uses_bounded_facts_without_text_or_identifiers(database, tmp_path):
    host, item, identities = await _seed(database, tmp_path)
    try:
        source = next(iter(item.controller.state.events.values()))
        _score(item, source)
        state = item.controller.state
        observation = next(iter(state.observations.values()))
        key = next(iter(state.observations))
        state.observations[key] = observation.model_copy(update={"input_tokens": 17})
        candidate = next(iter(state.candidates.values()))
        state.candidates["private-prediction-marker"] = candidate.model_copy(
            update={
                "support": candidate.support.model_copy(update={"kind": "predicted"}),
            }
        )
        state.observer_checkpoint["health"]["failures"] = 2
        state.observer_checkpoint["last_failure"] = {
            "category": "authentication",
            "status": 401,
            "source": "private-failure-marker",
            "at": 0,
        }
        report = await host.health()
        facts = report["diagnostics"]
        assert facts["selector"]["fallbacks"]["other"] == 1
        assert facts["runs"]["accepted_count"] == 2
        assert facts["runs"]["no_reply_per_accepted"] == 0.5
        assert facts["runs"]["effect_runs_per_accepted"] == 0.5
        assert facts["runs"]["message_effects_known"] == 1
        assert facts["runs"]["tool_receipts_known"] == 1
        assert facts["runs"]["charged_model_requests_known"] == 1
        assert facts["controller"]["support"]["observed"] == 1
        assert facts["controller"]["support"]["predicted"] == 1
        provider = facts["controller"]["provider"]
        assert provider["input_tokens_known_sum"] == 17
        assert provider["output_tokens_known_sum"] is None
        assert provider["consecutive_failures_sum"] == 2
        assert provider["last_failure_categories"] == {"authentication": 1}
        assert provider["last_failure_http_statuses"] == {"401": 1}
        assert provider["latency_seconds"] is provider["accuracy"] is None
        encoded = json.dumps(report)
        for secret in (*identities, item.scene.conversation_id, "private-", "secret-"):
            assert secret not in encoded
    finally:
        await host.close()


async def test_run_window_reports_truncation_and_does_not_count_older_outcomes(database, tmp_path):
    host, item, _ = await _seed(database, tmp_path, count=130)
    try:
        facts = await participation_diagnostics(database, (item.controller.state,), now=0)
        assert facts["runs"]["truncated"] is True
        assert facts["runs"]["accepted_count"] == 128
        assert facts["runs"]["no_reply_per_accepted"] == 0
        assert facts["runs"]["effect_runs_per_accepted"] == 1 / 128
        assert facts["runs"]["feedback_complete"] is True
    finally:
        await host.close()


async def test_empty_window_is_unknown_not_zero_rate(database):
    facts = await participation_diagnostics(database, (), now=0)
    assert facts["runs"]["accepted_count"] == 0
    assert facts["runs"]["no_reply_per_accepted"] is None
    assert facts["runs"]["effect_runs_per_accepted"] is None
    assert facts["controller"]["provider"]["unknown_winning_ratio"] is None


async def test_truncated_feedback_does_not_publish_a_complete_effect_rate(
    database, tmp_path, monkeypatch
):
    host, item, _ = await _seed(database, tmp_path)
    try:
        monkeypatch.setattr("qq_ai_bot.services.participation_diagnostics._FEEDBACK_WINDOW", 1)
        facts = await participation_diagnostics(database, (item.controller.state,), now=0)
        assert facts["runs"]["feedback_complete"] is False
        assert facts["runs"]["feedback_pages_read"] == 1
        assert facts["runs"]["effect_runs_per_accepted"] is None
        assert facts["runs"]["effect_runs_known"] == 1
    finally:
        await host.close()
