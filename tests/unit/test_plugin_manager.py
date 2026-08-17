from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.plugin_host.discovery import PluginDiscovery
from qq_ai_bot.plugin_host.event_bus import PluginEventBus
from qq_ai_bot.plugin_host.extension_registry import ExtensionRegistry
from qq_ai_bot.plugin_host.loader import PluginLoader
from qq_ai_bot.plugin_host.manager import PluginManager
from qq_ai_bot.plugin_host.repository import (
    PluginApprovalError,
    PluginAuditRepository,
    PluginInstallationRepository,
)
from yuki_plugin_sdk.events import EventEnvelope, EventName
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.testing import FakePluginContext

_BASIC_PLUGIN = """class ExamplePlugin:
    async def register(self, registrar):
        return None

    async def start(self, context):
        self.context = context

    async def stop(self):
        return None
"""

_GOOD_PLUGIN = """import asyncio

from yuki_plugin_sdk.events import EventEnvelope, EventName
from yuki_plugin_sdk.models import RestartPolicy
from yuki_plugin_sdk.registrar import (
    BackgroundServiceMetadata,
    BackgroundServiceRegistration,
    EventHookMetadata,
    EventHookRegistration,
)

_events = None


class ExamplePlugin:
    async def register(self, registrar):
        async def observe(event):
            if _events is not None:
                await _events.publish(
                    EventEnvelope(
                        name=EventName.TURN_ADMITTED,
                        payload={"observed": event.name.value},
                    )
                )

        async def worker():
            await asyncio.Event().wait()

        registrar.register_event_hook(
            EventHookRegistration(
                EventHookMetadata(id="observe_reply", event=EventName.REPLY_SENT),
                observe,
            )
        )
        registrar.register_background_service(
            BackgroundServiceRegistration(
                BackgroundServiceMetadata(
                    name="worker",
                    restart_policy=RestartPolicy.NEVER,
                ),
                worker,
            )
        )

    async def start(self, context):
        global _events
        _events = context.events

    async def stop(self):
        return None
"""

_REGISTER_FAILURE = """class ExamplePlugin:
    async def register(self, registrar):
        raise ValueError("registration failed")

    async def start(self, context):
        return None

    async def stop(self):
        return None
"""

_START_FAILURE = """class ExamplePlugin:
    async def register(self, registrar):
        return None

    async def start(self, context):
        raise RuntimeError("start failed")

    async def stop(self):
        return None
"""

_START_TIMEOUT = """import asyncio


class ExamplePlugin:
    async def register(self, registrar):
        return None

    async def start(self, context):
        await asyncio.sleep(60)

    async def stop(self):
        return None
"""

_STOP_TIMEOUT = """import asyncio

from yuki_plugin_sdk.registrar import (
    BackgroundServiceMetadata,
    BackgroundServiceRegistration,
)


class ExamplePlugin:
    async def register(self, registrar):
        async def worker():
            await asyncio.Event().wait()

        registrar.register_background_service(
            BackgroundServiceRegistration(
                BackgroundServiceMetadata(name="worker"),
                worker,
            )
        )

    async def start(self, context):
        return None

    async def stop(self):
        await asyncio.sleep(60)
"""


class SpyDiscovery:
    def __init__(self) -> None:
        self.calls = 0

    def discover(self):
        self.calls += 1
        raise AssertionError("disabled PluginManager must not scan")


def _write_plugin(
    directory: Path,
    plugin_id: str,
    *,
    code: str = _BASIC_PLUGIN,
    version: str = "0.1.0",
    permissions: tuple[str, ...] = (),
    entrypoint: str = "plugin:ExamplePlugin",
    background_tasks: int = 1,
) -> Path:
    root = directory / plugin_id
    root.mkdir(parents=True, exist_ok=True)
    quoted_permissions = ", ".join(f'"{item}"' for item in permissions)
    (root / "plugin.toml").write_text(
        f'''id = "{plugin_id}"
name = "{plugin_id}"
version = "{version}"
description = "PluginManager test plugin"
entrypoint = "{entrypoint}"
plugin_api = "2.0"
yuki_requires = ">=1.6.0,<2.0"
permissions = [{quoted_permissions}]

[limits]
background_tasks = {background_tasks}
''',
        encoding="utf-8",
    )
    (root / "plugin.py").write_text(code, encoding="utf-8")
    return root


