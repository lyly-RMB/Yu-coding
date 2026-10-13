# ProjectLens 知识闭环整改与测试方案

**版本**：V1.0  
**日期**：2026-10-13  
**适用范围**：ProjectLens 当前代码、Feishu 群聊、Evidence 检索、Knowledge Gap、Memory Proposal 和 Obsidian 派生知识链路

## 1. 文档结论

当前项目已经具备知识闭环的主要组件，但还不能判定为“已完成验收”。现状更准确的描述是：

```text
资料接入 -> Evidence -> ACL 检索 -> Hermes 回答 -> 引用校验
                                  |
                                  v
                         Unknown / Conflict
                                  |
                                  v
                         Knowledge Gap 持久化
                                  |
                                  v
                  补充资料 -> Recheck -> resolved/reopened
```

旁路包括：

```text
Hermes/人工提案 -> MemoryProposal -> 审批 -> ProjectMemory
来源镜像 -> Obsidian Wiki/Inbox -> 审阅后再进入正式知识
```

代码中已有不少闭环服务和测试，但真实 Feishu 链路、异步响应契约、角色视图、记忆审批和评测数据仍存在阻断项。

## 2. 当前基线

### 2.1 已确认的测试结果

2026-10-13 在当前工作区执行：

```text
pytest -q
871 passed, 2 warnings
```

P1 修复后的自动化回归已全绿；剩余 2 个 warning 为依赖弃用提示，不构成当前功能失败。真实 Feishu 群聊链路和 Knowledge Gap 复查仍需在真实配置与数据下由人工执行。

### 2.2 当前评测门禁

| 项目 | 当前情况 | 发布要求 |
|---|---|---|
| 检索黄金集 | 12 条，报告为 `insufficient_data` | 至少 100 条 |
| 上下文压缩集 | 2 条 | 至少 50 条 |
| 安全集 | 尚未形成最终门禁报告 | 至少 30 条 |
| 端到端问答集 | 尚未完成完整报告 | 至少 50 条 |
| Feishu 剧本 | 剧本一历史上曾因 P1 中止 | 三套剧本完整通过 |

### 2.3 已确认或高风险问题

| 编号 | 优先级 | 问题 | 影响 |
|---|---|---|---|
| KL-001 | P1 | 多角色群聊历史上按 `effective_actor_id` 拆分会话，导致总结看不到其他角色发言 | 知识上下文不完整，角色协作结论错误 |
| KL-002 | P1 | 全量测试仍有 16 个失败 | 说明跨模块契约尚未稳定 |
| KL-003 | P1 | Feishu 异步响应在部分路径返回 `pending/session_id`，测试和调用方期望 `run_id` | 运行追踪、重试和审计链断裂 |
| KL-004 | P1 | Hermes 空 Evidence/fallback 和工具重新绑定测试失败 | 无证据回答可能无法恢复，真实模型链路不稳定 |
| KL-005 | P1 | 记忆提案卡片和审批动作测试失败 | “提案 -> 审批 -> 正式记忆”闭环不可验收 |
| KL-006 | P1 | API 与 Feishu 卡片的角色视图/事实引用不一致 | 同一事实在不同出口表现不一致 |
| KL-007 | P2 | 检索评测样本不足，vector/hybrid 尚未证明优于 lexical | 无法量化引用正确性和召回质量 |
| KL-008 | P2 | 文档同步和知识缺口摘要默认关闭 | 资料更新不一定自动触发复查和通知 |
| KL-009 | P2 | Obsidian 是派生/提案层，不是正式事实层 | 页面存在不能代表知识已审批生效 |
| KL-010 | P2 | 过期、撤销、权限变化后的复查需要真实链路回归 | 可能继续使用失效 Evidence |

## 3. 修复方案

### 3.1 P1：先恢复全量测试稳定性

执行顺序：

1. 按失败模块分组修复，不修改测试使其“适配失败行为”。
2. 统一 Feishu ingress 的响应契约：同步完成返回 `run_id`，异步返回 `pending=true`、`session_id`、可查询的 `run_id` 或明确的状态查询地址。
3. Hermes runner 统一处理空 Evidence、工具异常、SSE 超时和插件工具重绑定；失败时必须返回可审计的降级结果，不把无证据文本标成已验证事实。
4. 恢复 Memory Proposal 卡片按钮、审批/拒绝/idempotency 和权限校验。
5. API envelope、Feishu card 和纯文本共用同一 `audience_view`，只在展示层做角色裁剪，不改变事实 ID 和 Evidence ID。
6. 修复 SQLite memory route 测试依赖的公共查询接口，禁止业务代码直接依赖 store 私有字段。
7. 明确 Obsidian export feature flag；默认关闭时必须返回 503 或统一的 disabled 状态，开启后才允许导出。

