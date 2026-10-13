# Knowledge Gap Workflow 收尾计划

**更新时间**：2026-09-27  
**适用范围**：ProjectLens Knowledge Gap Daily Digest Workflow  
**关联设计**：[knowledge-gap-daily-digest-workflow.md](knowledge-gap-daily-digest-workflow.md)

## 1. 当前结论

Knowledge Gap Workflow 的核心 MVP 已经落地，可以完成：

```text
问答/扫描发现候选
-> 授权校验和 Evidence 过滤
-> 稳定 key 去重与观测聚合
-> 生命周期状态管理
-> 项目摘要和责任人视图
-> Feishu 卡片按钮
-> 长连接接收卡片动作
-> SQLite 持久化和通知幂等
```

当前仍未完全收口的重点是：**真实授权检索器接入自动复查、资料更新触发与任务队列、定时摘要、责任人映射、生产级长连接生命周期和端到端验收**。

## 2. 已完成能力

### 2.1 领域和存储

- `KnowledgeGapCase`、`KnowledgeGapObservation`、`KnowledgeGapStateEvent`；
- 稳定 canonical key、重复观测聚合、严重性不降级；
- `candidate/open/acknowledged/assigned/waiting_for_source/resolved_pending_check/resolved/reopened/snoozed/dismissed` 状态机；
- SQLite 和 InMemory store；
- Evidence 授权过滤，混合可见/不可见引用按保守策略隐藏。

### 2.2 摘要和通知

- 项目级每日摘要；
- 责任人级摘要；
- 摘要 hash 和同周期幂等；
- 内容变化时刷新摘要投递状态；
- `notification_key` 去重记录；
- 项目群卡片和负责人私信发送入口。

### 2.3 Feishu 交互

- 卡片按钮：认领、请求复查、暂缓、误报；
- `card.action.trigger` 处理；
- 长连接接收按钮事件；
- 可信 `operator.open_id`、租户和项目隔离校验；
- 操作结果回写卡片/会话。

### 2.4 API 和动作

- 摘要预览 API；
- 缺口动作创建 API；
- 动作状态更新 API；
- 缺口生命周期 transition API；
- `GapAction` 持久化和状态流转校验。

### 2.5 本轮新增收口

- `KnowledgeGapService` 增加公开 case/action 查询、通知 claim 和 recheck audit 接口；
- API、问答通知和 Feishu action 的既有 store 私有访问改用公开接口；
- 新增 `KnowledgeGapRecheck` 审计实体和 SQLite v2 migration；
- 新增可注入 verifier 的 `KnowledgeGapRecheckService`，覆盖完整授权 Evidence 后解决、冲突重开、结果不足/异常继续 pending 和跨项目拒绝；
- **真实 Hermes verifier 尚未接入应用容器，Feishu 按钮暂不可执行真实复查。**当前 recheck service 为待接运行时 adapter 的 application 能力。

## 3. 必须完成的收尾项

### P0：自动复查服务

新增 `KnowledgeGapRecheckService`，至少包含：

- 根据 `gap_id` 读取原始问题、Evidence 引用和项目范围；
- 重新调用当前授权检索/问答验证链路；
- 检查新答案是否有完整 Evidence；
- 区分 `resolved`、`reopened`、继续等待资料；
- 保存复查记录：触发来源、旧状态、新状态、Evidence、结果和时间；
- 复查失败时保留 `resolved_pending_check`，不能伪装成已解决；
- 复查过程必须使用当前 actor/project scope，不能复用过期权限。

建议接口：

```python
recheck(gap_id, *, actor, project, trigger, now) -> GapRecheckResult
```

当前进度：服务、审计表和单元测试已完成。待将现有异步 `HermesRuntimeService` 封装为 verifier，并通过当前 Feishu actor 的实时 ProjectSpace scope 执行；在该 adapter 注入前，不能宣称按钮已可自动复查。

验收标准：

- 补充有效资料后进入 `resolved`；
- 资料撤销、权限不足或答案仍冲突时不关闭；
- 关闭后的新问题再次证明缺口存在时进入 `reopened`。

### P0：资料更新触发复查

在 Feishu 文档同步、Evidence 写入或资料版本变更后：

```text
source/evidence updated
-> 找到引用该资料的开放缺口
-> 排队执行 recheck
-> 更新状态和通知
```

必须具备：

- 通过 Evidence ID 或 source ref 反查相关缺口；
- 单次资料更新的复查任务幂等；
- 复查失败可重试；
- 不因资料更新直接把缺口标记为 resolved。

当前实现状态（2026-09-27）：已完成基础闭环。Feishu 文档成功同步后按稳定 `feishu_doc:<token>` source ref 查找开放/待复查缺口，并以 `project + source_ref + revision + gap_id` 生成幂等任务；任务持久化到 SQLite/InMemory，失败保留 `failed` 并可重试。任务执行必须由当前 Feishu actor 的实时 `EffectiveAccessScope` 调度，普通资料同步线程不会直接关闭缺口。Feishu 卡片复查入口会先将 `waiting_for_source/reopened` 转为 `resolved_pending_check`，再执行授权复查。

### P0：真正的每日调度

增加一个可控的 digest runner：

