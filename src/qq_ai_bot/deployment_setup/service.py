"""Configuration, validation, persistence, and post-start setup services."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_origin
from urllib.parse import urlsplit

import httpx
from pydantic import AliasChoices, AliasPath, ValidationError

from qq_ai_bot import __version__
from qq_ai_bot.config import Settings
from qq_ai_bot.mcp.config import MCPConfigurationError, load_mcp_config
from qq_ai_bot.mcp.models import MCPConfigFile
from qq_ai_bot.model_runtime import ModelCapability, ModelTask, load_model_profile_catalog
from qq_ai_bot.model_runtime.profiles import ModelRuntimeConfigurationError
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.plugin_host.discovery import PluginDiscovery
from qq_ai_bot.plugin_host.repository import PluginInstallationRepository
from qq_ai_bot.speech.paths import SpeechPathPolicy
from qq_ai_bot.speech.profiles import VoiceProfileService
from qq_ai_bot.speech.repository import VoiceProfileRepository
from qq_ai_bot.web.models import WebMode

_ENV_LINE = re.compile(r"^(?P<prefix>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)=")
_SAFE_ENV_VALUE = re.compile(r"^[A-Za-z0-9_./:@+,-]*$")
_ENV_REFERENCE = re.compile(r"\$\{([A-Z][A-Z0-9_]{0,63})\}")
_SECRET_HEADER_TOKENS = ("authorization", "cookie", "token", "api-key", "api_key", "secret")
_FLASH_TASKS = frozenset(
    {
        ModelTask.PLANNER,
        ModelTask.MEMORY_EXTRACTION,
        ModelTask.MEMORY_SELF_REFLECTION,
        ModelTask.MEMORY_CONSOLIDATION,
        ModelTask.MEMORY_DREAM,
        ModelTask.MEMORY_ATTRIBUTION,
        ModelTask.RELATIONSHIP_EVALUATION,
        ModelTask.EMOJI_REPLACEMENT,
        ModelTask.AUTOMATION_TEXT_GENERATION,
        ModelTask.TOOL_SELECTION,
        ModelTask.UTILITY_STRUCTURED,
    }
)


class SetupValidationError(ValueError):
    """A user-facing setup configuration error with no secret material."""


@dataclass(frozen=True, slots=True)
class SetupPaths:
    root: Path

    @property
    def env_example(self) -> Path:
        return self.root / ".env.example"

    @property
    def env(self) -> Path:
        return self.root / ".env"

    @property
    def model_profiles(self) -> Path:
        return self.root / "config/model_profiles.toml"

    @property
    def mcp(self) -> Path:
        return self.root / ".mcp.json"

    @property
    def pending(self) -> Path:
        return self.root / "data/setup/pending.json"

    @property
    def backups(self) -> Path:
        return self.root / ".yuki/backups"


@dataclass(frozen=True, slots=True)
class SetupConfiguration:
    environment: dict[str, str]
    model_profiles: str
    mcp_document: dict[str, object]
    pending_plugins: tuple[str, ...] | None
    write_model_profiles: bool = True
    write_mcp: bool = True


@dataclass(frozen=True, slots=True)
class SpeechProfileCandidate:
    profile_id: str
    display_name: str


class EnvironmentDocument:
    """Line-preserving .env reader and managed-value merger."""

    def __init__(self, text: str) -> None:
        self._lines = text.splitlines()

    @classmethod
    def load(cls, paths: SetupPaths) -> EnvironmentDocument:
        source = paths.env if paths.env.is_file() else paths.env_example
        if not source.is_file():
            raise SetupValidationError("部署目录缺少 .env.example")
        try:
            return cls(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise SetupValidationError("无法读取环境变量模板") from exc

    def values(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in self._lines:
            match = _ENV_LINE.match(line)
            if match is None:
                continue
            raw = line[match.end() :]
            values[match.group("key").upper()] = _decode_env_value(raw)
        return values

    def merge(self, updates: Mapping[str, str]) -> str:
        normalized = {key.upper(): value for key, value in updates.items()}
        written: set[str] = set()
        output: list[str] = []
        for line in self._lines:
            match = _ENV_LINE.match(line)
            if match is None:
                output.append(line)
                continue
            key = match.group("key").upper()
            if key not in normalized:
                output.append(line)
                continue
            if key in written:
                continue
            _raw_value, comment = _split_env_comment(line[match.end() :])
            output.append(
                f"{match.group('prefix')}{match.group('key')}="
                f"{_encode_env_value(normalized[key])}{comment}"
            )
            written.add(key)
        missing = [key for key in normalized if key not in written]
        if missing and output and output[-1]:
            output.append("")
        output.extend(f"{key}={_encode_env_value(normalized[key])}" for key in missing)
        return "\n".join(output).rstrip() + "\n"


def build_model_profiles(*, main_protocol: str, flash_enabled: bool) -> str:
    if main_protocol not in {"chat_completions", "responses"}:
        raise SetupValidationError("主模型协议无效")
    provider = "deepseek" if main_protocol == "responses" else "openai_compatible"
    main_capabilities = ["tools", "structured_output", "long_context"]
    if main_protocol == "responses":
        main_capabilities.extend(("reasoning", "native_web_search"))
    lines = [
        "schema_version = 2",
        "",
        "# Generated by qq-ai-bot-cli setup. Secrets remain in .env.",
        "[profiles.main]",
        f'provider = "{provider}"',
        f'protocol = "{main_protocol}"',
        'base_url_env = "LLM_BASE_URL"',
        'api_key_env = "LLM_API_KEY"',
        'model_env = "LLM_MODEL"',
        "timeout_seconds = 120.0",
        "max_retries = 2",
        "default_temperature = 0.7",
        "default_max_output_tokens = 8192",
        f'thinking_mode = "{"configurable" if main_protocol == "responses" else "disabled"}"',
        'structured_output_mode = "function_tool"',
        f"capabilities = {json.dumps(main_capabilities)}",
    ]
    if main_protocol == "responses":
        lines.insert(
            lines.index('structured_output_mode = "function_tool"'),
            'reasoning_effort_env = "LLM_REASONING_EFFORT"',
        )
    if flash_enabled:
        lines.extend(
            (
                "",
                "[profiles.flash]",
                'provider = "openai_compatible"',
                'protocol = "chat_completions"',
                'base_url_env = "LLM_FLASH_BASE_URL"',
                'api_key_env = "LLM_FLASH_API_KEY"',
                'model_env = "LLM_FLASH_MODEL"',
                "timeout_seconds = 30.0",
                "max_retries = 1",
                "default_temperature = 0.1",
                "default_max_output_tokens = 2048",
                'thinking_mode = "disabled"',
                'structured_output_mode = "function_tool"',
                'capabilities = ["structured_output"]',
            )
        )
    lines.extend(("", "[routes]"))
    for task in ModelTask:
        profile = "flash" if flash_enabled and task in _FLASH_TASKS else "main"
        lines.append(f'{task.value} = "{profile}"')
    return "\n".join(lines) + "\n"


def infer_main_protocol(profile_path: Path, environment: Mapping[str, str]) -> str:
    if profile_path.is_file():
        try:
            import tomllib

            payload = tomllib.loads(profile_path.read_text(encoding="utf-8"))
            profiles = payload.get("profiles", {})
            if isinstance(profiles, dict):
                main = profiles.get("main", profiles.get("pro", {}))
                if isinstance(main, dict) and main.get("protocol") == "responses":
                    return "responses"
        except (OSError, UnicodeError, ValueError):
            pass
    if environment.get("LLM_PROVIDER", "").casefold() == "deepseek":
        return "responses"
    return "chat_completions"


def model_profiles_use_flash(profile_path: Path) -> bool:
    if not profile_path.is_file():
        return False
    try:
        return "[profiles.flash]" in profile_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False


def sanitize_mcp_document(
    document: object,
    environment: dict[str, str],
) -> dict[str, object]:
    if not isinstance(document, dict):
        raise SetupValidationError("MCP 配置根节点必须是对象")
    raw_servers = document.get("mcpServers")
    if not isinstance(raw_servers, dict) or not raw_servers:
        raise SetupValidationError("MCP 开启时至少需要一个 Server")
    sanitized_servers: dict[str, object] = {}
    generated_secret_origins: dict[str, tuple[str, str]] = {}
    enabled_count = 0
    for raw_id, raw_server in raw_servers.items():
        server_id = str(raw_id).strip()
        if not server_id or not isinstance(raw_server, dict):
            raise SetupValidationError("MCP Server ID 或配置无效")
        server = dict(raw_server)
        if server.get("command") is not None:
            raise SetupValidationError("Docker 引导版不支持 stdio MCP，请使用 Streamable HTTP")
        url = server.get("url")
        if not isinstance(url, str) or not url.casefold().startswith(("http://", "https://")):
            raise SetupValidationError(f"MCP Server {server_id} 缺少有效 HTTP URL")
        headers = server.get("headers", {})
        if not isinstance(headers, dict):
            raise SetupValidationError(f"MCP Server {server_id} headers 必须是对象")
        safe_headers: dict[str, str] = {}
        for raw_name, raw_value in headers.items():
            name = str(raw_name).strip()
            value = str(raw_value)
            if any(token in name.casefold() for token in _SECRET_HEADER_TOKENS):
                references = _ENV_REFERENCE.findall(value)
                if not references:
                    env_name = _mcp_secret_name(server_id, name)
                    origin = (server_id, name.casefold())
                    previous_origin = generated_secret_origins.get(env_name)
                    if previous_origin is not None and previous_origin != origin:
                        raise SetupValidationError("MCP 敏感 Header 环境变量名称冲突")
                    generated_secret_origins[env_name] = origin
                    environment[env_name] = value
                    value = f"${{{env_name}}}"
            safe_headers[name] = value
        server["headers"] = safe_headers
        sanitized_servers[server_id] = server
        if not bool(server.get("disabled", False)):
            enabled_count += 1
    if enabled_count == 0:
        raise SetupValidationError("MCP 开启时至少需要一个未禁用 Server")
    sanitized: dict[str, object] = {"mcpServers": sanitized_servers}
    try:
        MCPConfigFile.model_validate(sanitized)
    except ValidationError as exc:
        raise SetupValidationError("MCP 配置不符合合同") from exc
    return sanitized


def missing_mcp_environment(document: object, environment: Mapping[str, str]) -> tuple[str, ...]:
    serialized = json.dumps(document, ensure_ascii=False)
    return tuple(sorted(set(_ENV_REFERENCE.findall(serialized)).difference(environment)))


def discover_speech_profiles(speech_root: Path) -> tuple[SpeechProfileCandidate, ...]:
    voices = speech_root / "voices"
    if not voices.is_dir():
        return ()
    validator = VoiceProfileService(
        repository=VoiceProfileRepository.__new__(VoiceProfileRepository),
        paths=SpeechPathPolicy(speech_root),
    )
    valid: list[SpeechProfileCandidate] = []
    for directory in sorted(voices.iterdir(), key=lambda item: item.name):
        manifest_path = directory / "profile.toml"
        if not directory.is_dir() or not manifest_path.is_file():
            continue
        try:
            manifest = validator.validate_profile(directory)
        except (OSError, UnicodeError, ValueError):
            continue
        valid.append(
            SpeechProfileCandidate(
                profile_id=manifest.id,
                display_name=manifest.display_name,
            )
        )
    return tuple(valid)


def validate_configuration(paths: SetupPaths, configuration: SetupConfiguration) -> Settings:
    environment = dict(configuration.environment)
    environment["BOT_PERSONA_FILE"] = str((paths.root / "config/persona.md").resolve())
    environment["MODEL_PROFILES_FILE"] = str(paths.model_profiles.resolve())
    environment["MCP_CONFIG_PATH"] = str(paths.mcp.resolve())
    environment["YUKI_VERSION"] = __version__
    _validate_credentials_and_endpoints(
        environment,
        flash_enabled="[profiles.flash]" in configuration.model_profiles,
    )
    _validate_local_feature_files(paths, environment)
    payload = _settings_payload(environment)
    try:
        settings = Settings.model_validate(payload)
    except ValidationError as exc:
        raise SetupValidationError(_validation_summary(exc)) from exc

    with tempfile.TemporaryDirectory(prefix="yuki-setup-validate-") as temporary_name:
        temporary = Path(temporary_name)
        profile_path = temporary / "model_profiles.toml"
        mcp_path = temporary / ".mcp.json"
        profile_path.write_text(configuration.model_profiles, encoding="utf-8")
        mcp_path.write_text(
            json.dumps(configuration.mcp_document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            catalog = load_model_profile_catalog(
                profile_path,
                legacy_provider=settings.llm_provider,
                legacy_base_url=settings.llm_base_url,
                legacy_model=settings.llm_model,
                legacy_timeout_seconds=settings.llm_timeout_seconds,
                legacy_max_retries=settings.llm_max_retries,
                legacy_temperature=settings.llm_temperature,
                legacy_max_output_tokens=settings.llm_max_output_tokens,
                legacy_thinking_enabled=settings.llm_thinking_enabled,
                legacy_reasoning_effort=settings.llm_reasoning_effort,
                environment=environment,
            )
            chat_profile = catalog.profiles[catalog.routes[ModelTask.CHAT_AGENT].profile_id]
            if (
                settings.web.mode
                in {
                    WebMode.NATIVE,
                    WebMode.NATIVE_WITH_TAVILY_FALLBACK,
                }
                and ModelCapability.NATIVE_WEB_SEARCH not in chat_profile.capabilities
            ):
                raise SetupValidationError("模型原生搜索只支持 DeepSeek Responses 主模型")
            if settings.mcp_enabled:
                sanitized = sanitize_mcp_document(configuration.mcp_document, dict(environment))
                if sanitized != configuration.mcp_document:
                    raise SetupValidationError("MCP 敏感 Header 必须通过环境变量引用")
                missing = missing_mcp_environment(sanitized, environment)
                if missing:
                    raise SetupValidationError("MCP 缺少环境变量：" + ", ".join(missing))
                load_mcp_config(mcp_path, environment=environment)
            else:
                MCPConfigFile.model_validate(configuration.mcp_document)
        except (ModelRuntimeConfigurationError, MCPConfigurationError, ValidationError) as exc:
            raise SetupValidationError(str(exc)) from exc
    return settings


def commit_configuration(
    paths: SetupPaths,
    document: EnvironmentDocument,
    configuration: SetupConfiguration,
) -> Path | None:
    targets: dict[Path, bytes] = {
        paths.env: document.merge(configuration.environment).encode("utf-8"),
    }
    if configuration.write_model_profiles:
        targets[paths.model_profiles] = configuration.model_profiles.encode("utf-8")
    if configuration.write_mcp:
        targets[paths.mcp] = (
            json.dumps(configuration.mcp_document, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
    if configuration.pending_plugins is not None:
        targets[paths.pending] = (
            json.dumps(
                {
                    "schema_version": 1,
                    "selected_plugins": list(configuration.pending_plugins),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
    previous = {path: path.read_bytes() if path.is_file() else None for path in targets}
    backup = _create_backup(
        paths,
        tuple(path for path, value in previous.items() if value is not None),
    )
    try:
        for path, content in targets.items():
            _atomic_write(path, content, private=path in {paths.env, paths.pending})
    except OSError:
        for path, previous_content in previous.items():
            if previous_content is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write(
                    path,
                    previous_content,
                    private=path in {paths.env, paths.pending},
                )
        raise
    _prune_backups(paths.backups, keep=5)
    return backup


async def apply_pending_plugins(paths: SetupPaths, settings: Settings) -> int:
    if not paths.pending.is_file():
        return 0
    try:
        payload = json.loads(paths.pending.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1:
            raise ValueError
        selected = frozenset(str(item) for item in payload.get("selected_plugins", []))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise SetupValidationError("待应用插件配置无效") from exc
    discovery = PluginDiscovery(
        settings.plugin_directory,
        yuki_version=__version__,
        plugin_api=settings.plugin_api_version,
    )
    database = Database(settings.database_url)
    repository = PluginInstallationRepository(database)
    changed = 0
    try:
        manifests = tuple(
            item.manifest for item in discovery.discover() if item.manifest is not None
        )
        known_ids = {manifest.id for manifest in manifests}
        unknown = selected.difference(known_ids)
        if unknown:
            raise SetupValidationError("选择的插件已不存在或 Manifest 无效")
        for manifest in manifests:
            previous = await repository.get(manifest.id)
            record = await repository.upsert_discovered(
                plugin_id=manifest.id,
                name=manifest.name,
                version=manifest.version,
                plugin_api=manifest.plugin_api,
                yuki_requires=manifest.yuki_requires,
                manifest_hash=manifest.manifest_hash,
                entrypoint=manifest.entrypoint,
                requested_permissions=(permission.value for permission in manifest.permissions),
            )
            if manifest.id in selected:
                requested = frozenset(permission.value for permission in manifest.permissions)
                requires_change = (
                    not record.enabled or frozenset(record.approved_permissions) != requested
                )
                if requires_change:
                    await repository.approve(manifest.id)
                    await repository.set_enabled(manifest.id, enabled=True)
                    changed += 1
            elif record.enabled:
                await repository.set_enabled(manifest.id, enabled=False)
                changed += 1
            elif previous is not None and previous.enabled:
                # A changed manifest revokes approval and disables itself during discovery.
                changed += 1
    finally:
        await database.close()
    paths.pending.unlink(missing_ok=True)
    return changed


def verify_health(url: str, *, timeout_seconds: float = 180.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_category = "unavailable"
    with httpx.Client(timeout=3.0) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(url)
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, dict) and payload.get("status") == "ok":
                    return payload
                last_category = "unhealthy"
            except (httpx.HTTPError, ValueError):
                last_category = "unavailable"
            time.sleep(2)
    raise SetupValidationError(f"Bot 健康检查超时（{last_category}）")


def _settings_payload(environment: Mapping[str, str]) -> dict[str, object]:
    upper = {key.upper(): value for key, value in environment.items()}
    payload: dict[str, object] = {}
    for name, field in Settings.model_fields.items():
        aliases: list[str] = [name.upper()]
        alias = field.validation_alias
        if isinstance(alias, str):
            aliases.insert(0, alias.upper())
        elif isinstance(alias, AliasChoices):
            aliases = [
                str(item).upper() for item in alias.choices if isinstance(item, str)
            ] + aliases
        elif isinstance(alias, AliasPath) and alias.path and isinstance(alias.path[0], str):
            aliases.insert(0, alias.path[0].upper())
        value = next((upper[item] for item in aliases if item in upper), None)
        if value is not None:
            if isinstance(value, str) and get_origin(field.annotation) in {
                dict,
                list,
                set,
                tuple,
            }:
                try:
                    payload[name] = json.loads(value)
                except json.JSONDecodeError:
                    payload[name] = value
            else:
                payload[name] = value
    return payload


def _validation_summary(error: ValidationError) -> str:
    rows = error.errors(include_url=False, include_input=False)
    if not rows:
        return "配置验证失败"
    row = rows[0]
    location = ".".join(str(item) for item in row.get("loc", ())) or "配置"
    return f"{location}: {row.get('msg', '配置验证失败')}"


def _create_backup(paths: SetupPaths, existing: Iterable[Path]) -> Path | None:
    files = tuple(existing)
    if not files:
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = paths.backups / stamp
    for path in files:
        target = destination / path.relative_to(paths.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        if os.name != "nt":
            target.chmod(0o600)
        else:
            _restrict_windows_acl(target)
    return destination


def _prune_backups(root: Path, *, keep: int) -> None:
    if not root.is_dir():
        return
    directories = tuple(sorted((item for item in root.iterdir() if item.is_dir()), reverse=True))
    for directory in directories[keep:]:
        resolved = directory.resolve()
        if resolved.parent != root.resolve():
            raise SetupValidationError("备份目录越界")
        shutil.rmtree(resolved)


def _atomic_write(path: Path, content: bytes, *, private: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if private:
            if os.name == "nt":
                _restrict_windows_acl(temporary)
            else:
                temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _decode_env_value(value: str) -> str:
    raw_value, _comment = _split_env_comment(value)
    stripped = raw_value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] == '"':
        try:
            decoded = json.loads(stripped)
            return decoded if isinstance(decoded, str) else stripped
        except json.JSONDecodeError:
            return stripped[1:-1]
    if len(stripped) >= 2 and stripped[0] == stripped[-1] == "'":
        return stripped[1:-1]
    return stripped


def _encode_env_value(value: str) -> str:
    if _SAFE_ENV_VALUE.fullmatch(value):
        return value
    return json.dumps(value, ensure_ascii=False)


def _split_env_comment(value: str) -> tuple[str, str]:
    quote = ""
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote == '"':
            escaped = True
            continue
        if quote:
            if character == quote:
                quote = ""
            continue
        if character in {'"', "'"}:
            quote = character
            continue
        if character == "#" and index > 0 and value[index - 1].isspace():
            head = value[:index].rstrip()
            return head, value[len(head) :]
    return value, ""


def _mcp_secret_name(server_id: str, header_name: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]+", "_", f"MCP_{server_id}_{header_name}".upper()).strip("_")
    return normalized[:64] or "MCP_SERVER_SECRET"


def _validate_credentials_and_endpoints(
    environment: Mapping[str, str],
    *,
    flash_enabled: bool,
) -> None:
    required = ("SUPERUSERS", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")
    missing = [name for name in required if not _configured_value(environment.get(name, ""))]
    if missing:
        raise SetupValidationError("基础配置不完整：" + ", ".join(missing))
    endpoint_names = ["LLM_BASE_URL"]
    if flash_enabled:
        for name in ("LLM_FLASH_BASE_URL", "LLM_FLASH_API_KEY", "LLM_FLASH_MODEL"):
            if not _configured_value(environment.get(name, "")):
                raise SetupValidationError("Flash 配置不完整")
        endpoint_names.append("LLM_FLASH_BASE_URL")
    if environment.get("MEMORY_EMBEDDING_ENABLED", "false").casefold() == "true":
        if not all(
            _configured_value(environment.get(name, ""))
            for name in (
                "MEMORY_EMBEDDING_BASE_URL",
                "MEMORY_EMBEDDING_API_KEY",
                "MEMORY_EMBEDDING_MODEL",
            )
        ):
            raise SetupValidationError("Embedding 配置不完整")
        endpoint_names.append("MEMORY_EMBEDDING_BASE_URL")
    if environment.get("VISION_ENABLED", "false").casefold() == "true":
        if not all(
            _configured_value(environment.get(name, ""))
            for name in ("VISION_BASE_URL", "VISION_API_KEY", "VISION_MODEL")
        ):
            raise SetupValidationError("Vision 配置不完整")
        endpoint_names.append("VISION_BASE_URL")
    if environment.get("WEB_MODE", "disabled").casefold() == "tavily" and not _configured_value(
        environment.get("TAVILY_API_KEY", "")
    ):
        raise SetupValidationError("Tavily 模式必须配置 TAVILY_API_KEY")
    for name in endpoint_names:
        value = environment.get(name, "").strip()
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SetupValidationError(f"{name} 必须是绝对 HTTP(S) 地址")


def _configured_value(value: str) -> str:
    normalized = value.strip()
    return "" if normalized.casefold().startswith("replace-with-") else normalized


def _validate_local_feature_files(paths: SetupPaths, environment: Mapping[str, str]) -> None:
    if environment.get("SPEECH_ENABLED", "false").casefold() != "true":
        return
    genie_data = paths.root / "data/speech/genie_data"
    if not genie_data.is_dir() or not any(genie_data.iterdir()):
        raise SetupValidationError("Speech 已开启，但 data/speech/genie_data 为空")
    profiles = discover_speech_profiles(paths.root / "data/speech")
    selected = environment.get("SPEECH_DEFAULT_PROFILE", "")
    if not profiles or selected not in {item.profile_id for item in profiles}:
        raise SetupValidationError("Speech 默认声线不存在或档案无效")


def _restrict_windows_acl(path: Path) -> None:
    domain = os.getenv("USERDOMAIN", "").strip()
    username = os.getenv("USERNAME", "").strip()
    identity = f"{domain}\\{username}" if domain and username else username
    if not identity:
        raise OSError("无法确定当前 Windows 用户，不能安全写入密钥文件")
    completed = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"{identity}:(F)"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise OSError("无法限制 Windows 密钥文件 ACL")
