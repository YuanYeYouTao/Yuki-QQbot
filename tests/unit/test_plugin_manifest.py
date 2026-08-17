from __future__ import annotations

from pathlib import Path

import pytest

from qq_ai_bot.plugin_host.approval import InMemoryApprovalStore, PluginApprovalService
from qq_ai_bot.plugin_host.discovery import PluginDiscovery
from qq_ai_bot.plugin_host.manifest import PluginManifest, load_manifest
from qq_ai_bot.plugin_host.models import PluginStatus
from yuki_plugin_sdk.errors import ManifestValidationError
from yuki_plugin_sdk.permissions import PluginPermission


def _manifest_text(plugin_id: str = "com.example.echo") -> str:
    return f'''id = "{plugin_id}"
name = "Echo"
version = "0.1.0"
description = "Echo test plugin"
entrypoint = "echo_plugin:EchoPlugin"
plugin_api = "2.0"
yuki_requires = ">=1.6.0,<2.0"
permissions = ["tool.register", "network.http.allowlisted"]

[network]
allowed_hosts = ["api.example.com"]

[limits]
background_tasks = 2
http_concurrency = 4
storage_mb = 50
prompt_characters = 2000
'''


def _plugin_dir(tmp_path: Path, plugin_id: str = "com.example.echo") -> Path:
    root = tmp_path / plugin_id
    root.mkdir()
    (root / "plugin.toml").write_text(_manifest_text(plugin_id), encoding="utf-8")
    return root


def test_manifest_rejects_plugin_api_1x(tmp_path: Path) -> None:
    root = _plugin_dir(tmp_path)
    text = (root / "plugin.toml").read_text(encoding="utf-8")
    (root / "plugin.toml").write_text(
        text.replace('plugin_api = "2.0"', 'plugin_api = "1.1"'),
        encoding="utf-8",
    )

    with pytest.raises(ManifestValidationError, match="incompatible"):
        load_manifest(root, yuki_version="1.6.0")


def test_manifest_is_strict_compatible_and_hash_stable(tmp_path: Path) -> None:
    root = _plugin_dir(tmp_path)
    manifest = load_manifest(root, yuki_version="1.6.0")

    assert manifest.id == "com.example.echo"
    assert manifest.permissions == (
        PluginPermission.TOOL_REGISTER,
        PluginPermission.NETWORK_HTTP_ALLOWLISTED,
    )
    assert manifest.network.allowed_hosts == ("api.example.com",)
    assert len(manifest.manifest_hash) == 64
    assert (
        manifest.manifest_hash
        == PluginManifest.model_validate(manifest.model_dump(mode="json")).manifest_hash
    )


@pytest.mark.parametrize(
    "plugin_id",
    ["Yuki.bad", "yuki.bad", "yuki-tools", "core.test", "qq-ai-bot.fake", "bad_name"],
)
def test_manifest_rejects_invalid_or_reserved_ids(tmp_path: Path, plugin_id: str) -> None:
    root = tmp_path / plugin_id
    root.mkdir()
    (root / "plugin.toml").write_text(_manifest_text(plugin_id), encoding="utf-8")

    with pytest.raises(ManifestValidationError):
        load_manifest(root, yuki_version="1.6.0")


def test_manifest_rejects_private_or_url_hosts(tmp_path: Path) -> None:
    root = _plugin_dir(tmp_path)
    manifest_path = root / "plugin.toml"
    text = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(text.replace("api.example.com", "http://127.0.0.1"), encoding="utf-8")

    with pytest.raises(ManifestValidationError):
        load_manifest(root, yuki_version="1.6.0")


def test_manifest_checks_yuki_version_and_directory(tmp_path: Path) -> None:
    root = _plugin_dir(tmp_path)
    with pytest.raises(ManifestValidationError):
        load_manifest(root, yuki_version="2.0.0")

    renamed = tmp_path / "wrong-directory"
    root.rename(renamed)
    with pytest.raises(ManifestValidationError):
        load_manifest(renamed, yuki_version="1.6.0")


async def test_approval_is_bound_to_manifest_hash(tmp_path: Path) -> None:
    manifest = load_manifest(_plugin_dir(tmp_path), yuki_version="1.6.0")
    service = PluginApprovalService(InMemoryApprovalStore())
    record = await service.approve(
        manifest,
        approved_by="10001",
        permissions=(PluginPermission.TOOL_REGISTER,),
    )
    assert await service.valid_approval(manifest) == record

    changed = manifest.model_copy(update={"version": "0.2.0"})
    assert await service.valid_approval(changed) is None
    with pytest.raises(ValueError):
        await service.approve(
            manifest,
            approved_by="10001",
            permissions=(PluginPermission.ONEBOT_MUTATE,),
        )


def test_discovery_does_not_scan_when_disabled(tmp_path: Path) -> None:
    _plugin_dir(tmp_path)
    assert PluginDiscovery(tmp_path, yuki_version="1.6.0", enabled=False).discover() == ()


def test_discovery_isolates_invalid_plugin(tmp_path: Path) -> None:
    valid = _plugin_dir(tmp_path)
    invalid = tmp_path / "com.example.invalid"
    invalid.mkdir()
    (invalid / "plugin.toml").write_text("id = [", encoding="utf-8")

    records = PluginDiscovery(tmp_path, yuki_version="1.6.0").discover()
    statuses = {item.record.directory.name: item.record.status for item in records}
    assert statuses[valid.name] is PluginStatus.DISCOVERED
    assert statuses[invalid.name] is PluginStatus.INVALID