- 按项目时区计算上一周期；
- 生成项目摘要和责任人摘要；
- 先 claim 通知 key，再发送消息；
- 发送失败写入 `failed`，支持重试；
- 同一摘要 hash 和 recipient 不重复发送；
- 支持手动 dry-run、测试模式和暂停开关；
- 启动时不默认发送历史摘要。

当前实现状态（2026-09-28）：已新增 `KnowledgeGapDigestRunner`。runner 按配置时区计算上一完整日周期，支持手动 `run_once(dry_run=True)`，通知发送前按 gap/recipient/period claim，重复执行不会重复投递，项目群或负责人投递失败会将 digest run 标记为 `failed`。应用级自动启用仍需接入责任人目录和项目群 resolver，避免在没有可信 Feishu 映射时误发私信。

补充收口：runner 已接入 FastAPI lifespan，只有显式开启 `feishu_knowledge_gap_digest_enabled` 才会启动；项目绑定支持 `owner_mappings`，缺失映射的负责人只保留项目群摘要，不发送私信。默认配置仍关闭自动调度。

### P1：责任人目录和 Feishu ID 映射

不要永久假设 `suggested_owner_ids == Feishu open_id`。需要增加：

- 项目责任人目录；
- 内部 user ID -> Feishu `open_id` 映射；
- 映射不存在时只发项目群摘要，不发送失败私信；
- 映射变更审计；
- 责任人无权查看 Evidence 时不得出现在其私信中。

当前实现状态（2026-09-28）：项目绑定支持显式 `owner_mappings`（内部 owner ID -> Feishu `open_id`），digest runner 仅使用该映射发送私信；未映射负责人会被跳过，项目群摘要仍可发送。Evidence 仍先经项目授权过滤。

## 4. 工程收尾项

### P1：长连接生命周期

- 将 SDK client、线程和 event loop 作为明确的应用资源；
- FastAPI shutdown 时停止接收、关闭 websocket、回收线程；
- 连接异常自动重连并记录状态；
- `/integrations/feishu/status` 增加 `long_connection_status`、最后连接时间和最近错误；
- 避免直接依赖 SDK 私有属性，封装适配器。

当前实现状态（2026-09-28）：digest runner 已纳入 FastAPI lifespan；Feishu status endpoint 现在暴露 digest runner 与长连接的启用状态、运行状态、最后连接时间和最近错误。长连接仍依赖 SDK 的连接实现，后续可继续补充自动重连策略，但 shutdown 和运行可观测性已具备。

### P1：移除内部耦合

将以下直接访问改为公开接口：

- `service._store.get(...)` -> `KnowledgeGapService.get_case(...)`；
- `service._store.get_action(...)` -> `KnowledgeGapService.get_action(...)`；
- 通知 claim、项目归属和动作授权集中到 application service。

### P1：统一错误和审计

- 所有 Feishu action 使用统一错误码；
- 记录 actor、tenant、project、gap、action、event_id；
- 重复事件返回明确的 duplicate 结果；
- 不把 Evidence 内容写入日志或错误响应。

## 5. 测试和验收

### 单元测试

- canonical key 对日期、大小写和空白归一化；
- unknowns/conflicts 自动生成候选；
- 混合 Evidence ACL 隔离；
- notification claim 幂等；
- action 和 gap transition 非法跳转；
- recheck 的 resolved/reopened/blocked 分支；
- owner ID 映射缺失和越权。

### 集成测试

- ProjectAgentAsk -> KnowledgeGapService -> SQLite；
- 自动生成后项目群摘要和责任人私信；
- Feishu 长连接 card action -> 状态/动作更新；
- 重复点击不重复发送；
- 跨租户、跨项目、无权限 actor 被拒绝；
- 资料同步 -> recheck -> 状态变化。

### 手工验收场景

1. 在飞书群中提出一个无法由现有资料回答的问题。
2. 确认生成一条知识缺口并收到项目群摘要。
3. 确认责任人收到私信，且看不到未授权 Evidence。
4. 点击“认领”，确认动作记录和状态变化。
5. 点击“请求复查”，补充资料后确认自动进入 `resolved`。
6. 撤销资料或制造新冲突，确认进入 `reopened`。
7. 重复点击和重复运行摘要，确认不产生重复消息。

## 6. 实施顺序

```text
1. 抽取公开 KnowledgeGapService 查询接口
2. 实现 KnowledgeGapRecheckService
3. 接入 Evidence/source 更新事件
4. 完成 digest runner 和失败重试
5. 接入责任人目录与 Feishu ID 映射
6. 加固长连接 shutdown/reconnect/status
7. 补齐端到端测试和手工验收脚本
8. 再评估 RiskFinding 关联和 Phase 4 发布门禁
```

## 7. 完成定义

当以下条件全部满足时，Knowledge Gap Workflow 才算真正收口：

```text
问答或扫描发现缺口
-> 缺口持久化并去重
-> 项目群和责任人收到各自可见摘要
-> 用户可以认领、补充、暂缓和误报
-> 资料更新自动触发复查
-> 复查结果决定 resolved / reopened
-> 通知可重试且不重复
-> 长连接可启动、重连和优雅关闭
-> ACL、审计和端到端测试全部通过
```

## 8. 当前不纳入范围

- 自动修改正式需求；
- 自动创建外部任务或 PR；
- 未经审批自动发布或部署；
- 绕过 ProjectSpace 权限读取受限资料；
- 在没有完整验证前启用生产级自动关闭。
