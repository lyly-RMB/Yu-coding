# ProjectLens Hermes 上下文压缩快速落地开发文档

**版本**：v0.1  
**日期**：2026-09-21  
**状态**：快速实施方案  
**适用范围**：飞书会话、ProjectLens 会话上下文、Hermes 工具循环、模型请求失败恢复

## 1. 文档目的

本文用于把 Hermes 上下文压缩机制快速落地到 ProjectLens，优先解决长期飞书会话导致的：

- 请求上下文持续增长；
- 工具结果重复堆积；
- 压缩阈值没有及时触发；
- provider 超时、连接失败后重复发送超大请求；
- Hermes 完整 transcript 与 ProjectLens 结构化摘要不一致。

本方案的核心原则是：

> ProjectLens 管理业务上下文，Hermes 负责模型调用和工具循环；Hermes 不再承担完整业务历史的长期存储职责。

## 2. 当前问题与根因

### 2.1 已观察到的问题

一次真实故障中，单次请求约为 `198,133 tokens`。虽然 Hermes 把 `gpt-5.6-luna` 识别为约 `1,050,000` token 上下文模型，但默认压缩阈值为 50%，约 `525,000 tokens`，因此没有触发压缩。请求随后连续重试，最终返回 provider failure。

### 2.2 根因

1. 压缩阈值按模型最大上下文计算，而不是按项目可接受预算计算。
2. 预算没有同时统计历史、工具 schema、工具结果、输出预留和 provider overhead。
3. 完整工具结果直接进入 Hermes transcript，重复搜索结果和完整文件内容长期累积。
4. ProjectLens 的最近轮次摘要无法缩短 Hermes 已经持久化的完整历史。
5. 请求失败后直接重试原 payload，没有先裁剪、压缩或轮换会话。

## 3. 快速落地目标

### 3.1 MVP 必须实现

1. 记录每次请求的上下文组成和 token 估算。
2. 工具结果统一转换为短 EvidenceCard，完整内容留在 ProjectLens。
3. 增加 Context Governor，在发送 Hermes 前执行预算检查和分层裁剪。
4. 使用锚定式增量摘要，不重复重写已有摘要。
5. 大上下文请求失败时，禁止原 payload 重试，先裁剪或轮换会话。
6. 支持会话轮换，并用 handoff summary 恢复工作连续性。
7. 增加压缩质量评测和生产监控。

### 3.2 本期不做

- 不重写 Hermes 内核的全部 transcript 存储机制；
- 不把所有历史永久压缩成一个不可解释的 opaque blob；
- 不删除飞书原始聊天、文件、Evidence 或项目记忆；
- 不因为压缩而改变 ACL、审批和 Evidence 的事实来源规则；
- 不一次性开发完整的上下文管理器 UI。

## 4. 目标架构

```text
飞书消息
  -> ConversationService
  -> Context Governor
       - 任务状态
       - Evidence 工作集
       - 结构化会话摘要
       - 最近对话窗口
       - 工具 schema 与输出预算
  -> HermesRuntimeService
  -> Hermes tool loop
  -> 模型回答
```

ProjectLens 发送给 Hermes 的内容只包含当前任务需要的最小工作集。完整文件、完整工具响应和历史 transcript 仍保存在 ProjectLens 或原始系统中，需要时通过 `evidence_id`、`source_key` 和 `revision` 重新读取。

## 5. 上下文分层

每次请求按以下优先级组装，越靠前越不能被裁剪：

| 层级 | 内容 | 保留策略 |
|---|---|---|
| L0 | 系统约束、工具 schema、权限范围 | 固定保留，不做摘要 |
| L1 | 当前任务状态、目标、阻塞项、下一步 | 固定保留，结构化存储 |
| L2 | 当前 Evidence 工作集、来源版本、冲突 | 优先保留摘要和引用 |
| L3 | 项目会话锚定摘要、已确认决策 | 增量合并，固定保留关键字段 |
| L4 | 最近对话和最近工具调用 | 滑动窗口，优先保留 |
| L5 | 旧对话、重复工具结果、完整文件内容 | 默认裁剪，按需回查 |

