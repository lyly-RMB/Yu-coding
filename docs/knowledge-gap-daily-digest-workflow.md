# Knowledge Gap Daily Digest Workflow

**状态**：已实现，持续补充测试
**版本**：V1.1
**更新时间**：2026-10-14
**适用产品**：ProjectLens 项目协作 Agent

## 1. 目的

本 Workflow 用于把 ProjectLens 在日常问答、资料扫描和项目状态检查中发现的知识缺口，转化为可治理、可分配、可复查的项目协作事项。

它解决的问题不是“每天发送一份 Agent 失败日志”，而是：

```text
Agent 发现资料缺失/冲突/过期
-> 形成结构化缺口候选
-> 去重、聚合、判断影响和责任人
-> 按项目生成每日摘要
-> 分层通知管理者和资料责任人
-> 补充资料或确认结论
-> 自动重新检索和验证
-> 关闭、保留或重新打开缺口
```

核心目标是让项目团队知道：

- 哪些问题目前无法可靠回答；
- 无法回答的原因是什么；
- 它会影响需求、开发、测试、发布还是决策；
- 应由谁补充什么资料或确认什么结论；
- 补充资料后，系统是否真的能够回答这个问题。

## 1.1 当前实现状态

当前代码已经覆盖以下闭环：

| 能力 | 当前行为 |
|---|---|
| 飞书问答发现 | 从 `answer.unknowns`、工具结果或回答正文中的知识缺口信号生成 `missing_source` 候选 |
| 持久化 | 保存 `KnowledgeGapCase`、`KnowledgeGapObservation`，按 canonical key 聚合重复观察 |
| 无证据缺口 | `missing_source` 可以在没有授权 Evidence 时入库，并标记 `evidence_status=no_authorized_evidence` |
| 即时通知 | 创建/更新缺口后，按 `owner_mappings` 解析负责人 `open_id`，直接发送负责人私聊卡片 |
| 详细卡片 | 展示项目、周期、标题、原始问题、缺口说明、严重程度、状态、出现次数、证据状态、影响、建议资料类型和关联对象 |
| 周期摘要 | `KnowledgeGapDigestRunner` 按配置周期发送项目摘要和负责人摘要；默认测试配置可使用 60 秒间隔 |
| 去重 | `knowledge_gap_notifications` 记录通知认领，避免同一周期同一收件人重复发送 |
| 旧数据库 | `knowledge_gap` v4 迁移会补建通知表和索引 |

即时私发和周期 Digest 是两条独立路径：即时私发用于让负责人尽快看到新缺口，Digest 用于汇总新增、持续、重新打开和已关闭信息。飞书机器人可以使用 `open_id` 主动发起私聊，不要求用户预先创建单聊窗口。

## 1.2 当前限制

- 默认测试负责人是项目绑定中的 `owner_mappings`，没有映射时缺口仍会入库，但不会发送负责人私聊。
- 发送失败不会阻断原始问答；服务会记录异常，后续可通过新的观察或 Digest 重试。
- `access_limited`、`unclear_question` 不会按普通 `missing_source` 通知负责人。
- 详细卡片当前以单个负责人视角展示，不包含受限 Evidence 正文，只展示授权范围内的证据数量和状态。

## 2. 产品价值

### 2.1 从被动问答变成主动治理

普通问答 Agent 只在用户提问时响应。Knowledge Gap Workflow 会把反复出现的未知、冲突和过期资料变成项目治理事项，帮助团队持续改善项目共同上下文。

### 2.2 减少重复沟通

如果同一个问题一天被多人反复询问，系统不应该每次都重新回答“目前不知道”，而应该聚合为一条缺口：

```text
问题：会员等级规则是否已经生效？
出现次数：4 次
涉及角色：产品、研发、QA
冲突来源：会议纪要 vs 需求多维表
影响：开发拆分、测试验收和发布日期
```

### 2.3 提前暴露项目风险

知识缺口本身不一定是风险，但高影响、长期未处理或跨角色反复出现的缺口，通常是项目风险的早期信号。

## 3. 设计原则

1. **回答不上不等于知识缺口**：问题不清楚、用户无权限或查询范围错误不能直接创建缺口。
2. **候选和正式分离**：模型只能提出缺口候选，ProjectLens 校验后才进入持久化缺口池。
3. **原始资料不可被覆盖**：补充资料必须作为新版本或新来源写入，不能修改历史 Evidence。
4. **缺口必须可行动**：每条正式缺口都应说明缺什么、影响什么、建议找谁处理。
5. **通知必须去噪**：只通知新增、严重度变化、反复出现或需要升级的缺口。
6. **补充后必须复查**：用户点击“已处理”不能直接关闭，系统要重新验证是否已经能够回答。
7. **权限先于治理**：只能基于当前运行身份可访问的资料创建和展示缺口，不能通过缺口摘要泄露受限内容。
8. **管理者看趋势，责任人看行动**：不同收件人收到不同粒度的通知。