def _build_manager(
    database: Database,
    directory: Path,
    *,
    enabled: bool = True,
    discovery: SpyDiscovery | None = None,
    start_timeout: float = 0.05,
    stop_timeout: float = 0.05,
    context_factory: Callable[..., FakePluginContext] | None = None,
) -> tuple[
    PluginManager,
    PluginInstallationRepository,
    ExtensionRegistry,
    PluginEventBus,
    dict[str, FakePluginContext],
]:
    installations = PluginInstallationRepository(database)
    extensions = ExtensionRegistry()
    event_bus = PluginEventBus(default_timeout_seconds=0.05)
    contexts: dict[str, FakePluginContext] = {}

    def make_context(manifest, _permissions):
        if context_factory is not None:
            return context_factory(manifest, _permissions)
        return contexts.setdefault(manifest.id, FakePluginContext(manifest.id))

    manager = PluginManager(
        enabled=enabled,
        discovery=discovery or PluginDiscovery(directory, yuki_version="1.6.0", enabled=True),
        installations=installations,
        loader=PluginLoader(),
        extensions=extensions,
        event_bus=event_bus,
        context_factory=make_context,
        audit=PluginAuditRepository(database),
        start_timeout_seconds=start_timeout,
        stop_timeout_seconds=stop_timeout,
        background_task_limit=4,
        failure_disable_threshold=3,
    )
    return manager, installations, extensions, event_bus, contexts


async def _approve_and_enable(manager: PluginManager, plugin_ids: tuple[str, ...]) -> None:
    for plugin_id in plugin_ids:
        await manager.approve(plugin_id, actor_user_id="9000")
        await manager.enable(plugin_id, actor_user_id="9000")


async def test_disabled_manager_never_scans_external_directory(database: Database) -> None:
    spy = SpyDiscovery()
    manager, _, _, _, _ = _build_manager(
        database,
        Path("unused"),
        enabled=False,
        discovery=spy,
    )

    assert await manager.discover() == ()
    assert await manager.start() == ()
    assert spy.calls == 0
    report = await manager.doctor("com.example.missing")
    assert report.system_enabled is False
    assert "plugin_system_disabled" in report.problems


async def test_discovery_persists_pending_and_manifest_change_revokes_approval(
    database: Database,
    tmp_path: Path,
) -> None:
    plugin_id = "com.example.pending"
    _write_plugin(
        tmp_path,
        plugin_id,
        permissions=(PluginPermission.TOOL_REGISTER.value,),
    )
    manager, installations, _, _, _ = _build_manager(database, tmp_path)

    discovered = await manager.discover()
    assert len(discovered) == 1
    assert discovered[0].status == "pending_approval"
    assert discovered[0].enabled is False
    with pytest.raises(PluginApprovalError):
        await manager.approve(
            plugin_id,
            actor_user_id="9000",
            permissions=(PluginPermission.ONEBOT_MUTATE,),
        )

    approved = await manager.approve(plugin_id, actor_user_id="9000")
    assert approved.approved_permissions == (PluginPermission.TOOL_REGISTER.value,)
    enabled = await manager.enable(plugin_id, actor_user_id="9000")
    assert enabled.enabled is True

    _write_plugin(
        tmp_path,
        plugin_id,
        version="0.2.0",
        permissions=(
            PluginPermission.TOOL_REGISTER.value,
            PluginPermission.WEB_SEARCH.value,
        ),
    )
    changed = (await manager.discover())[0]
    assert changed.status == "pending_approval"
    assert changed.enabled is False
    assert changed.approved_permissions == ()
    assert changed.requested_permissions == (
        PluginPermission.TOOL_REGISTER.value,
        PluginPermission.WEB_SEARCH.value,
    )
    assert await manager.show(plugin_id) == await installations.get(plugin_id)
    assert [item.plugin_id for item in await manager.list()] == [plugin_id]
    doctor = await manager.doctor(plugin_id)
    assert doctor.manifest_hash_matches is True
    assert doctor.approval_valid is False
    assert "approval_missing_or_stale" in doctor.problems


