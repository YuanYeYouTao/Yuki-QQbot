# 麦当劳官方 MCP

Yuki 使用麦当劳中国托管的 Streamable HTTP MCP Server，不在本项目中复刻菜单、账户或下单接口。
官方接入地址是 `https://mcp.mcd.cn`，身份通过 `Authorization: Bearer <Token>` 绑定。Token 由用户
登录[麦当劳 MCP 开放平台](https://open.mcd.cn/mcp)后，在控制台激活并复制。

当前预设覆盖菜单与营养、附近门店、到店/外送点餐、活动日历、优惠券、订单、麦麦商城和积分。
远端工具通过 `tools/list` 动态发现，因此麦当劳以后增加工具时不需要修改 Yuki 核心代码。

## Docker 启用

1. 将 `.mcp.json.example` 复制为 `config/mcp.json`。
2. 在本地 `.env` 增加以下三项；真实 Token 只能放在 `.env`，不要写进 JSON 或提交 Git。

   ```dotenv
   MCP_ENABLED=true
   MCP_CONFIG_PATH=/app/config/mcp.json
   MCD_MCP_TOKEN=这里粘贴控制台生成的Token
   ```

3. 只重建 Bot，不重建 NapCat：

   ```bash
   docker compose up -d --build bot
   ```

4. 在 QQ 中以超级管理员执行：

   ```text
   /ai mcp doctor mcd
   /ai mcp tools mcd
   ```

诊断成功后，普通聊天即可自然提出“查一下附近麦当劳”“看看现在有什么券”“帮我搭一份套餐”等请求。
Yuki 由 Capability Runtime 本地检索官方工具；不需要让模型拼 `/ai` 命令。

预设把点餐能力拆成两个明确作用域：

- `mcp.mcd.order_planning`：只读查询地址/门店、菜单、餐品详情和价格，绝不暴露 `create-order`；
- `mcp.mcd.order`：包含上述完整链路，并额外允许创建待支付订单及查询订单。
  「下单 / 点餐 / 点麦当劳」只作为这个包的检索别名，不会抄到领券、积分或营养表上。
  若首轮先命中了同服务器的查询工具，`request_tools` 展开点餐包时会让出位置，而不是整包拒绝。

因此“帮我规划并报价，但不要创建订单”和“确认创建待支付订单”不会共用同一组修改权限。到店场景
使用 `query-nearby-stores`，外送场景先使用 `delivery-query-addresses` 和
`delivery-query-stores`；缺少城市、位置或配送地址时，Yuki 应先询问，而不是反复枚举菜单详情。

## 用于持久化自动化

预设在 `yuki.automation` 中为麦当劳声明了独立的后台任务允许列表，默认权限是
`superuser`。启用 `AUTOMATION_ENABLED=true` 后，超级管理员可以直接说：

```text
每天早上九点检查麦当劳本周活动，有新的就私聊告诉我
每天中午查看我的麦当劳优惠券和积分，整理成一条消息发给我
```

创建任务的聊天 Agent 只生成高层 TaskSpec，并选择 Schema 中与活动、优惠券、菜单或订单对应的
模型安全 capability ID；后端编译器再生成 `yuki.agent` 步骤和最小委托权限。Yuki 不再手写
`mcp.mcd.create-order` 一类内部名称，也不会在创建定时任务时提前下单。后台运行时仍通过同一个
`MCPManager` 调用官方服务，不会模拟 `/ai` 命令，也不会复制一套麦当劳客户端。

允许列表目前覆盖时间、活动日历、可用/本人优惠券、自动领券、账户、地址与门店、菜单与套餐详情、
校价、创建到店订单、普通订单和商城订单查询。到店点餐由后台 Agent 依次调用门店查询、
`query-meals`、`query-meal-detail`、`calculate-price` 和 `create-order`，不能根据商品中文名
猜测编码或跳过校价。
官方 `create-order` 只创建待支付订单并返回 `payH5Url`，自动化应把支付链接发送给任务创建者，
不能声称已经支付。创建地址和积分兑换没有默认进入自动化。

`auto-bind-coupons` 和 `create-order` 会改变账户状态，因此不会自动重试；如不希望无人值守的
领券或订单创建，可从 `includeTools` 删除对应工具并重启 Bot。远端工具 Schema 变化后，已有相关
任务会被后端阻止，重新确认并保存任务后才会采用新版参数。

预设还为 `create-order` 配置 `finalizeAfterCommit=true`：订单确认创建后，Agent 保留真实回执并进入
一次无工具最终回复，避免再次提交。该行为不是所有写操作的默认值；例如“发歌后创建提醒”仍可继续调用
下一项工具。

## 调度语义

`.mcp.json.example` 通过 `yuki.toolAnnotations` 将已知查询工具标为只读且幂等，使它们可以安全并行；
新增地址、领券、创建订单和积分兑换等未标为只读的工具继续按修改型串行执行。Annotation 只描述调度语义，
最终身份、账户状态、参数校验和业务结果仍由麦当劳 MCP Server 决定。

工具目录保存在无 Secret 的本地缓存中。Token、请求 Header 和完整工具结果不会进入普通日志；超大结果按
Tool Kernel 的 Artifact 规则落入限时文件。HTTP 401/403 会显示为鉴权失败，429 会显示为请求过于频繁，
5xx 与网络错误会显示为服务暂时不可用。

麦当劳菜单工具会同时返回结构化结果和供旧客户端显示的长文本。Yuki 在通用 MCP 结果层只保留成功调用
的结构化数据及非文本媒体块，避免同一菜单重复两遍后超过 Agent 结果预算。不要用无限工具循环掩盖截断
或缺失工具：一份普通的“门店 → 菜单 → 详情 → 校价”规划应在少量调用内完成。

## 兼容性与排错

- 麦当劳文档声明支持 MCP `2025-06-18` 及更早版本；Yuki 使用初始化协商结果继续本次会话。
- 官方限流为每个 Token 每分钟 600 次。Yuki 仍受全局 MCP 并发与 Schema/数量预算约束。
- `MCP 鉴权失败`：重新从官方控制台复制 Token，确认 `.env` 中没有引号、空格或换行，再只重建 Bot。
- `MCP 服务请求过于频繁`：等待后重试，不要增加并发。
- `当前没有配置 MCP Server`：确认容器内路径为 `/app/config/mcp.json`，而不是宿主机路径。
- 配置改动后可执行 `/ai mcp refresh mcd`；需要强制新会话时执行 `/ai mcp reconnect mcd`。

官方能力、工具参数和版本变化以[麦当劳 MCP 文档](https://open.mcd.cn/mcp/doc)为准。