## 4. 缺口类型

一次回答或扫描可能产生以下类型的缺口：

| 类型 | 含义 | 示例 |
|---|---|---|
| `missing_source` | 应有资料不存在或未接入 | 找不到正式验收记录 |
| `conflicting_sources` | 多个来源对同一事实结论不一致 | 会议纪要与需求表状态不同 |
| `stale_source` | 来源可能已经过期 | Wiki 仍引用旧版本规则 |
| `missing_owner` | 事项没有明确负责人 | 需求有人提议但无人负责推进 |
| `missing_decision` | 已有讨论但没有正式确认 | 会议讨论了方案但没有决策记录 |
| `missing_schedule` | 有需求或任务但缺开发/测试安排 | 需求已确认但没有 Issue 或 QA 计划 |
| `missing_acceptance` | 缺少验收标准或测试结论 | QA 无法判断是否可以发布 |
| `access_limited` | 可能存在资料，但当前身份无权访问 | 不能把它当作普通知识缺口 |
| `unclear_question` | 用户问题缺少必要上下文 | 缺项目、版本或业务范围 |

其中 `access_limited` 和 `unclear_question` 默认不进入管理者每日知识缺口摘要，而是通过当前回答提示用户补充上下文或申请权限。

## 5. 缺口生命周期

### 5.1 状态

```text
candidate
open
acknowledged
assigned
waiting_for_source
resolved_pending_check
resolved
snoozed
dismissed
reopened
```

### 5.2 状态含义

| 状态 | 含义 |
|---|---|
| `candidate` | Agent 或规则引擎刚发现，尚未完成校验 |
| `open` | 已确认是可治理的项目知识缺口 |
| `acknowledged` | 负责人已看到，但尚未承诺处理方式 |
| `assigned` | 已确定处理人或处理团队 |
| `waiting_for_source` | 等待文档、表格、会议确认或测试记录 |
| `resolved_pending_check` | 用户声明已处理，等待系统重新验证 |
| `resolved` | 重新验证通过，缺口关闭 |
| `snoozed` | 暂时不处理，保留唤醒时间和原因 |
| `dismissed` | 明确不属于项目范围或不应补充，必须记录原因 |
| `reopened` | 已关闭但新资料或新问题证明缺口再次出现 |

### 5.3 允许的主要转换

```text
candidate -> open
candidate -> dismissed
open -> acknowledged -> assigned -> waiting_for_source
waiting_for_source -> resolved_pending_check
resolved_pending_check -> resolved
resolved_pending_check -> reopened
open/assigned -> snoozed -> open
resolved -> reopened
```

所有状态变化必须记录操作人、时间、原因和相关 Evidence/资料引用。

## 6. 核心数据模型

现有 `KnowledgeGapReport` 是基于当前证据生成的只读报告，适合即时查询；本 Workflow 需要新增持久化对象。

### 6.1 KnowledgeGapCase

```text
KnowledgeGapCase
- id
- tenant_id
- project_id
- canonical_key
- type
- title
- question
- description
- fact_type
- status
- severity
- impact
- target_ref
- evidence_ids
- conflicting_source_ids
- suggested_source_type
- suggested_owner_ids
- assigned_owner_ids
- first_detected_at
- last_seen_at
- last_notified_at
- next_review_at
- occurrence_count
- affected_question_count
- resolution_note
- resolved_by
- resolved_at
- dismissed_reason
- created_at
- updated_at
```

### 6.2 KnowledgeGapObservation

每次问答或扫描对缺口的观察单独保存，不直接覆盖缺口案例。

```text
KnowledgeGapObservation
- id
- gap_id
- run_id
- project_id
- observed_at
- trigger_type       answer / scheduled_scan / wiki_compile / sync
- question_hash
- evidence_ids
- signal
- visibility_scope
- created_by          agent / rule / user
```

这样可以回答：

- 该缺口最早什么时候出现；
- 一天被问了多少次；
- 哪些角色受到影响；
- 是问答发现的，还是定时扫描发现的；
- 关闭后是否又重新出现。

### 6.3 GapAction

用于跟踪处理动作，不把“建议”混在缺口本身中。

```text
GapAction
- id
- gap_id
- action_type       add_source / confirm_decision / assign_owner / update_wiki / link_task
- proposed_by
- target_owner_id
- description
- status             proposed / accepted / completed / rejected
- linked_source_ids
- linked_task_id
- created_at
- completed_at
```

