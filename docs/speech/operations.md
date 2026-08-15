# 运维与命令

准备 GenieData 和声线后，在 `.env` 设置 `SPEECH_ENABLED=true`、
`SPEECH_DEFAULT_PROFILE=<id>`，再启动：

```bash
docker compose --profile speech pull
docker compose --profile speech up -d
docker compose ps
docker compose logs -f bot genie-tts-worker
qq-ai-bot-cli speech status
qq-ai-bot-cli speech genie doctor
qq-ai-bot-cli speech test <profile_id> "你好，这是语音测试。"
```

QQ 中可用 `/ai voice status|profiles|show|styles|test`；切换、reload、缓存清理仅超级管理员。
缓存清理：`qq-ai-bot-cli speech cache cleanup`，不会删除 profile、模型或 reference。升级前仍
先备份 `data/`；Alembic `0015` 和双语字段迁移 `0016` 均为非破坏性迁移。

CPU ONNX 推理在声线加载后可能常驻数 GiB。`SPEECH_WORKER_IDLE_RECYCLE_SECONDS` 默认 300 秒：
最后一次加载或合成后达到该空闲时间，Worker 会正常退出并由 Compose 自动拉起为空载进程，
释放 ONNX 全局 Session；Bot 启动只同步声线元数据，Worker 在首次合成时按需加载模型。设为 `0`
可关闭回收，以较高常驻内存换取下一次语音更快启动。
