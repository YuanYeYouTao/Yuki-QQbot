# 本地语音架构

Yuki 1.8.0 起的出站语音链路是本地合成：`Main Agent send_voice → SpeechService →
Unix Domain Socket → Genie-TTS Worker → 本地 GPT-SoVITS ONNX → WAV → OneBot record`。
音频不经过 LLM，也不调用任何云端 TTS。主 Bot 不依赖 Genie、ONNX Runtime 或 PyTorch。

Worker 是独立、串行的 CPU 推理进程，仅监听共享卷中的 Unix Socket，不开放 HTTP/TCP，
Compose 中使用 `network_mode: none`。模型、参考音频和缓存都位于 `data/speech/`；数据库
只保存相对路径。Adapter 在发送边界读取 WAV 并编码 Base64，因此 NapCat 无须看到宿主机路径。

Genie 当前真实技术边界：只支持 GPT-SoVITS V2/V2ProPlus；输出必须是 32 kHz、单声道、
16 位 WAV；单 Worker 依赖全局模型/参考状态，所以合成串行。业务侧字数、队列和缓存限制
均来自配置，留空表示不增加语音专用上限。
