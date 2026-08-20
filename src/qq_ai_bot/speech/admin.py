"""Deterministic speech administration shared by QQ commands and tools."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import AdminActor, RuntimeConfigSnapshot
from qq_ai_bot.domain.messages import AttachmentKind, InboundMessage, OutboundMedia, OutboundMessage
from qq_ai_bot.speech.genie_client import GenieWorkerClient
from qq_ai_bot.speech.profiles import VoiceProfileService
from qq_ai_bot.speech.provider import SpeechSynthesisRequest
from qq_ai_bot.speech.service import SpeechService


@dataclass(frozen=True, slots=True)
class SpeechAdminResult:
    text: str
    outbound: OutboundMessage | None = None


class SpeechAdminService:
    def __init__(
        self,
        *,
        speech: SpeechService,
        profiles: VoiceProfileService,
        runtime_config: RuntimeConfigService,
        worker: GenieWorkerClient,
        bot_display_name: str = "Yuki",
    ) -> None:
        self._speech = speech
        self._profiles = profiles
        self._runtime_config = runtime_config
        self._worker = worker
        self._bot_display_name = bot_display_name

    async def execute(
        self,
        *,
        actor: AdminActor,
        message: InboundMessage,
        argument: str,
        runtime: RuntimeConfigSnapshot,
    ) -> SpeechAdminResult:
        operation, _, remainder = argument.strip().partition(" ")
        operation = operation.casefold() or "status"
        if operation == "status":
            status = await self.status_data(runtime)
            doctor = await self._profiles.doctor()
            return SpeechAdminResult(
                "本地语音状态：\n"
                f"- 配置启用：{runtime.speech.enabled}\n"
                f"- Worker 已连接：{status['worker_connected']}\n"
                f"- Worker 就绪：{status['worker_ready']}\n"
                f"- Worker 忙碌：{status['worker_busy']}\n"
                f"- 当前加载声线：{status['loaded_profile_id'] or '无'}\n"
                f"- GenieData：{'已准备' if doctor['genie_data_present'] else '缺失'}\n"
                f"- 默认 profile：{runtime.speech.default_profile or '未设置'}\n"
                f"- 可用风格：{status['style_count']}\n"
                f"- 队列深度：{status['queue_depth']}"
            )
        if operation == "profiles":
            rows = await self._profiles.list_profiles()
            if not actor.is_superuser:
                rows = tuple(
                    row
                    for row in rows
                    if row.enabled and row.profile_id == runtime.speech.default_profile
                )
            if not rows:
                return SpeechAdminResult("尚未导入声线档案。")
            return SpeechAdminResult(
                "声线档案：\n"
                + "\n".join(
                    f"- {row.profile_id}：{row.display_name}"
                    f"（{'启用' if row.enabled else '停用'}"
                    f"{'，默认' if row.is_default else ''}）"
                    for row in rows
                )
            )
        if operation == "show":
            requested = remainder.strip() or runtime.speech.default_profile
            if not actor.is_superuser and requested != runtime.speech.default_profile:
                raise PermissionError("普通用户只能查看当前启用声线。")
            profile = await self._profiles.get_profile(requested)
            if profile is None:
                return SpeechAdminResult("未找到该声线档案。")
            return SpeechAdminResult(
                f"声线：{profile.profile_id} / {profile.display_name}\n"
                f"Provider：{profile.provider}\n"
                f"引擎模型：{profile.engine_model_version.value}\n"
                f"默认语言：{profile.language}\n"
                f"可用语言：{'、'.join(profile.supported_languages)}\n"
                f"默认风格：{profile.default_style}\n"
                f"状态：{'启用' if profile.enabled else '停用'}\n"
                f"来源：{profile.source}\n"
                f"参考音频：{len(profile.references)} 条"
            )
        if operation == "styles":
            profile_id = remainder.strip() or runtime.speech.default_profile
            if not profile_id:
                return SpeechAdminResult("请提供 profile_id，或先设置默认声线。")
            if not actor.is_superuser and profile_id != runtime.speech.default_profile:
                raise PermissionError("普通用户只能查看当前启用声线的风格。")
            styles = await self._profiles.list_styles(profile_id)
            return SpeechAdminResult("可用风格：" + ("、".join(styles) if styles else "无"))
        if operation in {"use", "reload"}:
            self._require_superuser(actor)
            profile_id = remainder.strip()
            if not profile_id:
                return SpeechAdminResult(f"请提供 profile_id：/ai voice {operation} <profile_id>")
            profile = (
                await self._profiles.activate_profile(profile_id)
                if operation == "use"
                else await self._profiles.reload_profile(profile_id)
            )
            return SpeechAdminResult(
                f"已{'启用默认声线' if operation == 'use' else '重新加载声线'}："
                f"{profile.profile_id}。"
            )
        if operation == "cache" and remainder.strip().casefold() == "cleanup":
            self._require_superuser(actor)
            expired_count, file_count = await self._speech.cleanup(runtime=runtime.speech)
            return SpeechAdminResult(
                f"语音缓存清理完成：过期记录 {expired_count} 条，删除文件 {file_count} 个。"
            )
        if operation == "test":
            return await self._test(actor, message, remainder, runtime)
        return SpeechAdminResult(
            "用法：/ai voice status|profiles|show <profile>|use <profile>|"
            "styles [profile]|test <文本>|test <profile> <style> <文本>|"
            "reload <profile>|cache cleanup"
        )

    async def mark_sent(self, message: OutboundMessage) -> None:
        for media in message.media:
            if media.generation_id is not None:
                await self._speech.mark_sent(media.generation_id)

    async def status_data(self, runtime: RuntimeConfigSnapshot) -> dict[str, object]:
        health = await self._speech.health()
        metrics = await self._speech.metrics()
        profile = (
            await self._profiles.get_profile(runtime.speech.default_profile)
            if runtime.speech.default_profile
            else None
        )
        return {
            "enabled": runtime.speech.enabled,
            "provider": runtime.speech.provider,
            "worker_connected": health.connected,
            "worker_ready": health.ready,
            "worker_busy": health.busy,
            "loaded_profile_id": health.loaded_profile_id,
            "default_profile": runtime.speech.default_profile,
            "style_count": (
                len({item.style for item in profile.references if item.enabled})
                if profile is not None
                else 0
            ),
            "queue_depth": metrics.queue_depth,
            "last_generation_at": (
                metrics.last_generation_at.isoformat()
                if metrics.last_generation_at is not None
                else None
            ),
            "last_generation_latency_seconds": metrics.last_generation_latency_seconds,
            "last_error_category": metrics.last_error_category,
        }

    async def execute_action(
        self,
        action: str,
        arguments: dict[str, object],
        actor: AdminActor,
    ) -> dict[str, object]:
        self._require_superuser(actor)
        runtime = await self._runtime_config.snapshot(
            user_id=actor.user_id,
            group_id=actor.current_group_id,
        )
        if action == "speech.status":
            health = await self._speech.health()
            return {
                "enabled": runtime.speech.enabled,
                "worker_connected": health.connected,
                "worker_ready": health.ready,
                "worker_busy": health.busy,
                "loaded_profile_id": health.loaded_profile_id,
            }
        if action == "speech.profile.list":
            rows = await self._profiles.list_profiles()
            return {"profiles": [self._profile_data(row) for row in rows]}
        profile_id = str(arguments.get("profile_id") or "").strip()
        if action == "speech.profile.show":
            row = await self._profiles.get_profile(profile_id)
            if row is None:
                raise LookupError("voice profile not found")
            return self._profile_data(row)
        if action == "speech.profile.activate":
            return self._profile_data(await self._profiles.activate_profile(profile_id))
        if action == "speech.profile.enable":
            return self._profile_data(await self._profiles.enable_profile(profile_id))
        if action == "speech.profile.disable":
            return self._profile_data(await self._profiles.disable_profile(profile_id))
        if action == "speech.profile.reload":
            return self._profile_data(await self._profiles.reload_profile(profile_id))
        if action == "speech.reference.list":
            row = await self._profiles.get_profile(profile_id)
            if row is None:
                raise LookupError("voice profile not found")
            return {
                "profile_id": row.profile_id,
                "references": [
                    {
                        "reference_key": item.reference_key,
                        "style": item.style,
                        "aliases": list(item.aliases),
                        "enabled": item.enabled,
                    }
                    for item in row.references
                ],
            }
        if action == "speech.cache.cleanup":
            expired, deleted = await self._speech.cleanup(runtime=runtime.speech)
            return {"expired_records": expired, "deleted_files": deleted}
        if action == "speech.worker.restart":
            await self._worker.shutdown()
            return {"restart_requested": True}
        if action == "speech.test":
            text = str(arguments.get("text") or "").strip()
            if not text:
                raise ValueError("text is required")
            result = await self._speech.synthesize(
                SpeechSynthesisRequest(
                    request_id=str(uuid4()),
                    profile_id=profile_id or runtime.speech.default_profile,
                    style_hint=str(arguments.get("style_hint") or ""),
                    text=text,
                    split_sentence=runtime.speech.split_sentence,
                    conversation_key=actor.conversation_key,
                    trigger_event_id=None,
                    turn_token=None,
                ),
                runtime=runtime.speech,
            )
            return {
                "generation_id": result.generation_id,
                "profile_id": result.profile_id,
                "reference_key": result.reference_key,
                "target_language": result.target_language,
                "duration_milliseconds": result.duration_milliseconds,
                "queued_reply_effect": False,
            }
        raise KeyError(action)

    @staticmethod
    def _profile_data(profile: object) -> dict[str, object]:
        from qq_ai_bot.speech.models import VoiceProfile

        if not isinstance(profile, VoiceProfile):
            raise TypeError("expected VoiceProfile")
        return {
            "profile_id": profile.profile_id,
            "display_name": profile.display_name,
            "provider": profile.provider,
            "engine_model_version": profile.engine_model_version.value,
            "language": profile.language,
            "supported_languages": profile.supported_languages,
            "default_style": profile.default_style,
            "enabled": profile.enabled,
            "is_default": profile.is_default,
            "reference_count": len(profile.references),
        }

    async def _test(
        self,
        actor: AdminActor,
        message: InboundMessage,
        remainder: str,
        runtime: RuntimeConfigSnapshot,
    ) -> SpeechAdminResult:
        value = remainder.strip()
        if not value:
            return SpeechAdminResult("请提供测试文本。")
        parts = value.split(maxsplit=2)
        profile_id = runtime.speech.default_profile
        style = ""
        text = value
        if len(parts) == 3 and await self._profiles.get_profile(parts[0]) is not None:
            if not actor.is_superuser and parts[0] != runtime.speech.default_profile:
                raise PermissionError("普通用户只能使用当前启用声线。")
            profile_id, style, text = parts
        generated = await self._speech.synthesize(
            SpeechSynthesisRequest(
                request_id=str(uuid4()),
                profile_id=profile_id,
                style_hint=style,
                text=text,
                split_sentence=runtime.speech.split_sentence,
                conversation_key=message.scope().key,
                trigger_event_id=None,
                turn_token=None,
            ),
            runtime=runtime.speech,
        )
        return SpeechAdminResult(
            "",
            OutboundMessage(
                media=(
                    OutboundMedia(
                        kind=AttachmentKind.AUDIO,
                        mime_type="audio/wav",
                        summary=(
                            f"{self._bot_display_name} 语音测试，声线：{generated.profile_id}，"
                            f"风格：{generated.reference_key}"
                        ),
                        local_path=str(self._speech.audio_path(generated)),
                        spoken_text=text,
                        generation_id=generated.generation_id,
                        voice_profile_id=generated.profile_id,
                        voice_reference_key=generated.reference_key,
                        duration_milliseconds=generated.duration_milliseconds,
                    ),
                )
            ),
        )

    @staticmethod
    def _require_superuser(actor: AdminActor) -> None:
        if not actor.is_superuser:
            raise PermissionError("该语音管理操作仅限超级管理员。")