## 7. 缺口生成流程

### 7.1 从 Agent 回答生成候选

回答契约增加以下字段：

```text
Answer
- facts
- inferences
- conflicts
- unknowns
- missing_evidence
- suggested_gap_actions
```

模型可以输出：

```json
{
  "type": "missing_acceptance",
  "question": "订单导出是否已经完成 QA 验收？",
  "description": "已找到开发完成记录，但未找到可授权的 QA 验收记录。",
  "impact": "无法判断是否可以进入发布",
  "suggested_source_type": "test_record",
  "suggested_owner_ids": ["qa-owner"]
}
```

### 7.2 ProjectLens 校验

候选缺口进入持久化前必须经过确定性校验：

1. `project_id` 是否与当前 ProjectSpace 一致。
2. 所有 Evidence 是否属于当前项目和有效权限范围。
3. 是否已经检索过候选缺口所对应的来源类型。
4. 是否属于权限受限，而不是资料缺失。
5. 是否属于问题不清晰，需要追问。
6. 是否已经存在同一 `canonical_key` 的缺口案例。
7. 是否有足够证据支持“缺失、冲突或过期”的判断。

### 7.3 定时扫描生成候选

每日扫描不依赖用户提问，直接检查：

- 需求是否有开发和测试安排；
- 任务是否有负责人和截止时间；
- 会议决策是否已经落入正式资料；
- Wiki 草稿是否引用过期资料；
- 发布前是否缺少验收、测试或负责人信息；
- 不同来源是否对同一事实产生冲突。

定时扫描必须只使用已授权的后台项目作用域，并在观测记录中写入 `visibility_scope`。

## 8. 去重与聚合

同一个缺口可能被多个问题、多个角色和多个来源重复发现。系统需要生成稳定的 `canonical_key`。

建议构成：

```text
sha256(
  tenant_id
  + project_id
  + gap_type
  + fact_type
  + normalized_target_ref
  + normalized_question_intent
)
```

规范化规则：

- 去除时间、礼貌词和角色特有表达；
- 保留项目、模块、需求、版本等实体；
- 优先使用结构化 `target_ref`，不要只依赖自然语言相似度；
- 相似度只能作为候选合并信号，不能单独决定合并。

合并时：

- 增加 `occurrence_count`；
- 更新 `last_seen_at`；
- 追加新的 Evidence 和问题引用；
- 保留不同来源的冲突信息；
- 取最高严重度，但不能自动降低已确认的高严重度；
- 重新计算责任人和影响范围。

## 9. 严重度和通知优先级

### 9.1 严重度

```text
low
medium
high
critical
```

严重度建议根据以下因素确定：

```text
severity = impact × recurrence × deadline_proximity × uncertainty
```

实际实现可以先使用确定性规则：

- 影响正式需求、发布或生产事故：至少 `high`；
- 只影响文档完整性：`low` 或 `medium`；
- 连续多日重复出现：提升一级，但不超过 `critical`；
- 影响本周里程碑或发布门禁：提升一级；
- 仅因权限不足：不进入普通严重度计算。

### 9.2 通知事件

只在以下情况触发通知：

- 新增高影响缺口；
- 同一缺口当天重复出现超过阈值；
- 严重度上升；
- 缺口进入升级时间点；
- 缺口重新打开；
- 缺口被验证关闭。

当前飞书问答路径会在缺口创建后立即私发配置的负责人；普通周期任务仍通过每日摘要汇总发送，避免在没有负责人映射时向无关用户打扰。

## 10. 每日摘要 Workflow

### 10.1 触发

默认每个工作日 17:30 按项目运行一次。时间应可按 ProjectSpace 配置，并支持手动触发和测试模式运行。

### 10.2 执行步骤

```text
1. 读取过去一个摘要周期内的 observations
2. 查询项目状态为 open/acknowledged/assigned/waiting 的 gap cases
3. 合并当天新增、重复、升级、关闭和重新打开事件
4. 过滤用户无权查看的内容
5. 计算项目级摘要和责任人级摘要
6. 应用通知去重和静默规则
7. 生成 Feishu 文本/卡片
8. 发送到项目负责人和责任人
9. 记录发送结果、摘要哈希和通知事件
```

### 10.3 管理者摘要

管理者看到项目级视图：

