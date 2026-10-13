# ProjectLens 飞书多角色群聊测试资产

本目录用于在真实飞书测试群中回放多个项目成员连续对话。测试目标不是逐条问答，而是验证 ProjectLens 能否理解一段真实的产品、研发、测试、项目管理协作过程。

## 文件

| 文件 | 用途 |
|---|---|
| `roles.json` | 虚拟成员、角色和测试 actor 配置 |
| `01_requirement_change.json` | 需求变更讨论剧本 |
| `02_release_risk_review.json` | 发布风险评审剧本 |
| `03_decision_memory.json` | 会议结论与长期记忆剧本 |
| `execution-log.template.jsonl` | 每条消息和回答的执行记录模板 |
| `defect.template.csv` | 缺陷记录模板 |

## 执行约定

三个剧本统一使用方式 A：

```text
/sim <role> <message>
```

示例：

```text
/sim product 支付失败重试需要从两次改成三次。
/sim developer 当前代码只支持两次。
```

`/sim` 已作为测试环境专用入口接入，但默认关闭。需要在 `.env` 中配置：

```text
PROJECT_LENS_FEISHU_VIRTUAL_MEMBERS_ENABLED=true
PROJECT_LENS_FEISHU_VIRTUAL_MEMBER_OPERATORS=REPLACE_WITH_REAL_FEISHU_OPEN_ID
```

只允许白名单中的真实账号在已绑定测试群使用，生产环境不应开启。

## 手动执行

1. 启动 ProjectLens API 和 Hermes Gateway。
2. 确认飞书测试群绑定当前 `projectlens` ProjectSpace。
3. 确认虚拟 actor 已写入 ProjectSpace，并且测试身份功能只对测试账号开启。
4. 选择一个剧本，按 `messages[].sequence` 顺序发送消息。
5. 在 `interventions` 节点发送 ProjectLens 问题。
6. 保存机器人回复、Run ID、Trace ID、引用和截图。
7. 对照 `expected` 和 `acceptance_checks` 记录通过或缺陷。
8. 每个剧本结束后关闭或重置会话，再执行下一个剧本。

## 真实身份与虚拟身份

测试记录必须同时记录：

- `physical_actor_id`：真实飞书发送者的 `open_id`；
- `effective_actor_id`：本条消息模拟的 ProjectLens actor；
- `chat_id`：真实测试群；
- `scenario_id` 和 `sequence`：剧本及消息顺序。

虚拟身份只能测试 ProjectLens 的角色和资源权限，不能替代真实飞书成员资格、通讯录权限或真实发送者测试。

## 结果文件

建议将实际执行结果另存为：

```text
evaluation/feishu_team_test/results/<date>-<scenario-id>.jsonl
```

不要把飞书密钥、模型 API Key、未脱敏敏感文档内容写入结果文件。
