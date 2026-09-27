# Hermes `projectlens-safe` 配置样例

这些文件在 **ProjectLens** 仓库内，供复制到本机 Hermes，**不会**自动写入 `~/.hermes`，也**不会**改 Hermes 源码。

飞书联调总流程（预览卡 / 长连接）：[`docs/feishu-test-quickstart.md`](../../docs/feishu-test-quickstart.md)

> **说明：** 飞书群正式路径已改为 ProjectLens 自管长连接（`scripts/start-projectlens-feishu-ws.ps1`）。  
> 「直接回答」时 Hermes 在 ProjectLens 进程内跑工具循环。本节 profile 仍可用于 **CLI / MCP 调试**，不要再单独起 Hermes Feishu Gateway 抢长连接。

## 文件

| 文件 | 用途 |
|------|------|
| `projectlens-safe.config.yaml` | MCP server、飞书工具白名单、禁用危险 toolset、群路由 stub |
| `SOUL.projectlens-safe.md` | Hermes 人格：必须走 ProjectLens MCP，禁止编造项目事实 |

## 前置

1. 已安装 ProjectLens（含 MCP / 飞书 WS extra）：

```powershell
cd <ProjectLens 仓库根目录>
py -3.12 -m pip install -e ".[dev,mcp,feishu-ws]"
```

2. （可选）本机已有 Hermes Agent 仓库，用于 CLI 调试：

```powershell
py -3.12 -m hermes_cli.main --help
```

3. ProjectLens API 可访问（默认 `http://127.0.0.1:8000`）。

验证 MCP stdio（会阻塞在 stdin，Ctrl+C 结束）：

```powershell
py -3.12 -m project_lens.integrations.mcp.server
```

## 拷贝到 Hermes（手动，CLI/MCP 调试用）

1. 创建 profile 目录（按你的 Hermes 版本）：  
   `%LOCALAPPDATA%\hermes\profiles\projectlens-safe\`  
   或 `~/.hermes/profiles/projectlens-safe/`
2. 将 `projectlens-safe.config.yaml` 中的键合并进该 profile 的 `config.yaml`（或当前激活的 `%LOCALAPPDATA%\hermes\config.yaml`）。
3. 将 `SOUL.projectlens-safe.md` 内容写入 profile 的 `SOUL.md`（或等价人格文件）。
4. **必须改路径**：
   - `mcp_servers.projectlens.command` → 你的 Python 绝对路径（推荐）
   - `mcp_servers.projectlens.cwd` → ProjectLens 仓库绝对路径
5. 将 `profile_routes[].chat_id` 换成真实飞书群 `oc_...`（若仍用 profile 路由）。

样例 YAML 里的路径是占位符，不要直接用。

## 为什么不用默认 `hermes-feishu`？

`hermes-feishu` 常带 terminal / file 等完整工具。项目群若用该预设，可能绕过 ProjectLens Gateway。  
`projectlens-safe` 只白名单：

- `mcp-projectlens`（工具名形如 `mcp__projectlens__projectlens_ask_project`）
- `clarify`

并用 `agent.disabled_toolsets` 做纵深防御。

## 验收（手测）

飞书群：

```powershell
.\scripts\start-projectlens-feishu-ws.ps1
```

1. `@` 后出现上下文预览卡  
2. 「直接回答」走 ProjectLens 内 Hermes；有工具审计；`allow_apply=false`  
3. 改代码 / 部署 / 重启类请求被拒绝  

MCP / CLI 工具列表（若调试 profile）：

1. 出现 `mcp__projectlens__projectlens_ask_project`  
2. 项目问题走该工具，而不是 terminal/file  

自动化：`pytest tests/test_hermes_phase2_profile.py tests/test_feishu_ws_ingress.py -q`