```text
今日项目知识缺口摘要

新增：3
持续未解决：5
高影响：2
重复出现最多：会员等级规则

1. 高影响：会员等级规则存在冲突
   依据：会议纪要、需求多维表
   影响：开发拆分、QA 验收、发布时间
   建议负责人：产品负责人
   建议动作：确认生效版本并更新正式需求

2. 中影响：订单导出缺少 QA 验收记录
   已出现：4 次
   影响：无法确认发布门禁
   建议负责人：QA 负责人
```

### 10.4 责任人摘要

责任人只看到自己有权处理的缺口：

- 缺口标题和问题；
- 为什么被发现；
- 影响范围；
- 建议补充的资料类型；
- 原始依据入口；
- 认领、补充、关联已有资料、暂缓和误报反馈。

## 11. 处理和解决闭环

### 11.1 处理动作

不同类型的缺口对应不同的解决路径：

| 缺口 | Agent 建议 | 人工确认动作 |
|---|---|---|
| 缺正式需求 | 指出缺失字段和关联讨论 | 产品更新需求文档或多维表 |
| 会议结论未落档 | 生成决策草案 | 会议负责人确认并发布 |
| 开发状态不明 | 检查 Issue、PR、提交 | 研发确认任务或代码状态 |
| 缺测试记录 | 生成待补测试清单 | QA 补验收或测试结论 |
| 来源冲突 | 对比版本和时间 | 有权限负责人确认生效来源 |
| 负责人不明 | 提供候选负责人及依据 | 项目负责人确认归属 |

### 11.2 自动复查

当出现以下事件时重新验证：

- 关联文档或多维表发生同步更新；
- GitHub Issue/PR/Commit 发生变化；
- Wiki 草稿重新编译；
- 用户点击“已补充资料”；
- 到达 `next_review_at`；
- 管理者手动点击“重新检查”。

复查流程：

```text
读取缺口原始问题
-> 使用新的授权证据重新检索
-> 重新判断缺失/冲突/过期状态
-> 生成验证结果
-> resolved 或 reopened
-> 发送状态变化通知
```

不能仅凭用户点击“完成”关闭缺口。

## 12. 权限与安全

### 12.1 检索权限

缺口发现、聚合和摘要都必须使用：

```text
ProjectSpace scope
∩ background service scope
∩ source ACL
```

### 12.2 摘要权限

管理者可以看到项目级趋势，但不自动获得所有技术源文件、日志或私密会议内容。摘要只应包含当前收件人有权看到的证据和行动信息。

### 12.3 权限受限的特殊处理

如果 Agent 发现“可能存在资料但当前用户无权访问”：

- 不创建普通 `missing_source`；
- 可以创建 `access_limited` 观测；
- 回答中只说明当前权限不足；
- 不在管理者摘要中泄露受限来源标题或内容；
- 需要通过权限申请流程处理。

## 13. 与现有 ProjectLens 的对应关系

### 13.1 可复用能力

- `ContextEngine.source_gaps()`：来源缺失、冲突、过期检测的基础；
- `KnowledgeGapReport`：即时诊断结果；
- `SourceRecordStore`：原始资料、版本和权限元数据；
- `WikiDraftCompiler`：资料更新后的 Wiki 草稿生成；
- `WikiDraftWorkflowService`：草稿审核和发布；
- `RiskEngine` 与风险通知：可复用调度、去重和通知思想；
- `FeishuDocSyncScheduler`：资料同步后的触发机制；
- `LifecycleBus` / `AgentRun`：运行审计和事件链；
- `ProjectSpace` / `EffectiveAccessScope`：项目和权限边界；
- 现有 Evidence、Memory、Approval 和 replay 测试基础。

### 13.2 需要新增能力

- 持久化 `KnowledgeGapCase`；
- 持久化 `KnowledgeGapObservation`；
- 缺口去重和聚合服务；
- 缺口状态机和处理动作；
- 每日摘要生成器；
- 缺口通知策略和通知记录；
- 补充资料后的自动复查器；
- 缺口反馈、误报和关闭原因；
- 缺口趋势与项目质量指标。

### 13.3 不建议直接复用的地方

不要直接把 `KnowledgeGapReport.gaps` 当作通知列表，因为它：

- 没有稳定 ID；
- 没有生命周期；
- 没有历史出现次数；
- 没有责任人状态；
- 没有通知去重；
- 没有关闭后的自动复查。

## 14. 建议的模块划分

```text
domain/knowledge_gap.py
    KnowledgeGapCase / Observation / Action / 状态和枚举

application/knowledge_gap_service.py
    候选校验、去重、聚合、状态转换、复查

application/knowledge_gap_digest.py
    周期摘要、角色摘要、通知事件生成

application/knowledge_gap_scheduler.py
    定时触发、手动触发、失败重试和运行状态

persistence/knowledge_gap_store.py
    SQLite 持久化，未来可替换 PostgreSQL

integrations/feishu/knowledge_gap_cards.py
    摘要卡片、认领、补充资料、暂缓、关闭和误报反馈

api/knowledge_gap_routes.py
    查询、认领、状态转换、手动复查和管理员摘要
```

