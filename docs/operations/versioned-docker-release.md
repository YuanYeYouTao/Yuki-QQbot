# Versioned Docker Release 运维说明

Yuki 3.6.0 的正式镜像只由 `.github/workflows/release.yml` 发布，目标平台为
`linux/amd64`。本地开发镜像不属于发布合同。

## 首次 GHCR Bootstrap

正式 Tag 前只执行一次：

1. 在 GitHub Actions 打开 `Release`，对最新 `main` 运行 `workflow_dispatch`。
2. 工作流只发布两个 `bootstrap-amd64` 标签，用于创建 Bot 与 Genie-TTS Worker Package。
3. 在仓库所有者的 GitHub Packages 设置中，把两个 Package 的 visibility 改为 **Public**。
4. 在未登录 GHCR 的环境确认两个 bootstrap 镜像可拉取。

公开 Package 才能让普通用户匿名执行 `docker compose pull`。Bootstrap 不创建正式 Release，
也不发布正式版本标签或 `latest`。

## 正式发布

1. 确认 `main` 中根 `pyproject.toml`、运行时 `__version__`、根 `uv.lock` 和 Memory Release
   Check 均为同一版本；Worker 内部组件版本保持 `1.9.0`。
2. 确认 Quality workflow 通过。
3. 在 `main` 可达提交创建严格 `vX.Y.Z` Tag 并推送。
4. Release workflow 会强制重跑完整 Quality，构建本地 amd64 镜像和部署包，并在无源码目录
   检查 Bot、Alembic、Worker、挂载与容器重建。
5. Smoke 通过后才推送不可变版本标签。匿名版本拉取和 Bot 启动通过后才更新 `latest`，最后
   校验 digest 并创建 GitHub Release。

若同一版本标签已存在，只有 OCI `org.opencontainers.image.revision` 等于当前 Tag SHA 才允许
失败重跑；其他 revision 永远不会被覆盖。Release 资产可在同 Tag 下安全 `--clobber` 重传。

## 用户升级合同

正式部署在 `.env` 固定 `YUKI_VERSION`。跨版本升级先修改版本，再执行：

```bash
docker compose pull
docker compose up -d
```

不要用新部署包覆盖旧目录；保留并备份 `data/`、`config/`、`plugins/` 和 `napcat-*`。