### 5.1 必须长期保留的内容

- 项目状态和当前任务目标；
- 已确认事实、冲突、未知项；
- 已做决策及决策原因；
- 下一步和阻塞项；
- `evidence_id`、`source_key`、`revision`、`content_hash`；
- 当前权限范围和有效期；
- 最近一次 handoff summary。

### 5.2 不应长期保留的内容

- 完整工具返回原文；
- 重复的搜索结果；
- 已经读取过的完整文件内容；
- 全部历史聊天 transcript；
- 已失效或旧版本的 source 内容；
- 无法追溯来源的自由文本结论。

## 6. Token 预算与触发规则

不要使用模型最大上下文作为 ProjectLens 的实际预算。第一版建议把 Hermes profile 的业务上下文上限设为 `128000`，并按以下公式估算：

```text
estimated_input =
  history_tokens
  + tool_schema_tokens
  + current_tool_result_tokens
  + system_prompt_tokens
  + output_reserve
  + provider_overhead
```

建议默认配置：

```yaml
hermes:
  model_context_length: 128000
  compression:
    soft_limit_ratio: 0.45
    summarize_ratio: 0.60
    aggressive_prune_ratio: 0.75
    rotate_ratio: 0.85
    hard_limit_ratio: 0.95
    output_reserve_tokens: 12000
    max_retry_after_recovery: 1
```

### 6.1 分级动作

| 使用率 | 动作 |
|---:|---|
| `<45%` | 正常发送，记录观测数据 |
| `45%-60%` | 清理重复工具结果，截断过长 EvidenceCard |
| `60%-75%` | 生成增量摘要，移除旧对话和完整工具原文 |
| `75%-85%` | 激进裁剪，仅保留 L0-L3、最近关键轮次和引用 |
| `85%-95%` | 轮换 Hermes 会话，使用 handoff summary 重建请求 |
| `>=95%` | 禁止原请求发送；强制压缩，失败则返回可恢复错误 |

## 7. EvidenceCard：工具结果裁剪格式

工具不再把完整结果直接写入 Hermes 上下文。统一转换为：

```json
{
  "evidence_id": "ev_123",
  "source_key": "obsidian/project-a/design.md",
  "revision": "rev_20260921_01",
  "content_hash": "sha256:...",
  "source_type": "obsidian",
  "authority": "approved",
  "status": "current",
  "short_summary": "当前项目采用事件驱动同步，失败任务进入重试队列。",
  "key_facts": [
    "重试上限为 3 次",
    "失败后需要人工审批"
  ],
  "conflicts": [],
  "detail_pointer": "projectlens://evidence/ev_123"
}
```

规则：

1. 摘要必须保留来源身份和版本，不允许只返回无来源的自然语言。
2. 需要完整内容时，Hermes 调用详情工具按 `evidence_id` 读取。
3. 同一 `source_key + revision` 在一次请求中只保留一份。
4. 旧 revision 默认只保留差异或冲突说明。
5. ACL 过滤必须在生成 EvidenceCard 前完成。

## 8. 锚定式增量摘要

摘要采用固定结构，不使用每次从头生成的全量摘要。建议结构：

```markdown
## Session Intent
当前任务目标和用户约束

## Current State
已完成、进行中、阻塞项

## Confirmed Facts
带 evidence_id 和 revision 的确认事实

## Decisions
决策、原因、替代方案和时间

## Open Questions
仍未确认的问题

## Files And Artifacts
创建、修改、读取过的文件和关键函数

## Next Steps
下一步动作及其依赖

## Compression Metadata
压缩轮次、覆盖消息范围、生成时间
```

压缩流程：

1. 第一次触发时，只摘要即将被移出窗口的历史片段。
2. 后续触发时，只处理新增被移出的片段。
3. 与旧摘要按文件路径、Evidence ID、决策 ID 去重合并。
4. 已有摘要不可被新摘要无依据覆盖；冲突必须进入 `Open Questions` 或 `Conflicts`。
5. 原始历史仍保留在 ProjectLens，摘要只作为 Hermes 工作上下文。

## 9. ProjectSessionState 数据结构

