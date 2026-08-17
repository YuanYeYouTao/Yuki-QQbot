# Plugin Speech API

Plugin API 2.0 提供 `ctx.speech`：status、list/get profile、list styles、synthesize、
queue_reply_voice、send_private 和 send_group。权限为 `speech.profile.read`、
`speech.generate`、`speech.reply_effect`、`speech.send`、`speech.manage` 和
`speech.provider.register`。

合成返回 `GeneratedSpeechHandle`，只包含 Handle、generation、profile、时长和到期时间；
插件拿不到相对/绝对路径、模型、参考音频或 Base64，也不能使用其他插件伪造的 Handle。
未来扩展点为 `speech.tts_provider.v1`；外部 Provider 默认不能注册，必须由管理员明确批准
`speech.provider.register`，本版 Core 只实现 Genie Provider。
