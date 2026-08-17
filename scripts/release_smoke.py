"""Run source-free production Compose smoke and persistence checks."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any


class SmokeError(RuntimeError):
    """Raised when a release image or deployment contract fails smoke testing."""


_MODEL_TASKS = (
    "chat_agent",
    "memory_extraction",
    "memory_self_reflection",
    "memory_consolidation",
    "memory_dream",
    "memory_attribution",
    "relationship_evaluation",
    "emoji_replacement",
    "automation_text_generation",
    "automation_agent",
    "plugin_agent_session",
    "utility_structured",
)
_SMOKE_PLUGIN_ID = "io.github.yuanyeyoutao.kun-game"


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
            encoding="utf-8",
            errors="replace",
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
        environment = (deploy_directory / ".env.example").read_text(encoding="utf-8")
        replacements = {
            "ONEBOT_ACCESS_TOKEN=replace-with-a-long-random-token": (
                "ONEBOT_ACCESS_TOKEN=release-smoke-onebot-token"
            ),
            "NAPCAT_WEBUI_TOKEN=replace-with-a-long-random-webui-token": (
                "NAPCAT_WEBUI_TOKEN=release-smoke-napcat-token"
            ),
            "SUPERUSERS=replace-with-superuser-qq": "SUPERUSERS=10000",
            "LLM_PROVIDER=openai": "LLM_PROVIDER=openai_compatible",
            "LLM_BASE_URL=https://replace-with-provider.example/v1": (
                "LLM_BASE_URL=https://models.example.invalid/v1"
            ),
            "LLM_API_KEY=replace-with-api-key": "LLM_API_KEY=release-smoke-key",
            "LLM_MODEL=replace-with-model-name": "LLM_MODEL=release-smoke-model",
        }
        for old, new in replacements.items():
            environment = environment.replace(old, new)
        env_file.write_text(environment, encoding="utf-8")
    if env_file.exists():
        _enable_plugin_system(env_file)
    mcp_file = deploy_directory / ".mcp.json"
    if not mcp_file.exists():
        mcp_file.write_text('{"mcpServers": {}}\n', encoding="utf-8")
    model_profiles = deploy_directory / "config/model_profiles.toml"
    if not model_profiles.exists():
        model_profiles.parent.mkdir(parents=True, exist_ok=True)
        routes = "\n".join(f'{task} = "main"' for task in _MODEL_TASKS)
        model_profiles.write_text(
            """schema_version = 3

[profiles.main]
provider = "openai_compatible"
protocol = "chat_completions"
base_url_env = "LLM_BASE_URL"
api_key_env = "LLM_API_KEY"
model_env = "LLM_MODEL"
timeout_seconds = 120.0
max_retries = 0
default_temperature = 0.0
default_max_output_tokens = 512
thinking_mode = "disabled"
structured_output_mode = "function_tool"
capabilities = ["tools", "structured_output", "long_context"]