## 15. 可靠性和幂等

### 15.1 幂等键

摘要运行使用：

```text
digest_key = project_id + period_start + period_end + audience_scope
```

同一 `digest_key` 只能产生一条摘要记录，重复执行只能重试发送或补写失败状态。

### 15.2 通知去重

使用：

```text
notification_key = gap_id + recipient_id + event_type + period
```

同一周期内不重复发送相同事件。

### 15.3 失败处理

- 缺口入库成功但消息发送失败：保留 `pending` 通知，下次重试；
- 摘要生成失败：不改变缺口状态，记录运行失败；
- 单个项目失败：不能阻塞其他项目；
- 外部资料同步失败：缺口可以保留，但要标记资料新鲜度异常；
- 关闭复查失败：保持 `resolved_pending_check`，不能伪装为已解决。

## 16. 评估指标

### 16.1 缺口识别质量

- 缺口候选被人工确认的比例；
- 缺口类型分类准确率；
- 同一问题去重准确率；
- 冲突来源识别准确率；
- 权限受限误判为资料缺失的比例。

### 16.2 治理效果

- 缺口平均解决时间；
- 高影响缺口在发布前关闭的比例；
- 重复问题数量下降比例；
- 知识缺口重复出现率；
- 补充资料后自动复查通过率。

### 16.3 通知质量

- 摘要打开率；
- 认领率；
- 误报率；
- 被静音或忽略的比例；
- 重复通知率；
- 越权内容泄露数，必须为 0。

## 17. 分阶段实现

### Phase 1：候选和持久化

- 定义 `KnowledgeGapCase`、`Observation` 和状态机；
- 从 `ProjectAnswer.unknowns/conflicts` 生成候选；
- 复用 `ContextEngine.source_gaps()`；
- 实现稳定 key、去重和出现次数；
- 增加 SQLite store 和单元测试。

### Phase 2：每日摘要

- 实现按项目的周期聚合；
- 添加项目负责人目录和建议责任人；
- 生成管理者摘要和责任人摘要；
- 复用 Feishu messenger 和幂等事件模式；
- 支持手动运行和测试模式。

### Phase 3：处理闭环

- 飞书卡片支持认领、关联资料、暂缓、误报和请求复查；
- 增加 GapAction；
- 接入 Wiki 草稿和资料更新事件；
- 实现自动复查和 reopened。

当前代码已落地第一版处理动作闭环：`KnowledgeGapAction` 支持提议、接受、完成、拒绝状态，动作可持久化到 SQLite，并通过 Project Agent API 创建和更新；真实 Feishu 卡片交互与自动复查仍作为后续接入点。

### Phase 4：主动风险关联

- 将高影响、长期未解决、重复出现的缺口关联到 RiskFinding；
- 在发布前检查中纳入缺口门禁；
- 增加项目知识质量趋势和管理视图。

## 18. MVP 验收场景

### 场景 A：同一缺口被多人重复询问

系统应合并为一个 `KnowledgeGapCase`，累计出现次数，并在每日摘要中只展示一次。

### 场景 B：资料冲突

系统应同时展示更可信来源、冲突来源、更新时间和需要确认的责任人，不得直接选择一个来源掩盖冲突。

### 场景 C：补充资料后关闭

用户补充 QA 记录后，系统重新检索并通过验证，缺口才进入 `resolved`。

### 场景 D：用户无权限

系统不能把受限资料误判为普通缺口，也不能通过摘要泄露受限内容。

### 场景 E：通知失败重试

缺口状态和摘要记录必须保留，消息发送失败可重试，不重复创建缺口或重复发送已成功通知。

### 场景 F：关闭后重新出现

新版本资料或新问题证明缺口仍然存在时，系统将其标记为 `reopened`，重新进入通知策略。

## 19. 完成定义

第一版 Workflow 完成的标准是：

```text
问答或扫描发现缺口
-> 缺口持久化并可去重
-> 每日按项目生成摘要
-> 管理者和责任人收到各自可见范围内的通知
-> 用户可以认领和补充资料
-> 系统自动复查
-> 缺口正确关闭或重新打开
```

它不要求第一版自动修改正式需求、自动创建任务或自动更新 Wiki。第一版要先证明：ProjectLens 能把“答不上来”变成一个可追踪、可负责、可验证的项目改进事项。
