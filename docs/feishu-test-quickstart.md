# 飞书测试快速上手

更新时间：2026-09-23

给**拿到本仓库、想在飞书群里试问一轮**的人用。按顺序做即可。

产品边界见 [`projectlens-product-document.md`](projectlens-product-document.md)。更细的配置表见 [`pilot-launch-config.md`](pilot-launch-config.md)；飞书权限与回调清单见 [`feishu-setup.md`](feishu-setup.md)。Hermes 模型/profile 样例见 [`../config/hermes/README.md`](../config/hermes/README.md)。上下文管理器规格见 [`projectlens-context-manager-development-plan.md`](projectlens-context-manager-development-plan.md)。

---

## 0. 你需要准备什么

| 项 | 是否必须 | 说明 |
|---|---|---|
| Python 3.12+ | 必须 | 跑 ProjectLens API；建议装 `.[dev,mcp,feishu-ws]` |
| 飞书企业自建应用 | 必须（真机联调） | App ID / App Secret / Verification Token |
| 一个测试群 | 必须 | 把机器人拉进群 |
| 测试者的飞书 `open_id` | 强烈建议 | 写入 ProjectSpace `members`，否则只能看 `public_sources` |
| OpenAI 兼容 API Key | 真机自然语言问答时必须 | DeepSeek / OpenAI / 内网网关均可 |
| 公网 HTTPS | **不需要**（本地长连接） | 仅生产 HTTP 回调才需要域名/穿透 |

当前推荐联调路径（上下文管理器 / 预览卡）：

```text
飞书群 @机器人
  -> ProjectLens 飞书长连接（WS ingress）
  -> 本机 POST /api/v1/feishu/events
  -> 预览卡（直接回答 / 编辑 / 选择更多历史）
  -> 「直接回答」时在 ProjectLens 内跑 Hermes 工具循环
  -> 回复到飞书
```

不要同时再跑旧的 Hermes Feishu Gateway：同一应用只能有一条长连接，Gateway 会抢走事件且**不会**出预览卡。

---

## 1. 克隆并安装 ProjectLens

```powershell
git clone https://github.com/lyly-RMB/Yu-coding.git
cd Yu-coding
Copy-Item .env.example .env
py -3.12 -m pip install -e ".[dev,mcp,feishu-ws]"
```

`feishu-ws` extra 提供 `lark-oapi`（长连接 SDK）。

先确认 API 能起来：

```powershell
py -3.12 -m uvicorn project_lens.main:app --host 127.0.0.1 --port 8000
```

浏览器打开：

```text
http://127.0.0.1:8000/docs
http://127.0.0.1:8000/api/v1/health
```

---

## 2. 创建飞书应用

