# 安全模型

## 当前真实边界

Plugin API 2.0 是 API 访问治理，不是 Python/OS 沙盒。插件与 Yuki 同进程运行，因此管理员必须把插件源码视为与 Bot 本身同等级的本地可信代码。恶意插件理论上可以使用 Python 能力绕过 SDK；当前版本不承诺安全运行未知第三方代码。

## Host 不向 Facade 暴露

- `ApplicationContainer`、完整 `Settings`；
- SQLAlchemy Session、主数据库连接、Repository；
- NoneBot Bot、原始 `MessageEvent`；
- API Key 集合、完整系统 Prompt、模型隐藏推理；
- Docker Socket、任意宿主文件路径；
- Shell、Python eval、任意 SQL。

插件只应导入 `yuki_plugin_sdk`。这些 API 约束用于可维护性、审计和最小权限，即使它们不是强沙盒。

## 不变量

1. `SUPERUSERS` 只来自启动配置和当前真实 OneBot 发送者 QQ。
2. 插件、历史、网页、OCR、记忆和用户自报都不能授予管理员权限。
3. 插件只能缩小后端能力，不能扩大权限或指定工具顺序。
4. 图片轮次关闭插件写工具、管理员写、OneBot 修改、关系/记忆/配置写入。
5. 使用网页工具后撤销本轮高风险修改能力。
6. 自动化 Action 服从创建时与执行时权限交集。
7. 插件 Prompt 和工具结果始终是不可信数据。
8. Secret、完整正文、Prompt、推理、Base64/OCR 不进入普通日志或审计。

## 网络

官方 HTTP Facade 执行精确域名白名单、DNS/重定向复检、私有地址拒绝、体积/超时限制。插件不能把 Manifest 白名单当 SSRF 例外。

## 安装建议

- 固定源码版本并审阅 diff；不从聊天链接自动下载。
- 最小权限批准；高风险权限单独说明。
- 以非 root Bot 用户运行容器，插件目录只读挂载。
- 不把 Docker Socket、宿主密钥目录或宽泛目录挂进 Bot。
- Manifest 改变后重新审阅，不盲目批准。

