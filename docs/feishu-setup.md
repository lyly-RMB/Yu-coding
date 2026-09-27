# Feishu 联调清单

更新时间：2026-09-23

飞书群聊是 ProjectLens 的工作现场。本地/试点推荐路径：

```text
飞书 @ / 卡片点击
  -> ProjectLens 飞书长连接（WS ingress）
  -> /api/v1/feishu/events
  -> 预览卡；「直接回答」时在进程内跑 Hermes
```

**新人从零联调请先看：** [`feishu-test-quickstart.md`](feishu-test-quickstart.md)

相关文档：

- 产品总纲：[`projectlens-product-document.md`](projectlens-product-document.md)
- 配置剖面：[`pilot-launch-config.md`](pilot-launch-config.md)
- 项目接入：[`projectspace-onboarding-guide.md`](projectspace-onboarding-guide.md)
- 上下文管理器：[`projectlens-context-manager-development-plan.md`](projectlens-context-manager-development-plan.md)

## 1. 服务端配置

1. 复制 `.env.example` 为 `.env`（密钥只放本机，勿提交）。
2. 填入飞书 `APP_ID` / `APP_SECRET`。
3. 设置 `PROJECT_LENS_FEISHU_VERIFICATION_TOKEN`；建议同时配置 `PROJECT_LENS_FEISHU_SIGNING_SECRET`。
4. 在 `PROJECT_LENS_FEISHU_PROJECT_BINDINGS` 中配置租户、群聊与**唯一**项目映射。
5. 确认 `PROJECT_LENS_DATABASE_PATH` 为持久化文件，`PROJECT_LENS_CONVERSATION_STORE=sqlite`。
6. 生产决策路径保持：

```text
PROJECT_LENS_AGENT_MODE=hermes
```

7. 配置 Hermes 模型（`PROJECT_LENS_FEISHU_HERMES_*`；可复用 OpenAI 兼容网关）。试点/生产内部 API 需配置 `PROJECT_LENS_SERVICE_TOKEN`。
8. 安装长连接依赖：`pip install -e ".[feishu-ws]"`（提供 `lark-oapi`）。
9. 启动后检查：

```text
GET /api/v1/integrations/feishu/status
```

确认 `configured=true`、`outbound_mode=http`，并且 `binding_count` 大于 0。

## 2. 事件与回调（推荐：长连接）

开放平台 **事件订阅** 与 **回调配置** 均选 **使用长连接**。

订阅：

- 事件：`im.message.receive_v1`（接收消息）
- 回调：`card.action.trigger`（卡片回传交互）

本机启动：

```powershell
.\scripts\start-projectlens-feishu-ws.ps1
```

同一飞书应用同时只能有一条长连接：不要再跑 Hermes Feishu Gateway 或其它 WS 客户端。

服务会校验 verification token、可选签名和 `event_id` 去重。未映射的群聊在创建运行前返回 `403`。

### 2.1 备选：HTTP 请求地址（公网部署）

仅当服务部署在公网 HTTPS 时使用：

```text
https://<your-domain>/api/v1/feishu/events
```

1. 完成 URL Verification。  
2. 订阅消息事件与卡片回传。  
3. 只跑 `uvicorn`，**不要**再开本机 WS ingress。

本地联调不要依赖 cloudflared / ngrok；优先长连接。

## 3. 权限与可见性

权限在检索前进入 Agent 上下文，不是回答后再过滤：

```text
用户角色权限 ∩ 群聊可见范围 ∩ 项目资源权限
```

实践建议：

- 开发阶段开通机器人收发消息相关权限，并发布或测试安装到目标企业。
- 「选择更多历史」群聊细选还需 **`im:message.group_msg`（获取群组中所有消息）**，开通后必须发布版本。
- 生产环境只安装到需要 ProjectLens 的项目群。
- 群 ↔ 项目关系以 `PROJECT_LENS_FEISHU_PROJECT_BINDINGS` 与 ProjectSpace `feishu_chat_bindings` 为准。
- 混合业务/研发群应使用更窄的 `ChatVisibilityPolicy`；敏感日志与内部调用细节默认不进业务群。

## 4. 本地测试

未配置 App ID / App Secret 时，使用 recording adapter，不会向外网发消息。可用：

```powershell
py -3.12 -m pytest tests/test_feishu_http_and_acl.py tests/test_feishu_integration.py tests/test_hermes_runtime.py tests/test_feishu_ws_ingress.py -q
```

群聊读历史冒烟（需凭证 + 群 id）：

```powershell
.\scripts\smoke-feishu-group-history.ps1 -ChatId "oc_..."
```

## 5. 群置顶说明（建议）

```text
ProjectLens 是只读项目协作 Copilot（默认）。
可以问：项目介绍 / 当前状态 / 需求与实现是否一致 / 负责人与阻塞 / 会议结论与待办
@ 后会先出上下文预览卡，确认后再回答
回答会区分：已确认事实、推断、冲突、未知和下一步
暂不支持：自动改代码、自动 PR、自动部署回滚、无审批写正式项目记忆
```

## 6. 常见问题

**飞书没回消息 / 没出预览卡？**  
未配 App ID/Secret 时只走 recording adapter。真机需：凭证、bindings、机器人在群、长连接 ingress 在跑、开放平台选长连接且未与其它 WS 冲突。

**点卡片按钮无反应？**  
回调也要选长连接，并订阅 `card.action.trigger`。

**群聊发言区不可用？**  
检查并发布 `im:message.group_msg`；用 `smoke-feishu-group-history.ps1` 验证。

**越权或看不到内容？**  
检查成员角色、群聊可见性与 ProjectSpace `public_sources` / `readable_sources` 交集。

**会话追问丢上下文？**  
确认 `CONVERSATION_STORE=sqlite` 且 `DATABASE_PATH` 指向同一持久化文件。
