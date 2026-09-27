# ProjectLens

> ProjectLens 是一个以 ProjectSpace 为核心、以群聊为工作现场、以原始资料和证据为基础，帮助不同角色建立共同认知、核对项目事实并推进协作的 Project Copilot。

产品与研发共用总纲：[`docs/projectlens-product-document.md`](docs/projectlens-product-document.md)。

## 它解决什么

企业项目信息分散在文档、会议纪要、多维表格、代码、任务和发布系统中，带来查找成本、角色理解成本、上下文丢失成本和风险暴露成本。

ProjectLens 的目标不是替团队做所有决定，而是让团队更快获得**可追溯的项目上下文**，减少无效确认，并尽早暴露冲突与未知。

核心闭环：

```text
资料进入
-> 持续整理和关联
-> 形成带来源的项目上下文
-> 在群聊中核对事实
-> 暴露冲突和知识缺口
-> 形成决策和行动
-> 将确认结果重新沉淀回项目
```

## 职责边界

```text
飞书群聊 / HTTP
        |
   Hermes Runtime          意图理解、模型调用、工具循环、会话与呈现
        |
   ProjectLens Kernel      ProjectSpace、权限、检索、Evidence、记忆审批、校验与审计
        |
   项目资料                文档 / 会议纪要 / 多维表格 / GitHub / 任务与发布等
```

- **Hermes**：通用 Agent 运行时。不直接决定项目事实、权限或来源可信度。
- **ProjectLens**：项目智能内核。负责 ProjectSpace、角色与群聊可见性、证据链、冲突与知识缺口、记忆审批和回答校验。
- **RAG**：检索能力，不是产品边界；结构化状态优先走结构化查询。

权限在检索前进入上下文：

```text
用户角色权限 ∩ 群聊可见范围 ∩ 项目资源权限
```

## 当前 MVP 范围

第一阶段建议聚焦**一个真实项目**，接入三类来源和三类角色。

**来源**：飞书文档与会议纪要、一个飞书多维表格、一个 GitHub 仓库。

**角色**：研发、产品/业务、项目负责人或管理者。

**必须支持的场景**：项目介绍、当前状态、需求与实现核对、PR/提交/发布影响、负责人与阻塞、会议结论与待办、带来源回答、知识缺口、角色与群聊权限。

**明确不做**：自动改代码、自动创建/合并 PR、自动部署回滚、无审批写正式记忆、无边界全量群聊监听、跨全公司开放式搜索。

## 快速开始

```powershell
Copy-Item .env.example .env
# 编辑 .env：填入飞书 / Hermes / 项目绑定；密钥不要提交

py -3.12 -m pip install -e ".[dev,mcp,feishu-ws]"
py -3.12 -m pytest -q
py -3.12 -m uvicorn project_lens.main:app --reload
```

常用检查：

```text
GET /docs
GET /api/v1/health
GET /api/v1/ready
GET /api/v1/integrations/feishu/status
```

飞书群联调（推荐长连接，无需公网穿透）：

```powershell
.\scripts\start-projectlens-feishu-ws.ps1
```

开放平台「事件 / 回调」均选长连接；`@` 后先出上下文预览卡。细节见 [`docs/feishu-test-quickstart.md`](docs/feishu-test-quickstart.md)。

默认生产决策路径为 `PROJECT_LENS_AGENT_MODE=hermes`。可执行配置剖面见 [`docs/pilot-launch-config.md`](docs/pilot-launch-config.md)。Hermes profile（CLI/MCP 调试）见 [`config/hermes/README.md`](config/hermes/README.md)。

## 项目接入

更换项目应更换 **ProjectSpace** 配置，而不是修改 Agent 逻辑。

1. 在 `config/projects/` 新增项目 manifest（成员、来源、权限、群聊绑定）。
2. 在 `.env` 的 `PROJECT_LENS_LOCAL_PROJECT_REGISTRY` / `PROJECT_LENS_FEISHU_PROJECT_BINDINGS` 中注册并绑定。
3. 用 inspect API 确认 ProjectSpace 与 EffectiveAccessScope。

完整步骤：[`docs/projectspace-onboarding-guide.md`](docs/projectspace-onboarding-guide.md)。

## 文档地图

详见 [`docs/README.md`](docs/README.md)。常用入口：

| 文档 | 用途 |
|------|------|
| [`docs/projectlens-product-document.md`](docs/projectlens-product-document.md) | 产品总纲（唯一产品真相） |
| [`docs/feishu-test-quickstart.md`](docs/feishu-test-quickstart.md) | 飞书测试快速上手（新人） |
| [`docs/feishu-setup.md`](docs/feishu-setup.md) | 飞书联调清单 |
| [`docs/pilot-launch-config.md`](docs/pilot-launch-config.md) | Stub / Safe Live / 测试群配置 |
| [`docs/projectspace-onboarding-guide.md`](docs/projectspace-onboarding-guide.md) | 新项目接入 |

规划稿与历史蓝图以产品总纲为准；冲突时以 `projectlens-product-document.md` 覆盖。