验收：`pytest -q` 0 failed；至少新增回归测试覆盖每个已修复失败类别。

### 3.2 P1：固定群聊知识上下文键

规则：

```text
session binding = tenant_id + chat_id + physical_actor_id（或明确的群级 binding）
effective_actor_id = 只用于角色权限、角色视图和审计
ConversationTurn.user_id = 保留 virtual/effective actor，便于还原发言角色
```

禁止：使用 `effective_actor_id` 单独创建多个彼此隔离的群聊 session。

验收：同一真实发送者、同一群聊中的 product/developer/qa `/sim` 消息和普通总结共享同一 session；跨群、跨租户、跨项目不能共享。

### 3.3 P1：补齐 Knowledge Gap 复查闭环

标准流程：

```text
source/evidence revision 更新
-> 根据 source_ref 或 evidence_id 找到开放 gap
-> 生成幂等 recheck job
-> 使用当前 actor + EffectiveAccessScope 执行复查
-> 完整授权 Evidence 且可回答：resolved
-> 仍冲突、资料不足、权限不足或验证异常：pending/reopened
-> 记录审计、失败原因、重试次数和通知状态
```

约束：

- 资料更新本身不能直接把 gap 标为 `resolved`；
- 复查失败必须保留任务并可重试；
- 重新执行相同 `project + source_ref + revision + gap_id` 不得重复创建任务；
- 关闭后的新冲突必须进入 `reopened`；
- 复查必须使用当前权限，不能复用历史 Evidence 授权。

### 3.4 P2：明确默认开关和运行态验证

默认关闭的功能必须在健康检查或 Feishu status 中明确展示：

- `feishu_doc_sync_on_startup`；
- `feishu_knowledge_gap_digest_enabled`；
- vector/hybrid retrieval；
- Obsidian export；
- `/sim` virtual members。

测试环境可以显式开启，但测试报告必须记录配置快照、Git revision、数据集 hash 和时间。

### 3.5 P2：扩充评测集并冻结门禁

至少补齐：

- 检索 100 条：需求、代码、会议、任务、发布、风险、冲突、过期、拒答；
- 上下文压缩 50 条：决策、引用、任务、负面约束、长对话、多角色；
- 安全集 30 条：跨项目、跨租户、角色越权、撤销、过期、敏感字段；
- 端到端 50 条：普通问答、三角色总结、知识缺口、审批、资料更新和重启。

发布门禁：`cross_scope_leakage=0`、`revoked_expired_misuse=0`，引用正确性、时效正确性、冲突召回和拒答精度均不低于 0.95；低于 0.90 直接阻断。

## 4. 测试用例

### 4.1 单元与契约测试

| ID | 场景 | 操作 | 预期 |
|---|---|---|---|
| UT-01 | 会话绑定 | 同一 chat 用三个 virtual actor 发送 `/sim` | session_id 相同；turn 保留各自 actor |
| UT-02 | 会话隔离 | 不同 chat 或 tenant 发送同一问题 | 不共享 session、摘要或引用 |
| UT-03 | 异步响应契约 | 触发慢模型路径 | 返回 pending、session_id 和可追踪 run 信息 |
| UT-04 | 空 Evidence | 检索返回空证据 | 输出 unknown/待确认，不生成 verified fact |
| UT-05 | 工具异常 | 工具超时或抛异常 | 有限重试；最终结果可审计且不伪造引用 |
| UT-06 | 引用绑定 | 使用无效 citation id | 不进入已验证 facts，正文仍可展示边界说明 |
| UT-07 | 角色视图 | 同一 ProjectAnswer 渲染四种角色 | Evidence/fact ID 一致，展示字段按角色裁剪 |
| UT-08 | Memory Proposal | 创建、审批、拒绝、重复点击 | 未审批不生效；重复动作幂等；越权拒绝 |
| UT-09 | Obsidian 开关 | 关闭 export 后调用 API | 返回明确 disabled/503，不写 Vault |
| UT-10 | 过期 Evidence | 将来源标记 expired/revoked 后重新查询 | 不作为当前事实引用 |

### 4.2 Knowledge Gap 集成测试

