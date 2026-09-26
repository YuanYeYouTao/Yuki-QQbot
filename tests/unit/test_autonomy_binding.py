from qq_ai_bot.conversation.autonomy_binding import (
    AcceptedInitiative,
    AutonomyBinding,
    AutonomyOwner,
    InitiativeSource,
    InitiativeSourceKind,
)


def test_master_off_and_explicit_disable_win_over_provider_recovery() -> None:
    binding = AutonomyBinding("conversation", 2)
    binding = binding.transition(master_enabled=True, external_enabled=True)
    assert binding.effective_owner is AutonomyOwner.SEMANTIC
    epoch = binding.controller_epoch
    binding = binding.transition(master_enabled=True, external_enabled=False)
    assert binding.effective_owner is AutonomyOwner.LEGACY
    assert binding.controller_epoch == epoch + 1
    binding = binding.transition(master_enabled=False, external_enabled=True)
    assert binding.effective_owner is AutonomyOwner.OFF


def test_policy_change_fences_old_proposals_without_observer_readiness() -> None:
    binding = AutonomyBinding("conversation", 2).transition(
        master_enabled=True,
        external_enabled=True,
    )
    epoch = binding.controller_epoch
    fallback = binding.transition(
        master_enabled=True,
        external_enabled=False,
    )
    assert fallback.fallback_reason is None
    assert not fallback.accepts(
        owner=AutonomyOwner.SEMANTIC, epoch=epoch, conversation_id="conversation", generation=2
    )
    assert fallback.accepts(
        owner=AutonomyOwner.LEGACY,
        epoch=fallback.controller_epoch,
        conversation_id="conversation",
        generation=2,
    )
    assert not fallback.accepts(
        owner=AutonomyOwner.LEGACY,
        epoch=fallback.controller_epoch,
        conversation_id="conversation",
        generation=3,
    )


def test_old_provider_fallback_recovers_by_current_explicit_policy() -> None:
    old = AutonomyBinding(
        "conversation",
        2,
        master_enabled=True,
        external_enabled=True,
        effective_owner=AutonomyOwner.LEGACY,
        controller_epoch=4,
        fallback_reason="provider_unavailable",
    )
    current = old.transition(master_enabled=True, external_enabled=True)
    assert current.effective_owner is AutonomyOwner.SEMANTIC
    assert current.fallback_reason is None
    assert current.controller_epoch == 5
    assert current.transition(master_enabled=True, external_enabled=True) == current


def test_noop_does_not_advance_epoch_and_accepted_run_does_not_depend_on_it() -> None:
    binding = AutonomyBinding("conversation", 2).transition(
        master_enabled=True,
        external_enabled=True,
    )
    unchanged = binding.transition(master_enabled=True, external_enabled=True)
    assert unchanged == binding
    run = AcceptedInitiative(
        "run",
        "proposal",
        "conversation",
        2,
        "space",
        "presence",
        binding.controller_epoch,
        (InitiativeSource(InitiativeSourceKind.EVENT, "123", "v1"),),
        AutonomyOwner.SEMANTIC,
    )
    binding = binding.transition(master_enabled=True, external_enabled=False)
    assert run.belongs_to(binding.conversation_id, binding.generation)
    assert not run.belongs_to(binding.conversation_id, 3)