在 [飞书开放平台](https://open.feishu.cn/app) 创建企业自建应用：

1. 记下 **App ID**、**App Secret**。
2. 打开「事件与回调」，记下 **Verification Token**；建议开启签名校验相关 Secret（若后台提供）。
3. **事件订阅**、**回调配置**均选 **「使用长连接」**（不要填 HTTP 请求地址 / cloudflared）。
4. 订阅：
   - 事件：接收消息（`im.message.receive_v1`）
   - 回调：卡片回传交互（`card.action.trigger`）
5. 权限（开通后须 **创建版本并发布**）：
   - 收发消息相关（单聊 / 群内 @ 机器人等）
   - **获取群组中所有消息**（`im:message.group_msg`）— 「选择更多历史」里的群聊细选需要
6. 启用机器人能力，把应用安装到你的测试企业；在测试群里添加该机器人。

### 2.1 怎么拿到 `chat_id`

任选一种：

- 用飞书开放平台「调试工具 / API 调试台」查群列表；
- 或先让机器人收到一条消息，从事件 payload 里读 `chat_id`（形如 `oc_...`）。

`tenant_key` 必须与事件里的租户一致，否则绑不上项目。

### 2.2 怎么拿到用户 `open_id`（写入 members）

权限按 **飞书 `open_id`** 识别用户，不是花名或邮箱。

常见取法：

1. 飞书开放平台 API 调试台调用通讯录/用户信息接口；
2. 从机器人收到的消息事件里读 `sender.open_id` / `sender_id.open_id`（形如 `ou_...`）；
3. 企业管理员在应用后台查看测试用户 ID（视租户能力而定）。

拿到后写入 `config/projects/<project>.json`：

```json
"members": [
  {"actor_id": "ou_你的open_id", "roles": ["developer"]}
]
```

角色建议：`developer` / `product` / `manager` / `qa` 等（与 manifest 策略一致）。

**未列入 `members` 的用户**只能访问 `public_sources`（guest）。这是预期行为，不是 bug。

demo `payment` 里有 `u1`、`u_dev` 等本地测试 ID；真机飞书联调时，请把你自己的 `ou_...` 加进去，或临时把测试账号写进 `members`。

---

## 3. 填写 `.env`（最少必填）

编辑仓库根目录 `.env`（**不要提交到 Git**）。字段细节仍以 [`pilot-launch-config.md`](pilot-launch-config.md) 为准。至少需要：

- `PROJECT_LENS_FEISHU_APP_ID` / `APP_SECRET` / `VERIFICATION_TOKEN`
- `PROJECT_LENS_FEISHU_PROJECT_BINDINGS`（或 ProjectSpace `feishu_chat_bindings`）
- `PROJECT_LENS_AGENT_MODE=hermes`
- 真机问答时的模型 key（`PROJECT_LENS_MODEL_*` / `PROJECT_LENS_FEISHU_HERMES_*`）
- `PROJECT_LENS_SERVICE_TOKEN`

---

## 4. 启动（推荐：一键长连接）

```powershell
cd <ProjectLens 根目录>
.\scripts\start-projectlens-feishu-ws.ps1 `
  -PythonExe "D:\path\to\python.exe"
```

脚本会：

1. 停掉冲突的 Hermes Feishu Gateway / cloudflared（若在跑）
2. 必要时拉起 `uvicorn`（`:8000`）
3. 启动 `python -m project_lens.integrations.feishu.ws_ingress`（飞书长连接 → 本机事件 API）

常用参数：

| 参数 | 含义 | 默认 |
|------|------|------|
| `-ProjectLensRoot` | 本仓库路径 | 脚本所在仓库 |
| `-PythonExe` | Python 路径 | `PYTHON_EXE` / `project-lens` conda / `py -3.12` |
| `-SkipApiStart` | 不自动启动 uvicorn | 关 |

检查 API：

```text
GET http://127.0.0.1:8000/api/v1/integrations/feishu/status
```

期望：`configured=true`，`binding_count >= 1`，`agent_mode=hermes`。

WS 日志出现 `connected to wss://...` 后，在测试群 **@机器人** 发送：

```text
介绍一下这个项目
```

期望：

1. 先出现 **上下文预览卡**（「直接回答 / 编辑上下文 / 选择更多历史」）
2. 点 **「直接回答」** 后才开始 Hermes 回答
3. 「选择更多历史」可细选会话侧 / 群聊消息（群聊区依赖 `im:message.group_msg`）

群聊历史权限冒烟（可选）：

```powershell
$env:PROJECT_LENS_SMOKE_CHAT_ID = "oc_你的群"
.\scripts\smoke-feishu-group-history.ps1
```

---

## 5. 备选：HTTP 事件回调（需公网，生产常用）

本地联调**不必**走这条。若部署到有公网 HTTPS 的服务器：

1. 飞书「事件 / 回调」改为「发送至开发者服务器」
2. 请求地址：

```text
https://<你的域名>/api/v1/feishu/events
```

3. 完成 URL Verification；订阅消息事件 + 卡片回传
4. 只跑 `uvicorn`（**不要**再开本机长连接 ingress）

细节见 [`feishu-setup.md`](feishu-setup.md)。

---

## 6. 建议冒烟问题

```text
介绍一下这个项目
项目当前状态怎么样
负责人是谁
知识库还缺什么
```

期望：

- `@` 后先出预览卡，再「直接回答」有回复；
- 能区分事实 / 推断 / 未知（至少不胡编成「已确认」）；
- 未绑定的群没有项目上下文或被拒绝；
- 未在 `members` 中的用户只能看到公开资料；
- **不会**自动改代码、开 PR、部署。

---

## 7. 配置对照清单

- [ ] `.env` 已从 `.env.example` 复制，且未提交 Git
- [ ] `PROJECT_LENS_AGENT_MODE=hermes`
- [ ] `PROJECT_LENS_DATABASE_PATH` 不是 `:memory:`
- [ ] 飞书 App ID / Secret / Verification Token 已填
- [ ] `PROJECT_LENS_SERVICE_TOKEN` 已设
- [ ] bindings 的 `chat_id` = 真实测试群
- [ ] bindings 的 `project_id` 在 registry 与 `config/projects/` 中存在
- [ ] ProjectSpace `members` 含测试者 `open_id`（`ou_...`）
- [ ] 飞书事件+回调均为 **长连接**；已订消息事件与卡片回传
- [ ] 已发布含 `im:message.group_msg` 的版本（若要测群聊细选）
- [ ] 已 `pip install -e ".[feishu-ws]"`（或等价安装 `lark-oapi`）
- [ ] 真机问答已开 `MODEL_LIVE=true` 且模型 key 可用
- [ ] `GET /api/v1/integrations/feishu/status` 正常
- [ ] 已跑 `start-projectlens-feishu-ws.ps1`，且**没有**其它进程占用飞书长连接
- [ ] 提问时 @了机器人

---

## 8. 常见问题

**Q: status 里 `configured=false`？**  
A: App ID/Secret 为空时走 recording adapter，不会真发飞书。

**Q: @了机器人没出预览卡？**  
检查：飞书是否仍填着 HTTP 请求地址（应改长连接）；WS ingress 是否在跑且日志有 `connected`；是否另有 Hermes Gateway / 其它长连接在抢；`chat_id` / `tenant_key` 是否绑错。

**Q: 出了卡但点按钮没反应？**  
回调配置也要选长连接，并订阅 **卡片回传交互**（`card.action.trigger`）。权限页里找不到该名字是正常的——它在「回调」里，不在「权限」列表。

**Q: 「群聊发言」显示不可用？**  
开通并**发布** `im:message.group_msg`；可用 `smoke-feishu-group-history.ps1` 本机冒烟。

**Q: 回了但几乎看不到代码细节？**  
你的 `open_id` 可能不在 `members` 里，当前是 guest，只能看 `public_sources`。

**Q: 回了但像在答错项目？**  
bindings / ProjectSpace `feishu_chat_bindings` 不一致。一个群只能绑一个项目。

**Q: 只想本地验证、不接飞书？**  
保持 App 凭证为空，跑：

```powershell
py -3.12 -m pytest tests/test_feishu_http_and_acl.py tests/test_hermes_runtime.py tests/test_feishu_ws_ingress.py -q
```

---

## 9. 下一步

1. 换成自己的项目：[`projectspace-onboarding-guide.md`](projectspace-onboarding-guide.md)
2. 收紧群可见性与成员角色
3. 更多环境变量：[`pilot-launch-config.md`](pilot-launch-config.md)