async def test_lifecycle_failures_are_isolated_and_hooks_and_tasks_are_cleaned(
    database: Database,
    tmp_path: Path,
) -> None:
    good_id = "com.example.good"
    register_id = "com.example.register-fail"
    start_id = "com.example.start-fail"
    timeout_id = "com.example.start-timeout"
    load_id = "com.example.load-fail"
    _write_plugin(
        tmp_path,
        good_id,
        code=_GOOD_PLUGIN,
        permissions=(
            PluginPermission.EVENT_SUBSCRIBE.value,
            PluginPermission.BACKGROUND_WORKER.value,
        ),
    )
    _write_plugin(tmp_path, register_id, code=_REGISTER_FAILURE)
    _write_plugin(tmp_path, start_id, code=_START_FAILURE)
    _write_plugin(tmp_path, timeout_id, code=_START_TIMEOUT)
    _write_plugin(
        tmp_path,
        load_id,
        code="VALUE = 1\n",
        entrypoint="plugin:MissingPlugin",
    )
    manager, installations, extensions, event_bus, contexts = _build_manager(database, tmp_path)
    plugin_ids = (good_id, register_id, start_id, timeout_id, load_id)
    await manager.discover()
    await _approve_and_enable(manager, plugin_ids)

    running = await manager.start()

    assert running == (good_id,)
    assert manager.running_count == 1
    assert (await installations.get(register_id)).last_error_category == "ValueError"  # type: ignore[union-attr]
    assert (await installations.get(start_id)).last_error_category == "RuntimeError"  # type: ignore[union-attr]
    assert (await installations.get(timeout_id)).last_error_category == "start_timeout"  # type: ignore[union-attr]
    assert (await installations.get(load_id)).last_error_category == "PluginLifecycleError"  # type: ignore[union-attr]

    report = await manager.doctor(good_id)
    assert report.running is True
    assert report.extension_count == 2
    assert report.background_task_count == 1
    hook_results = await event_bus.publish(EventEnvelope(name=EventName.REPLY_SENT))
    assert len(hook_results) == 1
    assert hook_results[0].success is True
    assert contexts[good_id].events.events[-1].payload["observed"] == "reply.sent"

    disabled = await manager.disable(good_id, actor_user_id="9000")
    assert disabled.status == "disabled"
    assert manager.running_count == 0
    assert extensions.list(plugin_id=good_id) == ()
    assert await event_bus.publish(EventEnvelope(name=EventName.REPLY_SENT)) == ()
    cleaned = await manager.doctor(good_id)
    assert cleaned.background_task_count == 0
    await manager.stop()


async def test_stop_timeout_still_cancels_tasks_and_unloads_plugin(
    database: Database,
    tmp_path: Path,
) -> None:
    plugin_id = "com.example.stop-timeout"
    _write_plugin(
        tmp_path,
        plugin_id,
        code=_STOP_TIMEOUT,
        permissions=(PluginPermission.BACKGROUND_WORKER.value,),
    )
    manager, installations, extensions, _, _ = _build_manager(
        database,
        tmp_path,
        stop_timeout=0.01,
    )
    await manager.discover()
    await _approve_and_enable(manager, (plugin_id,))
    assert await manager.start() == (plugin_id,)
    assert (await manager.doctor(plugin_id)).background_task_count == 1

    disabled = await manager.disable(plugin_id, actor_user_id="9000")

    assert disabled.status == "disabled"
    assert disabled.last_error_category == "stop_timeout"
    assert manager.running_count == 0
    assert extensions.list(plugin_id=plugin_id) == ()
    stored = await installations.get(plugin_id)
    assert stored is not None and stored.enabled is False


async def test_cancelled_start_rolls_back_loaded_module_and_status(
    database: Database,
    tmp_path: Path,
) -> None:
    plugin_id = "com.example.cancel-start"
    _write_plugin(tmp_path, plugin_id, code=_START_TIMEOUT)
    manager, installations, extensions, _, _ = _build_manager(
        database,
        tmp_path,
        start_timeout=10,
    )
    await manager.discover()
    await _approve_and_enable(manager, (plugin_id,))

    starting = asyncio.create_task(manager.start())
    for _ in range(50):
        record = await installations.get(plugin_id)
        if record is not None and record.status == "starting":
            break
        await asyncio.sleep(0.01)
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting

    record = await installations.get(plugin_id)
    assert record is not None and record.status == "approved"
    assert manager.running_count == 0
    assert extensions.list(plugin_id=plugin_id) == ()


async def test_background_failure_isolated_from_other_running_plugin(
    database: Database,
    tmp_path: Path,
) -> None:
    failing_id = "com.example.background-fail"
    healthy_id = "com.example.healthy"
    failing_code = _GOOD_PLUGIN.replace(
        "await asyncio.Event().wait()",
        'raise RuntimeError("background failed")',
    )
    _write_plugin(
        tmp_path,
        failing_id,
        code=failing_code,
        permissions=(
            PluginPermission.EVENT_SUBSCRIBE.value,
            PluginPermission.BACKGROUND_WORKER.value,
        ),
    )
    _write_plugin(tmp_path, healthy_id)
    manager, installations, _, _, _ = _build_manager(database, tmp_path)
    await manager.discover()
    await _approve_and_enable(manager, (failing_id, healthy_id))
    await manager.start()

    for _ in range(20):
        if failing_id not in manager.running_plugin_ids:
            break
        await asyncio.sleep(0.01)

    assert manager.running_plugin_ids == (healthy_id,)
    failed = await installations.get(failing_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.last_error_category == "RuntimeError"
    await manager.stop()