| ID | 场景 | 操作 | 预期 |
|---|---|---|---|
| KG-01 | 缺口生成 | 问一个无资料问题 | 生成 candidate/open，记录问题、原因和项目 |
| KG-02 | 重复聚合 | 同一问题连续提问 | canonical key 相同，出现次数增加，不创建重复 gap |
| KG-03 | 资料补充 | 写入新 Evidence revision | 创建一个幂等 recheck job |
| KG-04 | 复查解决 | 新 Evidence 完整回答原问题 | gap 进入 resolved，写入 recheck audit |
| KG-05 | 复查失败 | verifier 超时/异常 | 保持 pending，job 标记 failed，可重试 |
| KG-06 | 冲突重开 | 新资料与正式来源冲突 | gap 进入 reopened，不得静默关闭 |
| KG-07 | 权限变化 | 新 Evidence 对当前 actor 不可见 | 不得 resolved；记录权限不足 |
| KG-08 | 重复资料事件 | 重复发送同一 revision | 不重复执行、不重复通知 |
| KG-09 | 摘要幂等 | 同一周期多次运行 digest | 相同 hash/recipient 只发送一次 |
| KG-10 | 失败通知 | 模拟 Feishu 发送失败 | 记录 failed，后续可重试且不重复成功消息 |

### 4.3 Feishu 端到端测试

| ID | 场景 | 操作 | 预期 |
|---|---|---|---|
| E2E-01 | 需求变更 | product -> developer -> qa -> 普通总结 | 总结包含三角色发言、确认项、未知项和引用 |
| E2E-02 | 发布风险 | 输入需求、代码、测试和发布资料 | 区分事实、冲突、风险和待确认项 |
| E2E-03 | 决策记忆 | 生成提案但不审批 | 再次查询不得把提案当正式事实 |
| E2E-04 | 审批生效 | 授权角色审批提案 | 写入来源、操作者、时间、hash 和审计；再次查询命中新记忆 |
| E2E-05 | 撤销记忆 | 撤销已审批记忆 | 后续查询不再使用该记忆 |
| E2E-06 | 重复事件 | 重放同一 Feishu event_id | 不重复回答、不重复写入、不重复通知 |
| E2E-07 | 服务重启 | 处理中重启 API，再继续提问 | session、gap、proposal、recheck job 可恢复 |
| E2E-08 | 跨项目访问 | actor 使用另一个 project_id 查询 | 返回拒绝或 not found，不泄露 Evidence 摘要 |
| E2E-09 | 资料更新 | Feishu 文档 revision 变化 | 自动或手动触发 recheck，状态正确变化 |
| E2E-10 | 长回答 | Hermes 返回多段/三角色正文 | 卡片、文本、API 正文一致，不静默截断 |

### 4.4 检索与安全评测

每条样例必须记录：`case_id`、问题、actor、tenant、project、时间范围、允许 Evidence、禁止 Evidence、期望答案、期望引用、是否应拒答。

重点负向用例：

1. 只在另一个项目存在的相同关键词；
2. 已撤销来源仍含旧结论；
3. 过期来源与当前来源冲突；
4. 当前角色只能看摘要，不能看原文；
5. 查询参数伪造 tenant/project/actor；
6. 混合可见和不可见 Evidence；
7. vector 召回高相似但无关内容；
8. 无证据问题要求系统给出确定结论。

## 5. 执行顺序与交付物

### 阶段 A：恢复基础稳定性

- 修复 16 个 pytest 失败；
- 生成失败分类和修复记录；
- `pytest -q` 全绿；
- 运行 `compileall`、Ruff、`git diff --check`。

### 阶段 B：验证核心闭环

- 从干净会话完整跑三套 Feishu 剧本；
- 保存执行日志、Run ID、Trace ID、引用、截图；
- 完成 KG-01 至 KG-10。

### 阶段 C：补齐数据和安全门禁

- 扩充四类评测集；
- 生成带 Git revision 和 dataset hash 的报告；
- 完成跨项目、撤销、过期、角色越权测试。

### 阶段 D：发布评审

只有同时满足以下条件才允许小范围试运行：

- P0/P1 缺陷为 0；
- 全量自动化测试通过；
- 三套 Feishu 剧本完整通过；
- cross-scope leakage 和 revoked/expired misuse 均为 0；
- 评测报告不再是 `insufficient_data`；
- 未审批 MemoryProposal 写入正式记忆次数为 0；
- 关键配置、模型、数据库和测试数据已冻结。

## 6. 测试记录模板

每条用例至少记录：

```text
case_id:
run_id:
trace_id:
git_revision:
config_snapshot:
actor_id / physical_actor_id / effective_actor_id:
tenant_id / project_id / chat_id:
input:
expected:
actual:
evidence_ids:
knowledge_gap_id:
memory_proposal_id:
status: passed | failed | blocked | not_run
defect_id:
notes:
```

## 7. 当前测试结论

截至 2026-10-13，项目应标记为：

```text
状态：整改中，禁止宣称知识闭环已验收
阻断：16 个自动化测试失败；真实 Feishu 三剧本未完成无阻断回归；评测数据不足
下一步：先修 P1，再跑 E2E 和 Knowledge Gap 复查，最后扩充评测集
```