建议在现有 `ConversationSession` 旁增加结构化状态：

```python
class ProjectSessionState:
    session_id: str
    project_id: str
    actor_id: str
    task_intent: str
    current_status: str
    confirmed_facts: list[FactRef]
    decisions: list[DecisionRecord]
    open_questions: list[str]
    blocked_items: list[str]
    evidence_refs: list[EvidenceRef]
    artifact_refs: list[ArtifactRef]
    summary_markdown: str
    summary_version: int
    last_compressed_turn_id: str | None
    last_handoff_id: str | None
    updated_at: datetime
```

实现要求：

- `summary_version` 单调递增；
- `last_compressed_turn_id` 用于保证增量摘要不重复处理；
- Evidence 引用必须带 revision 和 content hash；
- 状态更新需要记录事件，不能只覆盖一段自由文本；
- 项目状态与 Hermes transcript 解耦，Hermes 会话轮换不能丢失状态。

## 10. Context Governor 接口

建议新增 `src/project_lens/context/governor.py`：

```python
class ContextGovernor:
    def estimate_tokens(self, context: ContextInput) -> TokenBudgetReport: ...
    def prepare_context(self, context: ContextInput) -> PreparedContext: ...
    def should_prune(self, report: TokenBudgetReport) -> bool: ...
    def should_summarize(self, report: TokenBudgetReport) -> bool: ...
    def should_rotate(self, report: TokenBudgetReport) -> bool: ...
    def build_handoff(self, state: ProjectSessionState) -> HandoffSummary: ...
```

`prepare_context()` 必须返回：

- 最终发送给 Hermes 的消息；
- 被裁剪的内容清单；
- 使用的摘要版本；
- EvidenceCard 清单；
- token 预算报告；
- 是否需要会话轮换；
- 可审计的 `context_snapshot_id`。

## 11. Hermes 与 ProjectLens 职责边界

### ProjectLens 负责

- 业务上下文生命周期；
- 会话状态、摘要和 Evidence 工作集；
- ACL、来源版本和事实追溯；
- token 预算、裁剪和会话轮换决策；
- handoff summary 和审计；
- 压缩质量评测。

### Hermes 负责

- 模型调用；
- 工具 schema 和工具循环；
- 工具调用协议；
- provider 错误返回；
- 在收到已裁剪上下文后的执行。

Hermes 的全局 compression 配置仍保留作为最后一道防线，但不能作为 ProjectLens 的主要上下文治理机制。建议同步设置：

```yaml
model:
  context_length: 128000
compression:
  threshold: 0.5
```

## 12. 失败恢复策略

禁止对超大原 payload 直接重试。

```text
请求失败
  -> 分类错误
  -> 计算当前上下文使用率
  -> 若上下文 > 60%，先裁剪/增量摘要
  -> 若仍 > 85%，轮换 Hermes 会话
  -> 注入 handoff summary 和当前 EvidenceCard
  -> 最多重试一次
```

错误处理：

| 错误 | 处理 |
|---|---|
| `400` | 修正参数或移除不兼容字段，不重复原请求 |
| `401` | 立即停止并提示凭据/权限问题 |
| `402` | 立即停止，不自动重试 |
| `429` | 指数退避，保留裁剪后的 payload |
| `5xx`、超时、`APIConnectionError` | 先按上下文大小裁剪或轮换，再重试一次 |
| 上下文仍超限 | 返回可恢复错误，提示重新开始任务会话 |

## 13. 分阶段实施

### Phase 1：请求观测与基线

**目标**：先知道上下文是由什么组成的。

**开发内容**：

- 增加 `TokenBudgetReport`；
- 记录历史、schema、工具结果、系统提示、输出预留和 provider overhead；
- 记录压缩前后 token、重试次数、错误类型和会话是否轮换；
- 增加日志脱敏，禁止记录 API key、完整文件内容和完整用户消息。

**验收**：每次 Hermes 请求都能回答“总 token 从哪里来、是否触发裁剪、最终发送了多少”。

### Phase 2：工具结果裁剪

**目标**：阻止完整工具结果污染长期上下文。

**开发内容**：

