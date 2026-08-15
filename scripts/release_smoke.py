"""Run source-free production Compose smoke and persistence checks."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any


class SmokeError(RuntimeError):
    """Raised when a release image or deployment contract fails smoke testing."""


class Compose:
    def __init__(self, deploy_directory: Path, project: str, version: str) -> None:
        self.deploy_directory = deploy_directory
        self.environment = os.environ.copy()
        self.environment["YUKI_VERSION"] = version
        self.command = ["docker", "compose", "--project-name", project]

    def run(self, *arguments: str, capture: bool = False) -> str:
        completed = subprocess.run(
            [*self.command, *arguments],
            cwd=self.deploy_directory,
            env=self.environment,
            check=True,
            capture_output=capture,
            text=True,
        )
        return completed.stdout.strip() if capture else ""


def validate_production_compose(deploy_directory: Path, version: str, compose: Compose) -> None:
    raw = (deploy_directory / "docker-compose.yml").read_text(encoding="utf-8")
    if "build:" in raw:
        raise SmokeError("production Compose must not contain build")
    rendered = json.loads(
        compose.run("--profile", "speech", "config", "--format", "json", capture=True)
    )
    services: dict[str, dict[str, Any]] = rendered["services"]
    expected = {
        "bot": f"ghcr.io/yuanyeyoutao/yuki-qqbot:{version}",
        "genie-tts-worker": f"ghcr.io/yuanyeyoutao/yuki-genie-tts-worker:{version}",
    }
    for service, image in expected.items():
        if services[service]["image"] != image:
            raise SmokeError(
                f"{service} resolved to {services[service]['image']}, expected {image}"
            )
        if services[service].get("platform") != "linux/amd64":
            raise SmokeError(f"{service} does not resolve to linux/amd64")
    required_mounts = {
        "bot": {"/app/data", "/app/config", "/app/plugins", "/app/napcat-config"},
        "genie-tts-worker": {
            "/data/speech/genie_data",
            "/data/speech/voices",
            "/data/speech/cache",
            "/data/speech/japanese_frontend",
            "/run/yuki-speech",
        },
        "napcat": {"/app/.config/QQ", "/app/napcat/config", "/app/napcat/plugins"},
    }
    for service, destinations in required_mounts.items():
        actual = {mount["target"] for mount in services[service]["volumes"]}
        if not destinations <= actual:
            raise SmokeError(f"{service} is missing persistent mounts: {destinations - actual}")


def prepare_deployment(deploy_directory: Path) -> dict[Path, str]:
    env_file = deploy_directory / ".env"
    if not env_file.exists():
        shutil.copyfile(deploy_directory / ".env.example", env_file)
    sentinels = {
        deploy_directory / "data/.release-smoke-data": "data",
        deploy_directory / "config/.release-smoke-config": "config",
        deploy_directory / "plugins/.release-smoke-plugin": "plugins",
        deploy_directory / "napcat-data/.release-smoke-login": "napcat-login",
        deploy_directory / "napcat-config/.release-smoke-config": "napcat-config",
        deploy_directory / "napcat-plugins/.release-smoke-plugin": "napcat-plugins",
        deploy_directory / "data/speech/genie_data/.release-smoke": "offline-sentinel",
    }
    for path, value in sentinels.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    return sentinels


def wait_healthy(compose: Compose, service: str, timeout_seconds: float = 120.0) -> str:
    container_id = compose.run("ps", "--quiet", service, capture=True)
    if not container_id:
        raise SmokeError(f"{service} container was not created")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", container_id],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if status == "healthy":
            return container_id
        if status == "unhealthy":
            raise SmokeError(f"{service} became unhealthy")
        time.sleep(2)
    raise SmokeError(f"{service} did not become healthy within {timeout_seconds:.0f}s")


def verify_bot(compose: Compose, deploy_directory: Path, version: str) -> None:
    wait_healthy(compose, "bot")
    command = (
        "import json,urllib.request; "
        "print(json.dumps(json.load(urllib.request.urlopen("
        "'http://127.0.0.1:8080/healthz', timeout=3))))"
    )
    health = json.loads(compose.run("exec", "-T", "bot", "python", "-c", command, capture=True))
    expected = {"status": "ok", "version": version, "database": "ok"}
    actual = {key: health.get(key) for key in expected}
    if actual != expected:
        raise SmokeError(f"unexpected /healthz response: {actual}")
    database = deploy_directory / "data/qq_ai_bot.db"
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    if row != ("0036",):
        raise SmokeError(f"unexpected Alembic version: {row}")


def verify_persistence(
    compose: Compose, deploy_directory: Path, sentinels: dict[Path, str]
) -> None:
    database = deploy_directory / "data/qq_ai_bot.db"
    database_size = database.stat().st_size
    compose.run("up", "-d", "--no-deps", "--force-recreate", "bot")
    wait_healthy(compose, "bot")
    compose.run(
        "--profile", "speech", "up", "-d", "--no-deps", "--force-recreate", "genie-tts-worker"
    )
    wait_healthy(compose, "genie-tts-worker")
    if not database.exists() or database.stat().st_size < database_size:
        raise SmokeError("database did not survive container recreation")
    for path, value in sentinels.items():
        if path.read_text(encoding="utf-8") != value:
            raise SmokeError(f"persistent sentinel did not survive recreation: {path}")


def verify_napcat_mount_recreation(compose: Compose, deploy_directory: Path) -> None:
    compose.run("pull", "napcat")
    for attempt in range(2):
        compose.run("create", "--pull", "never", "napcat")
        container_id = compose.run("ps", "--all", "--quiet", "napcat", capture=True)
        mounts = json.loads(
            subprocess.run(
                ["docker", "inspect", "--format", "{{json .Mounts}}", container_id],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        login_mount = next(
            (mount for mount in mounts if mount["Destination"] == "/app/.config/QQ"), None
        )
        if login_mount is None:
            raise SmokeError("NapCat login directory is not mounted")
        if Path(login_mount["Source"]).resolve() != (deploy_directory / "napcat-data").resolve():
            raise SmokeError("NapCat login mount points outside the deployment directory")
        if attempt == 0:
            compose.run("rm", "-s", "-f", "napcat")
    sentinel = deploy_directory / "napcat-data/.release-smoke-login"
    if sentinel.read_text(encoding="utf-8") != "napcat-login":
        raise SmokeError("NapCat login sentinel did not survive container recreation")


def run_smoke(deploy_directory: Path, version: str, *, full: bool) -> None:
    if (deploy_directory / "src").exists() or (deploy_directory / "pyproject.toml").exists():
        raise SmokeError("deployment smoke directory contains project source")
    compose = Compose(deploy_directory, f"yuki-release-smoke-{os.getpid()}", version)
    sentinels = prepare_deployment(deploy_directory)
    try:
        validate_production_compose(deploy_directory, version, compose)
        compose.run("up", "-d", "--no-deps", "bot")
        verify_bot(compose, deploy_directory, version)
        if full:
            compose.run("--profile", "speech", "up", "-d", "--no-deps", "genie-tts-worker")
            wait_healthy(compose, "genie-tts-worker")
            verify_persistence(compose, deploy_directory, sentinels)
            verify_napcat_mount_recreation(compose, deploy_directory)
    finally:
        compose.run("--profile", "speech", "down", "--volumes", "--remove-orphans")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deploy-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    run_smoke(args.deploy_dir.resolve(), args.version, full=args.full)
    print(f"source-free smoke passed for Yuki {args.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
