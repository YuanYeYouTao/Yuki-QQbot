from __future__ import annotations

import subprocess
import tarfile
import zipfile
from hashlib import sha256
from pathlib import Path

import pytest
from scripts.build_release_bundle import (
    BundleBuildError,
    build_release_bundle,
    select_bundle_files,
    tracked_files,
)
from scripts.release_smoke import Compose, SmokeError, prepare_deployment, wait_healthy
from scripts.release_validate import (
    ReleaseValidationError,
    validate_release_identity,
    validate_tag_commit,
)

ROOT = Path(__file__).resolve().parents[2]
VERSION = "3.5.3"


def test_release_identity_matches_all_version_surfaces() -> None:
    assert validate_release_identity(ROOT, "v3.5.3") == VERSION


@pytest.mark.parametrize("tag", ["3.5.3", "v3.5", "v3.5.3-rc1", "v03.5.3", "latest"])
def test_release_identity_rejects_non_final_tags(tag: str) -> None:
    with pytest.raises(ReleaseValidationError, match=r"vX\.Y\.Z"):
        validate_release_identity(ROOT, tag)


def test_release_identity_rejects_mismatched_tag() -> None:
    with pytest.raises(ReleaseValidationError, match="do not match"):
        validate_release_identity(ROOT, "v3.5.2")


def test_tag_commit_must_be_reachable_from_main(tmp_path: Path) -> None:
    _git(tmp_path, "init", "--initial-branch=main")
    _git(tmp_path, "config", "user.email", "release-test@example.invalid")
    _git(tmp_path, "config", "user.name", "Release Test")
    (tmp_path / "state.txt").write_text("main\n", encoding="utf-8")
    _git(tmp_path, "add", "state.txt")
    _git(tmp_path, "commit", "-m", "main")
    _git(tmp_path, "switch", "-c", "side")
    (tmp_path / "state.txt").write_text("side\n", encoding="utf-8")
    _git(tmp_path, "commit", "-am", "side")
    _git(tmp_path, "tag", "v3.5.3")

    with pytest.raises(ReleaseValidationError, match="not reachable"):
        validate_tag_commit(tmp_path, "v3.5.3", "main")


def test_bundle_allowlist_requires_persona() -> None:
    tracked = {
        "docker-compose.yml",
        ".env.example",
        "docs/releases/v3.5.3.md",
        "install.sh",
        "install.ps1",
        "config/memory_contracts.toml",
        "config/memory_quality_gates.example.toml",
        "config/memory_quality_gates.toml",
        "config/model_profiles.example.toml",
        "config/qq_face_map.json",
        "config/system_prompt.example.md",
    }
    with pytest.raises(BundleBuildError, match=r"persona\.md"):
        select_bundle_files(tracked, VERSION)


