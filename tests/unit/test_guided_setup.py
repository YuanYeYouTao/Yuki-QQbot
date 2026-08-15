from __future__ import annotations

import io
import json
import os
import stat
import tomllib
from pathlib import Path

import pytest

import qq_ai_bot.cli as administrative_cli
import qq_ai_bot.deployment_setup.service as setup_service
from qq_ai_bot.config import Settings
from qq_ai_bot.deployment_setup.command import _configure
from qq_ai_bot.deployment_setup.service import (
    EnvironmentDocument,
    SetupConfiguration,
    SetupPaths,
    SetupValidationError,
    apply_pending_plugins,
    build_model_profiles,
    commit_configuration,
    discover_speech_profiles,
    missing_mcp_environment,
    sanitize_mcp_document,
    validate_configuration,
)
from qq_ai_bot.deployment_setup.terminal import TerminalUI
from qq_ai_bot.model_runtime import ModelTask
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.plugin_host.repository import PluginInstallationRepository


def _base_environment() -> dict[str, str]:
    return {
        "YUKI_VERSION": "3.5.3",
        "ONEBOT_ACCESS_TOKEN": "onebot-test-token",
        "NAPCAT_WEBUI_TOKEN": "napcat-test-token",
        "SUPERUSERS": "12345678",
        "LLM_PROVIDER": "openai_compatible",
        "LLM_BASE_URL": "https://models.example.invalid/v1",
        "LLM_API_KEY": "test-key",
        "LLM_MODEL": "test-model",
        "MODEL_PROFILES_FILE": "config/model_profiles.toml",
        "MCP_ENABLED": "false",
        "MCP_CONFIG_PATH": ".mcp.json",
        "MEMORY_EMBEDDING_ENABLED": "false",
        "VISION_ENABLED": "false",
        "PLUGIN_SYSTEM_ENABLED": "false",
        "AUTOMATION_ENABLED": "false",
        "SPEECH_ENABLED": "false",
        "WEB_MODE": "disabled",
    }


