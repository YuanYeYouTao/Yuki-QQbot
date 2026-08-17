"""Authoritative turn domain for the Yuki conversation runtime (R1).

This package owns the neutral, planner-free vocabulary shared by the
conversation / memory / capability runtimes introduced by the 3.6.0
refactor:

- ``origin``: :class:`~qq_ai_bot.runtime.origin.TurnOrigin` (moved here from
  ``automation.models``, which keeps a compatibility re-export).
- ``trigger``: the four-way ``TurnTrigger`` discriminated union.
- ``keys``: strongly typed coordination / memory partition keys.
- ``authority``: host-built ``TurnAuthority`` plus the neutral delegated
  authority snapshot and its pure revalidation function.
- ``turn``: ``TurnContext`` / ``TurnState`` and untrusted-content wrappers.
- ``result`` / ``delivery``: turn outcome and delivery accounting.
- ``invariants``: the turn phase machine with its legal transitions.
- ``observability``: ambient ``runtime_turn_id`` correlation plus the
  content-free observation row contract.
- ``contracts``: cross-domain pure data (memory capability view, tool batch
  results, delivery summary, capability exposure snapshot).

Import rules (enforced by ``tests/unit/test_runtime_dependency_boundaries``):
this package must never import ``planner`` (not even under ``TYPE_CHECKING``)
nor runtime-import ``conversation`` / ``capabilities`` / ``memory``
implementation / ``plugin_host`` / ``application`` / ``services``; the single
allowed exception is annotation-only (``TYPE_CHECKING``) references to
coordinator/admin/vision/time value types.  Consumers must import from the
submodules directly; this ``__init__`` intentionally re-exports nothing to
keep import cycles impossible.
"""