- 实现 EvidenceCard；
- 改造搜索、详情、文件读取和历史查询工具的输出适配器；
- ProjectLens 保存完整结果，Hermes 只接收摘要、关键事实和引用；
- 同源同版本去重。

**验收**：典型工具调用链上下文下降 50% 以上，且能根据 `evidence_id` 重新读取原文。

### Phase 3：结构化会话状态与增量摘要

**目标**：让任务连续性不依赖完整 transcript。

**开发内容**：

- 增加 `ProjectSessionState`；
- 实现固定章节摘要；
- 记录文件、函数、错误码、Evidence ID 和决策；
- 以 `last_compressed_turn_id` 实现锚定式增量摘要。

**验收**：轮换 Hermes 会话后，使用摘要可以继续当前任务，不需要重新阅读全部历史。

### Phase 4：Context Governor

**目标**：在请求进入 Hermes 前统一做预算决策。

**开发内容**：

- 实现五层上下文组装；
- 实现 45%、60%、75%、85%、95% 分级动作；
- 生成不可变 `context_snapshot_id`；
- 预览和实际发送复用同一 prepared context。

**验收**：所有请求在硬上限前完成裁剪或轮换，不再依赖模型最大上下文兜底。

### Phase 5：会话轮换与 handoff

**目标**：避免 Hermes transcript 无限增长。

**开发内容**：

- 设计 Hermes session rotation；
- 生成 handoff summary；
- 新会话注入摘要、当前状态和 EvidenceCard；
- 记录旧会话与新会话的关联关系。

**验收**：达到 85% 使用率时可无感轮换；新会话知道当前目标、决策、阻塞项和下一步。

### Phase 6：失败恢复

**目标**：解决 provider failure、超时和连接失败后的错误重试。

**开发内容**：

- 错误分类器；
- 裁剪后重试；
- 轮换后重试；
- 一次性恢复上限；
- 用户可理解的错误提示和诊断 ID。

**验收**：上下文过大时不再连续重发原 payload；恢复失败时可以明确告诉用户下一步操作。

### Phase 7：评测、灰度与回滚

**目标**：验证压缩没有换来“看似省 token、实际丢任务”的问题。

**开发内容**：

- 建立压缩前后回放集；
- 增加 probe 评测；
- 先在测试飞书群灰度；
- 保留旧路径 feature flag；
- 观察一周后再扩大范围。

**验收**：达到第 14 节的质量门禁，且可通过配置关闭新 Governor。

## 14. 评测体系与验收指标

### 14.1 必测维度

| 维度 | 测试问题 |
|---|---|
| 事实准确性 | 原始错误码、文件路径和版本是否保留 |
| 上下文连续性 | 轮换会话后能否继续下一步 |
| Artifact Trail | 是否知道修改过哪些文件和函数 |
| 决策保留 | 是否保留已确认方案及原因 |
| 引用正确性 | 回答中的 Evidence ID 和 revision 是否真实 |
| 权限安全 | 压缩和检索后是否出现越权内容 |
| 成本效率 | token、延迟、重试次数是否下降 |

### 14.2 Probe 测试

每个回放样本至少包含：

1. `Recall Probe`：询问原始错误、文件路径、函数名；
2. `Artifact Probe`：询问已修改、已读取和未修改的文件；
3. `Decision Probe`：询问最终决策及原因；
4. `Continuation Probe`：询问下一步和当前阻塞项；
5. `Citation Probe`：核对 Evidence ID、revision 和来源；
6. `Negative Probe`：询问未提供或已失效内容，验证模型不会猜测。

### 14.3 MVP 建议门禁

- 压缩后关键事实准确率不低于压缩前基线的 98%；
- 文件路径、函数名、错误码和 Evidence ID 保留率不低于 99%；
- 轮换会话后的任务继续成功率不低于 95%；
- 越权内容泄露率为 0；
- P95 输入 token 降低 40% 以上；
- 超大上下文原 payload 重试次数为 0；
- 因压缩导致的重复读取率不超过 10%。

## 15. 配置与数据变更

### 15.1 配置

建议新增：

