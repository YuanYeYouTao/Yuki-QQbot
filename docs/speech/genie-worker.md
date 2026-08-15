# Genie Worker

Worker 位于 `services/genie_tts_worker/`，固定依赖 `genie-tts==2.0.2`。它在导入
`genie_tts` 前设置 `GENIE_DATA_DIR` 和离线环境变量，不调用下载函数，也不启动 Genie
自带的 FastAPI Server。

IPC 使用 4 字节大端长度 + UTF-8 JSON。每个连接处理一个严格版本化请求；支持 health、
load/reload/unload profile、synthesize、cancel、clear reference cache 和 shutdown。
合成先写临时文件，校验 WAV 参数后原子替换。Socket 权限默认 `0660`。

```bash
docker compose --profile speech pull genie-tts-worker
docker compose --profile speech up -d genie-tts-worker
docker compose logs -f genie-tts-worker
```
