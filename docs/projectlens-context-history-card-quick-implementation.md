# ProjectLens 上下文历史编辑卡快速落地实施文档

版本：v0.1  
日期：2026-10-10  
状态：待实施  
适用范围：飞书上下文编辑卡；本方案只改善“可见性与历史选择”，不改变 Hermes 权限模型。

## 1. 目标

当前编辑卡将“最近压缩摘要、最近对话、引用资料”混在一起，用户无法判断摘要覆盖了哪些对话、最近每轮具体问答、更早历史是否可选，以及勾选项最终是否发送给 Hermes。

本次快速落地的目标：

1. 明确区分摘要、最近逐轮对话、历史对话和引用资料。
2. 默认展示最近逐轮对话的可读摘要，而不是只有短标题。
3. 保留分页，避免一次卡片承载全部历史。
4. 保证确认后的 `pending_send` 仍是 Hermes 唯一的会话历史来源。
5. 不改变现有压缩逻辑，不把完整历史直接塞入模型上下文。

## 2. 非目标

- 不改变会话压缩阈值和摘要算法；
- 不把项目记忆、Evidence 变成卡片勾选项；
- 不一次性加载全部飞书群消息；
- 不绕过 ACL、EffectiveAccessScope 或工具权限；
- 不重写 Hermes runtime；
- 不在本阶段实现完整历史永久归档表。

## 3. 当前实现基线

| 模块 | 当前职责 |
| --- | --- |
| `domain/conversation.py` | `recent_turns`、压缩摘要、引用、`pending_send` 和历史候选模型 |
| `application/conversation_service.py` | 刷新待发集合、分页历史、编辑暂存和确认 |
| `integrations/feishu/service.py` | 处理卡片动作并组装编辑卡数据 |
| `integrations/feishu/cards.py` | 渲染预览卡、编辑卡和更多历史卡 |
| `integrations/feishu/hermes_context.py` | 将确认后的待发集合渲染为 Hermes 上下文 |

当前默认上下文池由“最近一次压缩摘要 + `recent_turns`”组成。压缩后的旧轮次会进入 `compressed_turn_records`，但编辑卡目前主要显示一行摘要标题，因此用户看不到逐轮内容。

## 4. 第一版交互设计

编辑卡调整为四个区块：

```text
本次问题
当前已选：N 条 · 预计 N tokens

一、会话摘要
☑ 最近压缩摘要
   覆盖时间：2026-09-20 ～ 2026-10-08
   覆盖轮次：12 轮
   内容预览：...

二、最近对话
☑ 2026-10-08 · 发布影响
   用户：这次发布影响哪些服务？
   助手：涉及支付、订单和通知服务...

☑ 2026-10-07 · 支付超时问题排查
   用户：支付超时可能是什么原因？
   助手：主要检查连接池、网关和重试策略...

三、更早历史
[查看更早历史] [上一页] [下一页]

四、引用资料
☑ 事故复盘
☐ 数据库连接池调整
☐ 历史故障复盘

[确定修改] [返回预览]
```

原则：摘要是压缩后的上下文单元；最近对话是逐轮候选项；更早历史分页展示；引用资料单独分组；勾选变化在点击“确定修改”前不生效。

## 5. 数据契约调整

### 5.1 候选来源类型

在现有候选模型基础上明确：

```text
summary          最近压缩摘要
recent_turn      最近未压缩逐轮对话
historical_turn  从历史记录分页选择的旧对话
citation         会话引用资料
group_message    飞书群聊历史消息
```

第一版可复用 `PendingSendItem.source_kind`，先增加 `historical_turn`，不强制新建完整领域模型。

### 5.2 展示字段

每个候选项至少提供：

```text
item_id, source_kind, occurred_at, label,
user_question, answer_summary, run_id / citation_id,
mark, already_in_pending
```

`answer_summary` 必须限长；卡片展示摘要，Hermes 仍按现有上下文协议接收受控内容。

### 5.3 摘要覆盖信息

建议补充：

```text
summary_start_at
summary_end_at
compressed_turn_count
summary_preview
```

若暂时不修改 `DefaultContextItem`，可由 `ConversationService` 根据 `compressed_turn_records` 计算后传给 renderer。

## 6. 后端实施步骤

### 第一步：整理候选项