def test_bundle_contains_only_deployment_files_and_expected_assets(tmp_path: Path) -> None:
    tracked = tracked_files(ROOT) | {"docs/releases/v3.5.3.md", "install.sh", "install.ps1"}
    assets = build_release_bundle(ROOT, tmp_path, VERSION, tracked=tracked)
    assert {path.name for path in assets} == {
        "yuki-3.5.3-deploy.zip",
        "yuki-3.5.3-deploy.tar.gz",
        "docker-compose.yml",
        ".env.example",
        "Yuki-3.5.3-Upgrade.md",
        "install.sh",
        "install.ps1",
        "SHA256SUMS",
    }
    with zipfile.ZipFile(tmp_path / "yuki-3.5.3-deploy.zip") as archive:
        names = set(archive.namelist())
        shell_mode = archive.getinfo("yuki-3.5.3-deploy/install.sh").external_attr >> 16
    prefix = "yuki-3.5.3-deploy/"
    assert f"{prefix}docker-compose.yml" in names
    assert f"{prefix}.env.example" in names
    assert f"{prefix}config/persona.md" in names
    assert f"{prefix}data/speech/japanese_frontend/lexicon.toml" in names
    assert f"{prefix}napcat-data/" in names
    assert f"{prefix}install.sh" in names
    assert f"{prefix}install.ps1" in names
    assert shell_mode & 0o111
    assert not any(name.startswith(f"{prefix}src/") for name in names)
    assert not any("/tests/" in name for name in names)
    assert not any(name.endswith(".pyc") or "__pycache__" in name for name in names)
    assert f"{prefix}.env" not in names
    assert f"{prefix}.mcp.json" not in names
    assert f"{prefix}config/system_prompt.md" not in names
    assert f"{prefix}config/model_profiles.toml" not in names
    with tarfile.open(tmp_path / "yuki-3.5.3-deploy.tar.gz", "r:gz") as archive:
        assert archive.getmember(f"{prefix}install.sh").mode & 0o111
    checksum_lines = (tmp_path / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    checksums = {
        name: digest for line in checksum_lines for digest, name in (line.split("  ", maxsplit=1),)
    }
    assert len(checksums) == 7
    for name, expected in checksums.items():
        assert sha256((tmp_path / name).read_bytes()).hexdigest() == expected


def test_production_and_development_compose_are_separated() -> None:
    production = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    development = (ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8")
    assert "build:" not in production
    assert "ghcr.io/yuanyeyoutao/yuki-qqbot:${YUKI_VERSION:?missing}" in production
    assert "ghcr.io/yuanyeyoutao/yuki-genie-tts-worker:${YUKI_VERSION:?missing}" in production
    assert production.count("platform: linux/amd64") == 2
    assert production.count("pull_policy: missing") == 2
    assert "./.mcp.json:/app/.mcp.json:ro" in production
    assert "image: yuki-qqbot:dev" in development
    assert "image: yuki-genie-tts-worker:dev" in development
    assert development.count("pull_policy: build") == 2
    assert development.count("build:") == 2


@pytest.mark.parametrize(
    "dockerfile", [ROOT / "Dockerfile", ROOT / "services/genie_tts_worker/Dockerfile"]
)
def test_oci_revision_label_does_not_invalidate_system_dependency_layers(
    dockerfile: Path,
) -> None:
    content = dockerfile.read_text(encoding="utf-8")

    assert content.index("RUN apt-get update") < content.index("ARG VCS_REF=unknown")
    assert content.index("ARG VCS_REF=unknown") < content.index(
        'org.opencontainers.image.revision="${VCS_REF}"'
    )


def test_release_smoke_uses_non_model_genie_import_sentinels(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("YUKI_VERSION=3.5.3\n", encoding="utf-8")

    sentinels = prepare_deployment(tmp_path)

    hubert_sentinel = tmp_path / "data/speech/genie_data/chinese-hubert-base/.release-smoke"
    speaker_sentinel = tmp_path / "data/speech/genie_data/speaker_encoder.onnx"
    assert sentinels[hubert_sentinel] == "offline-directory"
    assert sentinels[speaker_sentinel] == "offline-file-sentinel"
    assert hubert_sentinel.read_text(encoding="utf-8") == "offline-directory"
    assert speaker_sentinel.read_text(encoding="utf-8") == "offline-file-sentinel"
    assert (tmp_path / ".mcp.json").read_text(encoding="utf-8") == '{"mcpServers": {}}\n'


def test_release_smoke_sentinels_are_idempotent_and_conflict_safe(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("YUKI_VERSION=3.5.3\n", encoding="utf-8")
    sentinels = prepare_deployment(tmp_path)

    assert prepare_deployment(tmp_path) == sentinels

    conflicting = tmp_path / "data/.release-smoke-data"
    conflicting.write_text("unexpected", encoding="utf-8")
    with pytest.raises(SmokeError, match="unexpected content"):
        prepare_deployment(tmp_path)


def test_release_smoke_decodes_docker_output_as_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, stdout="配置通过本地严格验证\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    output = Compose(tmp_path, "test-project", VERSION).run("config", capture=True)

    assert output == "配置通过本地严格验证"
    assert observed["encoding"] == "utf-8"
    assert observed["errors"] == "replace"


def test_release_smoke_allows_transient_unhealthy_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose = Compose(tmp_path, "test-project", VERSION)
    monkeypatch.setattr(compose, "run", lambda *args, **kwargs: "container-id")
    statuses = iter(("unhealthy\n", "healthy\n"))

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args[0], 0, stdout=next(statuses))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("scripts.release_smoke.time.sleep", lambda _: None)

    assert wait_healthy(compose, "bot", timeout_seconds=1) == "container-id"


def test_release_workflow_has_bootstrap_quality_smoke_and_all_assets() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    quality = (ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8")
    assert "workflow_call:" in quality
    assert "force_docker: true" in workflow
    assert workflow.count(":bootstrap-amd64") == 2
    assert "finalize_version:" in workflow
    assert "Verify immutable public version images" in workflow
    assert "org.opencontainers.image.revision" in workflow
    assert "yuki-source-free-anonymous" in workflow
    assert workflow.count('YUKI_VERSION="$VERSION" docker compose --profile speech pull') == 2
    assert "--require-main-ancestor" in workflow
    assert "--platform linux/amd64" in workflow
    assert '--deploy-dir "$deploy_dir" --version "$VERSION" --full' in workflow
    assert workflow.index("Push immutable version tags") < workflow.index(
        "Verify anonymous version pulls"
    )
    assert workflow.index("Verify anonymous version pulls") < workflow.index(
        "Update latest only after public version verification"
    )
    for asset in (
        "yuki-$VERSION-deploy.zip",
        "yuki-$VERSION-deploy.tar.gz",
        "docker-compose.yml",
        ".env.example",
        "Yuki-$VERSION-Upgrade.md",
        "install.sh",
        "install.ps1",
        "SHA256SUMS",
    ):
        assert f'"dist/{asset}"' in workflow


def test_installers_are_fixed_orchestrators_without_a_docker_socket_mount() -> None:
    shell = (ROOT / "install.sh").read_text(encoding="utf-8")
    powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")
    for installer in (shell, powershell):
        assert "docker.sock" not in installer
        assert "SHA256SUMS" in installer
        assert "qq-ai-bot-cli" in installer
        assert "setup --deployment-root /deploy" in installer
        assert "docker compose" in installer
        assert "apply-pending" in installer
        assert "setup verify" in installer
    assert '--user "$(id -u):$(id -g)"' in shell
    assert "icacls" in powershell


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True, text=True)