```env
PROJECTLENS_CONTEXT_GOVERNOR_ENABLED=true
PROJECTLENS_CONTEXT_MODEL_LIMIT=128000
PROJECTLENS_CONTEXT_SOFT_LIMIT_RATIO=0.45
PROJECTLENS_CONTEXT_SUMMARIZE_RATIO=0.60
PROJECTLENS_CONTEXT_PRUNE_RATIO=0.75
PROJECTLENS_CONTEXT_ROTATE_RATIO=0.85
PROJECTLENS_CONTEXT_HARD_LIMIT_RATIO=0.95
PROJECTLENS_CONTEXT_MAX_RECOVERY_RETRY=1
```

### 15.2 数据表或持久化对象

至少增加以下对象：

- `project_session_state`：结构化任务状态和摘要版本；
- `context_budget_report`：每次请求的预算观测；
- `context_snapshot`：实际发送内容的不可变记录；
- `handoff_summary`：会话轮换后的交接内容；
- `context_compression_event`：压缩、裁剪、轮换和恢复事件。

所有对象必须带 `project_id`、`actor_id`、权限范围和创建时间。

## 16. 测试清单

### 单元测试

- token 预算计算；
- EvidenceCard 截断、去重和引用完整性；
- 摘要增量合并和冲突保留；
- 分级阈值动作；
- 错误分类和重试上限。

### 集成测试

- 搜索结果转换为 EvidenceCard；
- 工具详情按 `evidence_id` 回查；
- Context Governor 与 HermesRuntimeService 联调；
- 会话轮换后注入 handoff summary；
- Feishu 群长会话连续请求。

### 回放测试

- 198k token 级别的历史输入；
- 重复搜索和重复文件读取；
- 多次压缩；
- 5xx、超时和连接失败；
- ACL 边界和过期 Evidence；
- 用户要求回溯历史但当前上下文未包含该历史。

## 17. 回滚策略

所有新能力必须受 feature flag 控制：

```env
PROJECTLENS_CONTEXT_GOVERNOR_ENABLED=false
```

回滚时保留：

- 原始 ConversationSession；
- 原始 Evidence 和工具结果存储；
- 已产生的 snapshot、摘要和审计数据；
- Hermes 旧 profile 配置。

禁止通过回滚删除会话状态或原始证据。若新压缩逻辑出现质量问题，只关闭发送路径上的 Governor，并保留观测和回放数据。

## 18. 第一周执行顺序

1. 第 1 天：加入 token 预算日志和错误分类，建立故障基线。
2. 第 2 天：实现 EvidenceCard 和工具结果适配器。
3. 第 3 天：实现 `ProjectSessionState` 和增量摘要。
4. 第 4 天：接入 Context Governor 和分级阈值。
5. 第 5 天：实现会话轮换、handoff 和失败恢复。
6. 第 6 天：跑长会话回放、Probe 和 ACL 测试。
7. 第 7 天：测试群灰度，确认指标后再扩大范围。

## 19. 完成定义

本方案完成的判断标准：


- ProjectLens 可以在 Hermes 请求前估算完整上下文预算；
- 工具结果默认以 EvidenceCard 进入模型上下文；
- 压缩摘要包含任务、状态、事实、决策、文件和下一步；
- 上下文达到阈值时会按顺序裁剪、摘要或轮换；
- provider failure 不会重复发送超大原始 payload；
- 会话轮换后可以通过 handoff 继续任务；
- 压缩质量、引用正确性、权限安全和成本都有可回放指标；
- 新路径可以灰度和回滚。

## 20. 与现有文档的关系

- 本文是快速实施入口；
- `projectlens-context-composer-development-spec.md` 是完整上下文管理器开发规格；
- `projectlens-context-composer-product-design.md` 是产品交互和原则说明；
- `projectlens-knowledge-retrieval-vector-and-evaluation-plan.md` 负责知识库检索与知识评测，不替代本文的会话压缩评测；
- `projectlens-obsidian-hermes-development-plan.md` 负责 Obsidian 与 Hermes 的整体阶段规划。

实施时优先按本文 Phase 1-4 落地，再进入会话轮换和评测灰度。