在 `ConversationService` 增加面向卡片的候选查询，例如：

```python
list_context_edit_candidates(
    session,
    *,
    offset: int = 0,
    page_size: int = 5,
    date_window: DateWindow | None = None,
) -> ContextEditCandidates
```

返回 `summary_item`、`recent_turn_items`、`historical_turn_items`、`joinable_citations`、`has_more` 和 `next_offset`。

保持 `list_default_context_items()` 的默认发送规则不变；最近逐轮项读取 `session.recent_turns` 并带上 `answer_summary`；压缩摘要读取最新 `compressed_turn_records`；更早历史优先复用 `list_session_history_candidates()` 的分页能力。

### 第二步：扩展服务层

修改 `FeishuEventService._context_edit_card_payload()`：

- 同时传递摘要、最近逐轮项、历史分页项和引用项；
- 保留现有暂存集合字段；
- 不在 renderer 内重新计算权限；
- 卡片按钮继续携带 `session_id` 和稳定 `item_id`。

### 第三步：重构 renderer

修改 `render_context_edit_card()`：

1. 将当前列表改成四个明确区块。
2. 逐轮项显示时间、标题、用户问题和回答摘要。
3. 摘要项显示覆盖轮数和时间范围。
4. 每个区块最多渲染 5 个操作按钮，避免超出飞书卡片限制。
5. 详细内容通过“查看详情”或“选择更多历史”进入下一张卡。
6. 所有长文本截断，不能让一条历史撑爆卡片。

### 第四步：扩展历史分页卡

修改 `render_context_more_history_card()` 及动作处理：每页 5 条，显示时间、问题摘要、回答摘要和已选状态；支持上一页、下一页、加入上下文、返回编辑卡；加入只更新编辑暂存，不启动 Hermes。

## 7. 发送语义与安全约束

1. `pending_send` 是本次发送集合的唯一事实来源。
2. 编辑卡“已选”不等于已经发送，必须点击“确定修改”。
3. 点击“直接回答”时，Hermes 使用确认后的 `pending_send`。
4. 未选历史不得因为默认 session dump 被隐式带入。
5. 历史查询必须基于当前租户、项目、群聊、用户和 `EffectiveAccessScope`。
6. `item_id`、`session_id`、`citation_id` 必须服务端重新校验。

## 8. 测试计划

### 单元测试

- 摘要项包含压缩轮数和覆盖时间；
- 最近逐轮项包含问题和回答摘要；
- 历史分页按时间倒序展示；
- 已选、待恢复、引用状态正确；
- 长问题和长答案不会超过卡片字段限制；
- 空摘要、空回答、空历史均有稳定展示。

### 集成测试

1. 进入编辑卡时同时看到摘要和最近逐轮对话。
2. 去掉一条最近对话后，预览卡和 `pending_send` 同步变化。
3. 从更早历史加入一条对话后，返回编辑卡显示为已选。
4. “返回预览”不会应用暂存变更。
5. “确定修改”后直接回答，Hermes 只收到确认后的条目。
6. 伪造 `item_id` 不能读取他人历史。
7. 重启后已有 session 的状态仍能展示。

推荐测试文件：

```text
tests/test_context_three_entries.py
tests/test_pending_send_preview.py
tests/test_conversation_fork.py
tests/test_feishu_integration.py
tests/test_feishu_http_and_acl.py
```

## 9. 验收标准

- 编辑卡能区分摘要和逐轮对话；
- 最近逐轮对话至少显示问题和回答摘要；
- 更早历史可以分页浏览和选择；
- 引用资料与对话历史分开显示；
- 卡片勾选结果与 Hermes 实际上下文一致；
- 不增加权限绕过路径；
- 现有分支、日期窗、引用和压缩测试不回归。

## 10. 后续正式版

快速版仍依赖 `ConversationSession` 中可用的轮次和压缩记录。正式版应增加独立的完整会话历史存储：

```text
conversation_history
- history_id
- session_id
- turn_index
- created_at
- user_question
- answer_summary
- answer_body_snapshot
- run_id
- evidence_ids
- compression_cycle
- access_scope
```

正式版完成后，压缩只影响默认上下文池，不再影响历史浏览和按轮选择；该工作不应阻塞本快速版实施。