def _setup_paths(tmp_path: Path) -> SetupPaths:
    (tmp_path / "config").mkdir()
    (tmp_path / "config/persona.md").write_text("Yuki persona\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("# template\n", encoding="utf-8")
    return SetupPaths(tmp_path)


def _scripted_ui(
    answers: list[str],
    *,
    secrets: list[str] | None = None,
) -> tuple[TerminalUI, io.StringIO]:
    remaining_answers = iter(answers)
    remaining_secrets = iter(secrets or [])
    output = io.StringIO()
    return (
        TerminalUI(
            input_fn=lambda _prompt: next(remaining_answers),
            secret_fn=lambda _prompt: next(remaining_secrets),
            output=output,
            is_tty=False,
        ),
        output,
    )


def _copy_deployment_templates(root: Path) -> SetupPaths:
    repository = Path(__file__).resolve().parents[2]
    (root / "config").mkdir()
    (root / ".env.example").write_bytes((repository / ".env.example").read_bytes())
    (root / "config/persona.md").write_bytes((repository / "config/persona.md").read_bytes())
    return SetupPaths(root)


def test_terminal_colors_are_accessible_and_disable_automatically() -> None:
    colored_output = io.StringIO()
    colored = TerminalUI(output=colored_output, is_tty=True, environ={"TERM": "xterm"})
    colored.success("ready")
    colored.warning("cost")
    assert "\033[32m✓ ready\033[0m" in colored_output.getvalue()
    assert "\033[33m! cost\033[0m" in colored_output.getvalue()

    for environment in ({"NO_COLOR": "1"}, {"TERM": "dumb"}, {"CI": "true"}):
        plain_output = io.StringIO()
        plain = TerminalUI(output=plain_output, is_tty=True, environ=environment)
        plain.error("blocked")
        assert plain_output.getvalue() == "× blocked\n"
    explicit_output = io.StringIO()
    explicit = TerminalUI(
        output=explicit_output,
        is_tty=True,
        environ={"TERM": "xterm"},
        no_color=True,
    )
    explicit.success("plain")
    assert explicit_output.getvalue() == "✓ plain\n"


def test_setup_dispatches_before_full_settings_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_settings() -> None:
        raise AssertionError("Settings must not be constructed before setup dispatch")

    monkeypatch.setattr(administrative_cli, "Settings", forbidden_settings)
    monkeypatch.setattr(administrative_cli, "run_setup_command", lambda _args: 23)
    monkeypatch.setattr("sys.argv", ["qq-ai-bot-cli", "setup", "validate"])

    with pytest.raises(SystemExit) as caught:
        administrative_cli.main()
    assert caught.value.code == 23


def test_secret_input_is_never_written_to_terminal_output() -> None:
    output = io.StringIO()
    ui = TerminalUI(
        output=output,
        is_tty=False,
        secret_fn=lambda _prompt: "super-secret-value",
    )

    assert ui.ask_secret("API Key") == "super-secret-value"
    assert "super-secret-value" not in output.getvalue()


def test_first_run_generates_safe_all_disabled_configuration(tmp_path: Path) -> None:
    paths = _copy_deployment_templates(tmp_path)
    ui, output = _scripted_ui(
        [
            "12345678",
            "",
            "https://models.example.invalid/v1",
            "test-main-model",
            "n",
            "n",
            "",
            "n",
            "n",
            "n",
            "n",
            "n",
            "y",
        ],
        secrets=["test-main-key"],
    )

    assert _configure(paths, ui) == 0
    environment = EnvironmentDocument.load(paths).values()
    assert environment["YUKI_VERSION"] == "3.5.3"
    assert len(environment["ONEBOT_ACCESS_TOKEN"]) >= 43
    assert len(environment["NAPCAT_WEBUI_TOKEN"]) >= 43
    assert environment["ONEBOT_ACCESS_TOKEN"] != environment["NAPCAT_WEBUI_TOKEN"]
    assert environment["WEB_MODE"] == "disabled"
    assert environment["MEMORY_EMBEDDING_ENABLED"] == "false"
    assert environment["VISION_ENABLED"] == "false"
    assert environment["MCP_ENABLED"] == "false"
    assert environment["PLUGIN_SYSTEM_ENABLED"] == "false"
    assert environment["AUTOMATION_ENABLED"] == "false"
    assert environment["SPEECH_ENABLED"] == "false"
    assert environment["COMPOSE_PROFILES"] == ""
    assert "[profiles.flash]" not in paths.model_profiles.read_text(encoding="utf-8")
    assert set(
        tomllib.loads(paths.model_profiles.read_text(encoding="utf-8"))["routes"].values()
    ) == {"main"}
    assert json.loads(paths.mcp.read_text(encoding="utf-8")) == {"mcpServers": {}}
    assert not paths.pending.exists()
    assert "test-main-key" not in output.getvalue()
    assert "\033[" not in output.getvalue()


def test_cancelled_first_run_writes_nothing(tmp_path: Path) -> None:
    paths = _copy_deployment_templates(tmp_path)
    ui, _output = _scripted_ui(
        [
            "12345678",
            "",
            "https://models.example.invalid/v1",
            "test-main-model",
            "n",
            "n",
            "",
            "n",
            "n",
            "n",
            "n",
            "n",
            "n",
        ],
        secrets=["test-main-key"],
    )

    assert _configure(paths, ui) == 2
    assert not paths.env.exists()
    assert not paths.model_profiles.exists()
    assert not paths.mcp.exists()
    assert not paths.pending.exists()


def test_selective_rerun_preserves_unselected_files_and_unknown_env(tmp_path: Path) -> None:
    paths = _copy_deployment_templates(tmp_path)
    initial_ui, _output = _scripted_ui(
        [
            "12345678",
            "",
            "https://models.example.invalid/v1",
            "test-main-model",
            "n",
            "n",
            "",
            "n",
            "n",
            "n",
            "n",
            "n",
            "y",
        ],
        secrets=["test-main-key"],
    )
    assert _configure(paths, initial_ui) == 0
    paths.env.write_text(
        paths.env.read_text(encoding="utf-8") + "USER_CUSTOM_VALUE=preserve-me\n",
        encoding="utf-8",
    )
    profiles_before = paths.model_profiles.read_bytes()
    mcp_before = paths.mcp.read_bytes()

    rerun_ui, _output = _scripted_ui(["automation", "y", "", "y"])
    assert _configure(paths, rerun_ui) == 0

    environment = EnvironmentDocument.load(paths).values()
    assert environment["AUTOMATION_ENABLED"] == "true"
    assert environment["USER_CUSTOM_VALUE"] == "preserve-me"
    assert paths.model_profiles.read_bytes() == profiles_before
    assert paths.mcp.read_bytes() == mcp_before


def test_environment_document_preserves_comments_unknown_values_and_secrets() -> None:
    document = EnvironmentDocument(
        "# keep this comment\nUNKNOWN_SETTING=custom\n"
        "FEATURE_ENABLED=true  # keep inline\nAPI_KEY=old-secret\n"
    )

    rendered = document.merge(
        {
            **document.values(),
            "FEATURE_ENABLED": "false",
            "NEW_SETTING": "value with spaces",
        }
    )

    assert "# keep this comment" in rendered
    assert "UNKNOWN_SETTING=custom" in rendered
    assert "FEATURE_ENABLED=false  # keep inline" in rendered
    assert "API_KEY=old-secret" in rendered
    assert 'NEW_SETTING="value with spaces"' in rendered


def test_model_profiles_route_all_tasks_and_keep_agents_on_main() -> None:
    without_flash = tomllib.loads(
        build_model_profiles(main_protocol="chat_completions", flash_enabled=False)
    )
    assert set(without_flash["routes"]) == {task.value for task in ModelTask}
    assert set(without_flash["routes"].values()) == {"main"}
    assert without_flash["profiles"]["main"]["protocol"] == "chat_completions"

    with_flash = tomllib.loads(build_model_profiles(main_protocol="responses", flash_enabled=True))
    assert with_flash["profiles"]["main"]["provider"] == "deepseek"
    assert "native_web_search" in with_flash["profiles"]["main"]["capabilities"]
    assert with_flash["routes"]["planner"] == "flash"
    assert with_flash["routes"]["memory_attribution"] == "flash"
    assert with_flash["routes"]["relationship_evaluation"] == "flash"
    assert with_flash["routes"]["emoji_replacement"] == "flash"
    assert with_flash["routes"]["chat_agent"] == "main"
    assert with_flash["routes"]["automation_agent"] == "main"
    assert with_flash["routes"]["plugin_agent_session"] == "main"


def test_mcp_sanitization_extracts_sensitive_headers_and_rejects_stdio() -> None:
    environment: dict[str, str] = {}
    sanitized = sanitize_mcp_document(
        {
            "mcpServers": {
                "search": {
                    "url": "https://mcp.example.invalid/server",
                    "headers": {"Authorization": "Bearer private-value"},
                }
            }
        },
        environment,
    )

    reference = sanitized["mcpServers"]["search"]["headers"]["Authorization"]  # type: ignore[index]
    assert reference == "${MCP_SEARCH_AUTHORIZATION}"
    assert environment["MCP_SEARCH_AUTHORIZATION"] == "Bearer private-value"
    assert missing_mcp_environment(sanitized, environment) == ()
    assert "private-value" not in json.dumps(sanitized)

    with pytest.raises(SetupValidationError, match="stdio"):
        sanitize_mcp_document(
            {"mcpServers": {"local": {"command": "python", "args": ["server.py"]}}},
            {},
        )


def test_configuration_validation_is_offline_and_protocol_aware(tmp_path: Path) -> None:
    paths = _setup_paths(tmp_path)
    environment = _base_environment()
    configuration = SetupConfiguration(
        environment=environment,
        model_profiles=build_model_profiles(
            main_protocol="chat_completions",
            flash_enabled=False,
        ),
        mcp_document={"mcpServers": {}},
        pending_plugins=None,
    )

    settings = validate_configuration(paths, configuration)
    assert settings.llm_model == "test-model"
    assert settings.web.mode.value == "disabled"

    environment["WEB_MODE"] = "native"
    with pytest.raises(SetupValidationError, match="DeepSeek Responses"):
        validate_configuration(paths, configuration)


def test_configuration_validation_rejects_placeholder_and_missing_feature_key(
    tmp_path: Path,
) -> None:
    paths = _setup_paths(tmp_path)
    environment = _base_environment()
    environment["LLM_API_KEY"] = "replace-with-api-key"
    configuration = SetupConfiguration(
        environment=environment,
        model_profiles=build_model_profiles(
            main_protocol="chat_completions",
            flash_enabled=False,
        ),
        mcp_document={"mcpServers": {}},
        pending_plugins=None,
    )
    with pytest.raises(SetupValidationError, match="LLM_API_KEY"):
        validate_configuration(paths, configuration)

    environment["LLM_API_KEY"] = "test-key"
    environment["MEMORY_EMBEDDING_ENABLED"] = "true"
    environment["MEMORY_EMBEDDING_BASE_URL"] = "https://dashscope.aliyuncs.com/api/v1"
    environment["MEMORY_EMBEDDING_MODEL"] = "qwen3.7-text-embedding"
    environment["MEMORY_EMBEDDING_API_KEY"] = ""
    with pytest.raises(SetupValidationError, match="Embedding"):
        validate_configuration(paths, configuration)


@pytest.mark.parametrize(
    "updates",
    [
        {
            "MEMORY_EMBEDDING_ENABLED": "true",
            "MEMORY_EMBEDDING_BASE_URL": "https://dashscope.aliyuncs.com/api/v1",
            "MEMORY_EMBEDDING_API_KEY": "embedding-key",
            "MEMORY_EMBEDDING_MODEL": "qwen3.7-text-embedding",
        },
        {"WEB_MODE": "tavily", "TAVILY_API_KEY": "tavily-key"},
        {
            "VISION_ENABLED": "true",
            "VISION_BASE_URL": "https://vision.example.invalid/v1",
            "VISION_API_KEY": "vision-key",
            "VISION_MODEL": "vision-model",
        },
        {"AUTOMATION_ENABLED": "true", "DEFAULT_TIMEZONE": "Asia/Shanghai"},
        {"PLUGIN_SYSTEM_ENABLED": "true"},
    ],
)
def test_optional_feature_configurations_validate_without_network(
    tmp_path: Path,
    updates: dict[str, str],
) -> None:
    paths = _setup_paths(tmp_path)
    environment = {**_base_environment(), **updates}
    configuration = SetupConfiguration(
        environment=environment,
        model_profiles=build_model_profiles(
            main_protocol="chat_completions",
            flash_enabled=False,
        ),
        mcp_document={"mcpServers": {}},
        pending_plugins=None,
    )
    validate_configuration(paths, configuration)


def test_flash_native_web_and_http_mcp_validate_together(tmp_path: Path) -> None:
    paths = _setup_paths(tmp_path)
    environment = {
        **_base_environment(),
        "LLM_PROVIDER": "deepseek",
        "LLM_FLASH_BASE_URL": "https://flash.example.invalid/v1",
        "LLM_FLASH_API_KEY": "flash-key",
        "LLM_FLASH_MODEL": "flash-model",
        "WEB_MODE": "native",
        "MCP_ENABLED": "true",
        "MCP_SEARCH_AUTHORIZATION": "Bearer mcp-key",
    }
    configuration = SetupConfiguration(
        environment=environment,
        model_profiles=build_model_profiles(
            main_protocol="responses",
            flash_enabled=True,
        ),
        mcp_document={
            "mcpServers": {
                "search": {
                    "url": "https://mcp.example.invalid/server",
                    "headers": {"Authorization": "${MCP_SEARCH_AUTHORIZATION}"},
                }
            }
        },
        pending_plugins=None,
    )

    settings = validate_configuration(paths, configuration)
    assert settings.mcp_enabled
    assert settings.web.mode.value == "native"


def test_atomic_commit_backs_up_and_does_not_rewrite_unselected_files(tmp_path: Path) -> None:
    paths = _setup_paths(tmp_path)
    paths.env.write_text("# user comment\nUNKNOWN=preserve\nYUKI_VERSION=3.5.2\n", encoding="utf-8")
    paths.model_profiles.write_text("original model profile\n", encoding="utf-8")
    paths.mcp.write_text('{"mcpServers": {}}\n', encoding="utf-8")
    document = EnvironmentDocument.load(paths)

    backup = commit_configuration(
        paths,
        document,
        SetupConfiguration(
            environment={**document.values(), "YUKI_VERSION": "3.5.3"},
            model_profiles="ignored replacement\n",
            mcp_document={"mcpServers": {"ignored": {}}},
            pending_plugins=None,
            write_model_profiles=False,
            write_mcp=False,
        ),
    )

    assert backup is not None
    assert (backup / ".env").read_text(encoding="utf-8").endswith("YUKI_VERSION=3.5.2\n")
    assert "UNKNOWN=preserve" in paths.env.read_text(encoding="utf-8")
    assert paths.model_profiles.read_text(encoding="utf-8") == "original model profile\n"
    assert paths.mcp.read_text(encoding="utf-8") == '{"mcpServers": {}}\n'
    if os.name != "nt":
        assert stat.S_IMODE(paths.env.stat().st_mode) == 0o600


def test_backup_rotation_keeps_five_snapshots(tmp_path: Path) -> None:
    paths = _setup_paths(tmp_path)
    paths.env.write_text("VALUE=0\n", encoding="utf-8")
    paths.model_profiles.write_text("model\n", encoding="utf-8")
    paths.mcp.write_text('{"mcpServers": {}}\n', encoding="utf-8")
    for index in range(7):
        document = EnvironmentDocument.load(paths)
        commit_configuration(
            paths,
            document,
            SetupConfiguration(
                environment={**document.values(), "VALUE": str(index + 1)},
                model_profiles="model\n",
                mcp_document={"mcpServers": {}},
                pending_plugins=None,
            ),
        )

    assert len([item for item in paths.backups.iterdir() if item.is_dir()]) == 5


def test_atomic_commit_rolls_back_every_file_after_one_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _setup_paths(tmp_path)
    paths.env.write_text("VALUE=before\n", encoding="utf-8")
    paths.model_profiles.write_text("model-before\n", encoding="utf-8")
    paths.mcp.write_text('{"mcpServers": {"before": {}}}\n', encoding="utf-8")
    before = {
        paths.env: paths.env.read_bytes(),
        paths.model_profiles: paths.model_profiles.read_bytes(),
        paths.mcp: paths.mcp.read_bytes(),
    }
    original_write = setup_service._atomic_write
    calls = 0

    def fail_once(path: Path, content: bytes, *, private: bool) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic atomic write failure")
        original_write(path, content, private=private)

    monkeypatch.setattr(setup_service, "_atomic_write", fail_once)
    with pytest.raises(OSError, match="synthetic"):
        commit_configuration(
            paths,
            EnvironmentDocument.load(paths),
            SetupConfiguration(
                environment={"VALUE": "after"},
                model_profiles="model-after\n",
                mcp_document={"mcpServers": {}},
                pending_plugins=None,
            ),
        )

    assert {path: path.read_bytes() for path in before} == before


def test_speech_discovery_requires_a_complete_valid_profile(tmp_path: Path) -> None:
    speech = tmp_path / "speech"
    profile = speech / "voices/yuki"
    (profile / "model").mkdir(parents=True)
    (profile / "references").mkdir()
    (profile / "model/voice.onnx").write_bytes(b"model")
    (profile / "references/neutral.wav").write_bytes(b"audio")
    (profile / "profile.toml").write_text(
        """id = "yuki"
display_name = "Yuki"
provider = "genie"
engine_model_version = "v2proplus"
language = "zh"
supported_languages = ["zh"]
default_style = "neutral"
enabled = true
source = "user_supplied"
source_note = "local"
license_note = "deployment owner"

[model]
path = "model"

[[references]]
id = "neutral"
style = "neutral"
aliases = []
audio = "references/neutral.wav"
text = "你好。"
language = "zh"
enabled = true
priority = 1
""",
        encoding="utf-8",
    )

    assert [item.profile_id for item in discover_speech_profiles(speech)] == ["yuki"]
    (profile / "references/neutral.wav").unlink()
    assert discover_speech_profiles(speech) == ()


@pytest.mark.asyncio
async def test_pending_plugin_selection_never_auto_approves_and_is_idempotent(
    tmp_path: Path,
) -> None:
    paths = SetupPaths(tmp_path)
    plugin = tmp_path / "plugins/example.guided"
    plugin.mkdir(parents=True)
    (plugin / "plugin.toml").write_text(
        """id = "example.guided"
name = "Guided Test"
version = "1.0.0"
description = "Manifest-only guided setup test."
entrypoint = "plugin:Plugin"
plugin_api = "1.1"
yuki_requires = ">=3.5.3,<4"
permissions = ["message.current.read"]
""",
        encoding="utf-8",
    )
    database_path = tmp_path / "data/test.db"
    database_path.parent.mkdir(parents=True)
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    database = Database(database_url)
    await database.create_schema()
    await database.close()
    settings = Settings.model_validate(
        {
            "database_url": database_url,
            "plugin_directory": plugin.parent,
            "llm_provider": "fake",
            "llm_model": "fake",
        }
    )

    paths.pending.parent.mkdir(parents=True)
    paths.pending.write_text(
        json.dumps({"schema_version": 1, "selected_plugins": []}),
        encoding="utf-8",
    )
    assert await apply_pending_plugins(paths, settings) == 0

    inspection_database = Database(database_url)
    repository = PluginInstallationRepository(inspection_database)
    pending = await repository.get("example.guided")
    assert pending is not None
    assert not pending.enabled
    assert pending.approved_at is None

    paths.pending.write_text(
        json.dumps({"schema_version": 1, "selected_plugins": ["example.guided"]}),
        encoding="utf-8",
    )
    assert await apply_pending_plugins(paths, settings) == 1
    approved = await repository.get("example.guided")
    assert approved is not None and approved.enabled
    assert approved.approved_permissions == ("message.current.read",)

    paths.pending.write_text(
        json.dumps({"schema_version": 1, "selected_plugins": ["example.guided"]}),
        encoding="utf-8",
    )
    assert await apply_pending_plugins(paths, settings) == 0
    await inspection_database.close()
