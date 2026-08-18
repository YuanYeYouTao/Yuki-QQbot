# 参考音频与风格

一条 reference 由稳定 ID、style、aliases、音频、逐字对应文本、语言、启用状态和优先级
组成。建议使用清晰、单人、无混响的短语音，转录必须和实际发音一致。可分别准备 neutral、
gentle、happy、shy 等参考；Main Agent 和插件只能给 `style_hint`，不能指定路径。

匹配顺序是规范化后的 style/alias，未匹配时回到 `default_style`，相同候选按优先级稳定选择，
不额外调用 LLM。新增参考可提供含 `reference.toml` 的目录，或音频文件与同名 `.toml`
sidecar：`qq-ai-bot-cli speech reference add <profile> <目录或音频>`。

reference 的 `language` 描述的是这段参考音频本身，不是生成文本的目标语言。Worker 会始终用它
解析参考音频，同时用本轮 `target_language` 处理正文；因此无需为了中文和日文复制两套相同
音色档案。目标语言参与缓存键，同一句跨语言生成不会错误命中旧缓存。