[routes]
"""
            + routes
            + "\n",
            encoding="utf-8",
        )
    sentinels = {
        deploy_directory / "data/.release-smoke-data": "data",
        deploy_directory / "config/.release-smoke-config": "config",
        deploy_directory / "plugins/.release-smoke-plugin": "plugins",
        deploy_directory / "napcat-data/.release-smoke-login": "napcat-login",
        deploy_directory / "napcat-config/.release-smoke-config": "napcat-config",
        deploy_directory / "napcat-plugins/.release-smoke-plugin": "napcat-plugins",
        deploy_directory
        / "data/speech/genie_data/chinese-hubert-base/.release-smoke": "offline-directory",
        deploy_directory / "data/speech/genie_data/speaker_encoder.onnx": "offline-file-sentinel",
    }
    for path, value in sentinels.items():
        if path.exists():
            if path.read_text(encoding="utf-8") != value:
                raise SmokeError(f"existing release sentinel has unexpected content: {path}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    return sentinels


def _enable_plugin_system(env_file: Path) -> None:
    text = env_file.read_text(encoding="utf-8")
    if re.search(r"(?m)^PLUGIN_SYSTEM_ENABLED=", text):
        text = re.sub(r"(?m)^PLUGIN_SYSTEM_ENABLED=.*$", "PLUGIN_SYSTEM_ENABLED=true", text)
    else:
        text = text.rstrip() + "\nPLUGIN_SYSTEM_ENABLED=true\n"
    env_file.write_text(text, encoding="utf-8")


def discover_smoke_plugin_ids(deploy_directory: Path) -> tuple[str, ...]:
    root = deploy_directory / "plugins"
    if not root.is_dir():
        return ()
    ids = tuple(
        sorted(
            path.name
            for path in root.iterdir()
            if path.is_dir() and not path.name.startswith(".") and (path / "plugin.toml").is_file()
        )
    )
    if _SMOKE_PLUGIN_ID in ids:
        return (_SMOKE_PLUGIN_ID,)
    return ids[:1]


def write_plugin_pending(deploy_directory: Path) -> tuple[str, ...]:
    selected = discover_smoke_plugin_ids(deploy_directory)
    pending = deploy_directory / "data/setup/pending.json"
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text(
        json.dumps({"schema_version": 1, "selected_plugins": list(selected)}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    return selected


def _read_healthz(compose: Compose) -> dict[str, Any]:
    command = (
        "import json,urllib.request; "
        "print(json.dumps(json.load(urllib.request.urlopen("
        "'http://127.0.0.1:8080/healthz', timeout=3))))"
    )
    return json.loads(compose.run("exec", "-T", "bot", "python", "-c", command, capture=True))


def _assert_core_health(health: dict[str, Any], version: str) -> None:
    expected = {"status": "ok", "version": version, "database": "ok"}
    actual = {key: health.get(key) for key in expected}
    if actual != expected:
        raise SmokeError(f"unexpected /healthz response: {actual}")
    if health.get("plugin_system_enabled") is not True:
        raise SmokeError(f"plugin system is not enabled: {health}")


def wait_healthy(compose: Compose, service: str, timeout_seconds: float = 120.0) -> str:
    container_id = compose.run("ps", "--quiet", service, capture=True)
    if not container_id:
        raise SmokeError(f"{service} container was not created")
    deadline = time.monotonic() + timeout_seconds
    last_status = "unknown"
    while time.monotonic() < deadline:
        last_status = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", container_id],
            check=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        ).stdout.strip()
        if last_status == "healthy":
            return container_id
        time.sleep(2)
    raise SmokeError(
        f"{service} did not become healthy within {timeout_seconds:.0f}s "
        f"(last status: {last_status})"
    )


def verify_bot(compose: Compose, deploy_directory: Path, version: str) -> None:
    wait_healthy(compose, "bot")
    health = _read_healthz(compose)
    _assert_core_health(health, version)
    migration_command = (
        "import sqlite3; "
        "connection=sqlite3.connect('/app/data/qq_ai_bot.db'); "
        "row=connection.execute('SELECT version_num FROM alembic_version').fetchone(); "
        "connection.close(); print(row[0] if row else '')"
    )
    alembic_version = compose.run(
        "exec", "-T", "bot", "python", "-c", migration_command, capture=True
    )
    if alembic_version != "0040":
        raise SmokeError(f"unexpected Alembic version: {alembic_version!r}")
    compose.run("exec", "-T", "bot", "qq-ai-bot-cli", "plugin", "discover", capture=True)
    selected = write_plugin_pending(deploy_directory)
    compose.run(
        "exec",
        "-T",
        "bot",
        "qq-ai-bot-cli",
        "setup",
        "apply-pending",
        "--deployment-root",
        "/app",
        "--no-color",
        capture=True,
    )
    if not selected:
        return
    compose.run("up", "-d", "--no-deps", "--force-recreate", "bot")
    wait_healthy(compose, "bot")
    health = _read_healthz(compose)
    _assert_core_health(health, version)
    running = int(health.get("plugin_running_count") or 0)
    if running < 1:
        raise SmokeError(f"plugin did not start after apply-pending: {health}")


def verify_guided_setup(deploy_directory: Path, version: str) -> None:
    image = f"ghcr.io/yuanyeyoutao/yuki-qqbot:{version}"
    command = ["docker", "run", "--rm"]
    if os.name != "nt":
        get_uid = getattr(os, "getuid", None)
        get_gid = getattr(os, "getgid", None)
        if not callable(get_uid) or not callable(get_gid):
            raise SmokeError("POSIX user identity is unavailable")
        command.extend(("--user", f"{get_uid()}:{get_gid()}"))
    command.extend(
        (
            "--entrypoint",
            "qq-ai-bot-cli",
            "--volume",
            f"{deploy_directory.resolve()}:/deploy",
            "--workdir",
            "/deploy",
            image,
            "setup",
            "validate",
            "--deployment-root",
            "/deploy",
            "--no-color",
        )
    )
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if "配置通过本地严格验证" not in completed.stdout or "\033[" in completed.stdout:
        raise SmokeError("source-free Guided Setup validation failed")

    permission_script = (
        "from pathlib import Path; "
        "from qq_ai_bot.deployment_setup.service import _atomic_write; "
        "root=Path('/deploy'); "
        "profile=root/'config/model_profiles.toml'; "
        "mcp=root/'.mcp.json'; "
        "_atomic_write(profile, profile.read_bytes(), private=False); "
        "_atomic_write(mcp, mcp.read_bytes(), private=False); "
        "_atomic_write(root/'data/setup/pending.json', "
        'b\'{"schema_version":1,"selected_plugins":[]}\\n\', private=False)'
    )
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "python",
            "--volume",
            f"{deploy_directory.resolve()}:/deploy",
            image,
            "-c",
            permission_script,
        ],
        check=True,
    )
    read_script = (
        "from pathlib import Path; "
        "root=Path('/deploy'); "
        "assert (root/'config/model_profiles.toml').read_bytes(); "
        "assert (root/'.mcp.json').read_bytes(); "
        "assert (root/'data/setup/pending.json').read_bytes()"
    )
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "10001:10001",
            "--entrypoint",
            "python",
            "--volume",
            f"{deploy_directory.resolve()}:/deploy:ro",
            image,
            "-c",
            read_script,
        ],
        check=True,
    )
    cleanup_script = (
        "from pathlib import Path; "
        "(Path('/deploy')/'data/setup/pending.json').unlink(missing_ok=True)"
    )
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "python",
            "--volume",
            f"{deploy_directory.resolve()}:/deploy",
            image,
            "-c",
            cleanup_script,
        ],
        check=True,
    )


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
                encoding="utf-8",
                errors="replace",
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
        verify_guided_setup(deploy_directory, version)
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
