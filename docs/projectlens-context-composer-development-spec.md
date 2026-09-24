# ProjectLens 上下文管理器开发规格

版本：v0.1  
日期：2026-09-20  
状态：待实施  
配套产品设计：[projectlens-context-composer-product-design.md](projectlens-context-composer-product-design.md)  
产品入口：飞书原生互动卡片

## 1. 文档目的

本文把上下文管理器产品设计转成开发团队可执行的工程规格。本期必须落地六项核心能力：

1. 继续昨天工作；
2. 今日专注、继续工作、历史调查三种模式；
3. 每日工作交接摘要；
4. 摘要与原始消息粒度选择：原文、仅摘要、摘要加关键原文；
5. 上下文完整性提醒；
6. 上下文快照回看。

本文不规定具体 UI 视觉风格，不替代产品设计，也不开放写代码、部署或生产操作。

## 2. 开发目标与不变式

### 2.1 目标

用户在飞书发起一次 Agent 请求时，系统能够：

- 按 ProjectSpace 时区把聊天分为今天、昨天和更早日期；
- 默认只把今天的聊天作为候选，不自动加入昨天原始消息；
- 通过三种模式给出不同的选择起点；
- 用每日交接摘要表达跨日工作状态；
- 在同一段历史上选择原文、摘要或摘要加关键原文；
- 发送前检查是否缺少已完成事项、任务状态或前置背景；
- 创建不可变 ContextSnapshot 并绑定 AgentRun；
- 在飞书回看本次 Agent 实际使用的日期、来源、Token、提醒和运行结果；
- 保证预览与 Hermes 实际输入使用同一套 Snapshot 渲染结果。

### 2.2 核心不变式

1. **日期隔离**：默认上下文只来自项目时区下的今天。
2. **主动回溯**：昨天及更早内容只有在用户明确选择后进入 Draft/Snapshot。
3. **状态独立**：每日交接摘要是派生信息，不等于原始聊天，也不修改历史日期。
4. **不偷偷补回**：完整性提醒只能推荐补充，不能静默加入用户取消的普通历史。
5. **知识边界**：未进入 Snapshot 的事实对 Agent 来说是未知或未确认。
6. **快照不可变**：Snapshot 创建后不能更新或删除；修改输入必须创建新快照。
7. **权限优先**：候选、Draft、预览、冻结、发送和回看都重新校验可信身份与 EffectiveAccessScope。
8. **统一渲染**：预览与实际模型输入必须经过同一 Snapshot -> ContextPack -> ContextPrompt 链路。

### 2.3 非目标

本期不做：

- 删除或编辑飞书原始聊天；
- 因为取消历史而回滚 Git、文件、数据库、API 或任务系统；
- 因为取消历史而自动删除 ProjectMemory；
- 完整的历史轮次到摘要/记忆的依赖图；
- 独立 Web 页面；
- 无限历史一次性放进飞书卡片；
- Apply、PR、deploy、rollback、restart 等写操作。

## 3. 当前仓库基础与改造缺口

### 3.1 可复用模块

| 现有能力 | 代码位置 | 本功能复用方式 |
| --- | --- | --- |
| ConversationSession、滚动摘要、task scratchpad | src/project_lens/domain/conversation.py | 今日候选、摘要和推荐状态来源 |
| ContextPack | src/project_lens/workflow/context_pack.py | Snapshot 到 L0-L5 的运行载体 |
| ContextPrompt | src/project_lens/workflow/context_prompt.py | 预览和真实 Prompt 的共同 renderer |
| Evidence、ProjectMemory、来源授权 | src/project_lens/context/、src/project_lens/domain/memory.py | 外部证据和派生信息候选 |
| AgentRun、AgentEvent、SQLite | src/project_lens/domain/models.py、src/project_lens/persistence/sqlite.py | 运行绑定、审计和回看 |
| EffectiveAccessScope | src/project_lens/project_space/ | 所有候选和发送权限边界 |
| HermesRuntimeService | src/project_lens/application/hermes_runtime.py | 只消费已冻结上下文 |
| Feishu callback/cards | src/project_lens/integrations/feishu/ | 卡片交互与结果展示 |

### 3.2 必须补齐的能力

1. ConversationSession 只保存短期轮次，不能作为完整跨日历史库。
2. 需要追加写入、按日期查询、带 scope 的 ConversationTurnRecord。
3. 现有 snapshot 基础主要覆盖 ProjectMemory，需要扩展为通用 SnapshotItem。
4. Composer 模式下 Hermes 不能隐式追加 session 最近轮次、摘要或 memory。
5. 需要 DailyHandoff、三种 mode、摘要/原文粒度选择和完整性检查。
6. 需要飞书卡片的日期分页、模式选择、预览、警告和 Snapshot 回看。

### 3.3 兼容已有快照字段

AgentRun 当前已有 context_snapshot_id、context_hash 等字段时，不重复添加同名字段。统一它们的语义，使其指向通用 ContextSnapshot；旧运行保持 NULL，不伪造历史快照。现有 memory-only snapshot 通过 adapter 兼容读取，逐步迁移到通用模型。

### 3.4 现有飞书接入的改造边界

当前 `FeishuMessageBody.text()` 会把文本中的 `@_user_N` 展示 token 清理后再交给路由。上下文管理器不能把这个清理结果当作完整事件模型，必须同时保留：

- 原始 `message_id`、`chat_id`、`chat_type` 和 sender；
- Feishu 提供的 mention 列表或 mention entity；
- `thread_id`、父消息 ID 或 reply/root 信息（如果事件提供）；
- 清理后的 `command_text`；
- `mentioned_bot` 和 `trigger_type` 的服务端判定结果。

群聊只有在可信 mention 元数据明确指向当前机器人时才创建 `FeishuContextTrigger`。仅凭消息文本中出现 `@`、`@_user_1` 或机器人名称不能触发；mention 元数据缺失时按普通非@消息入库。私聊消息可按现有 direct message 路由处理，`trigger_type` 记录为 `direct_message`。

事件解析、候选查询和卡片 action 都必须使用真实 callback 中解析出的 actor/chat/project，不能接受客户端通过 action value 伪造的 sender、群聊或消息正文。

## 4. 目标架构

### 4.1 数据流

~~~text
飞书提问或编辑本次上下文
  -> trusted actor + ProjectSpace + chat + project timezone
  -> ContextModeResolver
  -> DailyContextPartitioner
  -> ACL-filtered ContextCandidateService
  -> ContextSelectionDraft
  -> ContextCompletenessChecker
  -> 飞书预览/提醒卡片
  -> freeze ContextSnapshot
  -> create AgentRun(context_snapshot_id)
  -> SnapshotMaterializer
  -> ContextPack -> ContextPrompt
  -> Hermes Runtime
  -> Answer verification
  -> 飞书结果和 Snapshot 回看
~~~

### 4.3 群聊 @ 触发与非@候选流程

飞书群聊事件不能只按清理后的文本路由。接收层必须同时保留原始消息元数据和用于理解的纯文本：

~~~text
Feishu callback
  -> 校验签名、tenant、sender、chat、message_id
  -> 解析可信 mentions、thread/reply 元数据
  -> 判断 bot_mentioned
  -> bot_mentioned=false：追加当天历史，不启动 AgentRun
  -> bot_mentioned=true：提取 command_text，创建 Context Draft
  -> 召回 thread 父消息/回复和有限时间窗口内候选
  -> ACL 过滤后进行相关性排序
  -> 生成“我理解的重点”及来源引用
  -> 用户直接回答或编辑候选
  -> freeze ContextSnapshot -> AgentRun
~~~

`@` 后的文本是当前问题锚点，不能被摘要替代。非@消息只能作为候选，不能因为出现在同一群聊或最近时间窗口而自动全部加入 Snapshot。昨天及更早的消息仍然必须由用户主动选择。

“重点提取”可以调用模型生成候选摘要，但候选范围、权限、日期边界和最终入选必须由程序控制：模型不能决定能否读取某条群聊消息，也不能把没有原始来源的推断写成已确认事实。

### 4.2 模块职责

~~~text
domain/context_composer.py
  mode、日期分区、候选、handoff、Draft、完整性、通用 Snapshot 契约

context/conversation_history_store.py
  完整聊天追加写入、按日分页和 scope 过滤

context/daily_handoff_service.py
  生成、缓存、读取、标记过期/冲突的每日交接摘要

context/context_candidate_service.py
  按日期、模式、搜索条件和 EffectiveAccessScope 查询候选

context/context_completeness.py
  确定性判断任务连续性缺口

context/context_snapshot_store.py
  Draft、Snapshot、SnapshotItem 的 SQLite 持久化

application/context_composer_service.py
  初始化 mode、更新 Draft、预览、检查、冻结、发送和回看

integrations/feishu/context_cards.py
  模式、候选、日期、预览、warning、结果和 detail 卡片
~~~

## 5. 按日隔离

### 5.1 日期计算

所有事件保存为 UTC aware datetime。ProjectSpace 必须提供 IANA timezone，不能直接使用部署机器本地时间。

~~~text
local_date = created_at.astimezone(project_timezone).date()
~~~

写入时由服务端计算 local_date，客户端不能提交或覆盖。必须测试 Asia/Shanghai、UTC、跨午夜和夏令时地区。

### 5.2 候选规则

- local_date 等于 today 的 ConversationTurnRecord 进入 Today 候选；
- local_date 小于 today 的记录只能进入 History 候选；
- History 候选只有在用户选中日期和条目后才进入 Draft；
- 摘要、handoff、TaskState、RunSummary 按生成日期分类；
- 选择 handoff 不自动展开并加入其覆盖的全部原始消息。

### 5.3 模式默认值

| 模式 | 默认内容 | 历史行为 |
| --- | --- | --- |
| today_focus | 今天聊天、今天状态、当前问题 | 不加入旧日期 |
| continue_work | today_focus + 昨日 handoff、未完成项、最近运行摘要 | 不加入昨日全部原文 |
| history_investigation | today_focus + 用户指定日期和片段 | 只有用户选中的历史进入 |

模式只是 Draft 初始选择，不能绕过 ACL、Token 或完整性检查。

## 6. 领域模型

建议新增 src/project_lens/domain/context_composer.py，遵循现有 Pydantic FrozenModel 风格。

### 6.1 模式和历史记录

~~~python
ContextMode = Literal["today_focus", "continue_work", "history_investigation"]

class ConversationTurnRecord(FrozenModel):
    turn_id: UUID
    project: ProjectRef
    tenant_id: str
    chat_id: str | None
    message_id: str | None = None
    thread_id: str | None = None
    parent_message_id: str | None = None
    actor_id: str
    speaker: Literal["user", "assistant", "system", "tool"]
    text: str
    rewritten_text: str | None = None
    session_id: UUID | None = None
    run_id: UUID | None = None
    sequence: int
    created_at: datetime
    local_date: date
    timezone_name: str
    visibility_scope: str
    content_hash: str
    mentioned_bot: bool = False
    mention_spans: tuple["MentionSpan", ...] = ()
    trigger_type: Literal["mention", "direct_message", "card_action", "none"] = "none"
    retention_state: Literal["active", "redacted", "expired"] = "active"
~~~

`text` 保存去除展示 token 后的可读文本，原始 Feishu content 和 mention 元数据必须以受控审计字段或 payload 保存，不能因为清理 `@_user_N` 就丢失是否 @ 机器人的事实。`mentioned_bot` 只能由可信事件解析结果设置，不能由客户端提交。

~~~python
class MentionSpan(FrozenModel):
    start: int
    end: int
    mentioned_id: str
    mentioned_type: Literal["user", "bot", "all", "unknown"]
    is_target_bot: bool

class FeishuContextTrigger(FrozenModel):
    message_id: str
    chat_id: str
    thread_id: str | None
    parent_message_id: str | None
    sender_id: str
    bot_mentioned: bool
    command_text: str
    received_at: datetime
~~~

原始用户文本在 rewrite 前追加保存；session compression 不能删除或覆盖；assistant/tool 原文按敏感信息策略保存。

### 6.2 候选项和交接摘要

~~~python
ContextCandidateKind = Literal[
    "conversation_turn", "conversation_group", "session_summary",
    "daily_handoff", "task_state", "run_summary", "evidence",
    "project_memory", "external_state_ref",
]

class ContextCandidate(FrozenModel):
    candidate_id: str
    kind: ContextCandidateKind
    project: ProjectRef
    chat_id: str | None
    source_id: str
    title: str
    summary: str
    local_date: date | None
    source_refs: tuple[str, ...] = ()
    estimated_tokens: int
    content_hash: str
    status: Literal["active", "expired", "revoked", "conflicted", "changed"]
    coverage_key: str | None = None
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    relevance_reasons: tuple["ContextRelevanceReason", ...] = ()
    user_confirmed: bool = False
    selection_origin: Literal["default", "recommended", "user_selected"] = "default"

class ContextRelevanceReason(FrozenModel):
    reason: Literal[
        "mention_message", "thread_parent", "thread_reply",
        "nearby_time_window", "semantic_match", "daily_handoff",
    ]
    score: float
    source_message_id: str
    explanation: str | None = None

class DailyHandoff(FrozenModel):
    handoff_id: UUID
    project: ProjectRef
    chat_id: str | None
    local_date: date
    timezone_name: str
    target: str | None
    completed: tuple[str, ...]
    in_progress: tuple[str, ...]
    unfinished: tuple[str, ...]
    decisions: tuple[str, ...]
    open_questions: tuple[str, ...]
    next_actions: tuple[str, ...]
    source_refs: tuple[str, ...]
    status: Literal["draft", "confirmed", "stale", "conflicted"]
    generated_at: datetime
    valid_until: datetime | None = None
~~~

### 6.3 摘要/原文选择

~~~python
HistorySelectionMode = Literal["raw", "summary", "summary_plus_key_turns"]

class CoverageChoice(FrozenModel):
    coverage_key: str
    mode: HistorySelectionMode
    candidate_ids: tuple[str, ...]
    key_turn_ids: tuple[UUID, ...] = ()
~~~

同一 coverage_key 默认只有一个 choice。摘要加关键原文是一个组合条目，避免渲染器误把它当成两个独立历史段落。

### 6.4 Draft、完整性和 Snapshot

~~~python
class ContextSelectionDraft(FrozenModel):
    draft_id: UUID
    project: ProjectRef
    actor_id: str
    chat_id: str | None
    question: str
    mode: ContextMode
    ordered_candidate_ids: tuple[str, ...]
    coverage_choices: tuple[CoverageChoice, ...]
    token_budget: int
    revision: int
    scope_fingerprint: str
    completeness_override: bool = False
    expires_at: datetime

class CompletenessIssue(FrozenModel):
    code: str
    severity: Literal["info", "warning", "blocking"]
    title: str
    explanation: str
    recommended_candidate_ids: tuple[str, ...] = ()

class ContextSnapshotItem(FrozenModel):
    position: int
    candidate_id: str
    kind: ContextCandidateKind
    source_id: str
    content_hash: str
    estimated_tokens: int
    rendered_text: str | None
    source_refs: tuple[str, ...]
    local_date: date | None
    status: Literal["included", "changed", "revoked", "expired", "missing"]
    reason_code: str | None = None

class ContextSnapshot(FrozenModel):
    snapshot_id: UUID
    project: ProjectRef
    actor_id: str
    chat_id: str | None
    question: str
    mode: ContextMode
    items: tuple[ContextSnapshotItem, ...]
    estimated_tokens: int
    token_budget: int
    renderer_version: str
    policy_version: str
    snapshot_hash: str
    completeness_override: bool
    completeness_issue_codes: tuple[str, ...]
    created_at: datetime
    sent_at: datetime | None = None
    sent_run_id: UUID | None = None
~~~

## 7. 每日工作交接摘要

### 7.1 生成策略

采用“按需生成、结果缓存”：

1. 用户首次打开 continue_work；
2. 查询前一自然日是否已有有效 handoff；
3. 有效则直接展示；
4. 没有则用前一天授权的 TaskState、RunSummary、已确认摘要、Git/Evidence 摘要生成 draft；
5. 保存 source_refs 和 generated_at；
6. 用户选择后才进入当前 Draft，不自动升级为 ProjectMemory。

不在每天零点为所有项目无条件调用模型。

### 7.2 生成内容

交接必须区分：当前目标、已完成、进行中、未完成、已确认决策、待确认问题、下一步。来源优先级为已验证 AgentRun/Evidence、结构化 TaskState、已确认摘要、原始聊天候选结论。无法绑定来源的内容只能标为待确认或 unknown。

### 7.3 过期和冲突

如果最新 Git/Evidence 与 handoff 冲突，标记 stale 或 conflicted，展示冲突来源，不自动覆盖旧 handoff。用户仍可在 history_investigation 中选择，但发送前必须看到 warning。

## 8. 上下文完整性检查

### 8.1 规则引擎

ContextCompletenessChecker 必须是确定性的。输入为问题、mode、Draft 候选元数据、coverage、TaskState、handoff、RunSummary、预算和授权结果。LLM 可以辅助生成解释文案，但不能决定 blocking/warning。

MVP 规则：

| code | 触发条件 | severity |
| --- | --- | --- |
| MISSING_CURRENT_STATE | 问题包含继续/修改/修复，但没有状态、handoff 或运行摘要 | warning |
| MISSING_PRIOR_COMPLETION | 当前选择包含后续工作，却没有已完成事项来源 | warning |
| RAW_WITHOUT_BACKGROUND | 选择后续原文但未选择前置背景 | info |
| SUMMARY_RAW_DUPLICATE | 同 coverage_key 同时选 summary 和 raw | info |
| STALE_HANDOFF | handoff 过期或被新状态取代 | warning |
| ACCESS_REVOKED | 来源不再可见 | blocking |
| CONTENT_CHANGED | hash/version 和 Draft 不一致 | blocking |
| TOKEN_OVER_BUDGET | 估算超过硬预算 | blocking |

### 8.2 Override

用户选择仍然发送时，Draft revision 增加，设置 completeness_override=true，记录 issue code。Override 不能绕过权限、撤销、hash/version 或 Token 硬错误。Agent Prompt 必须声明未选择内容不代表为假，只代表本次没有提供。

## 9. 飞书卡片状态机

### 9.1 状态

~~~text
IDLE -> MODE_SELECT -> CANDIDATE_LIST -> DRAFT_EDITING
     -> PREVIEW -> COMPLETENESS_WARNING -> SENDING
     -> RUN_RESULT -> SNAPSHOT_DETAIL
~~~

历史选择额外经过 HISTORY_DATE_LIST -> HISTORY_ITEMS。卡片 action 只携带 draft_id、revision、action、cursor 和短期 nonce；actor、tenant、project、chat、role 由可信 callback 解析。

### 9.2 三种入口

~~~text
开始今天的工作

[今日专注]       今天聊天和当前状态
[继续工作]       今天内容 + 昨日工作交接
[历史调查]       选择日期和具体历史
~~~

### 9.3 候选和预览

候选按今日聊天、跨日状态、历史日期、Evidence、ProjectMemory 分组。历史分页展示。每项展示标题、类型、来源日期、摘要、Token、状态和推荐标记，并支持选择、取消、上移、下移、查看详情、切换原文/摘要粒度。

Preview 必须展示今天数量、历史日期和数量、摘要/原文/组合选择、handoff/TaskState/Evidence 数量、Token、完整性 issues 和发送按钮状态。

Snapshot Detail 必须展示同一组元数据、snapshot_id、AgentRun 状态和时间。回看不能修改 Snapshot，只能复制成新 Draft。

## 10. SQLite 持久化

建议新增命名空间 migration context_composer。用于过滤和排序的字段单独存列，复杂内容可放 payload JSON。

~~~sql
CREATE TABLE conversation_turn_records (
  turn_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  chat_id TEXT,
  message_id TEXT,
  thread_id TEXT,
  parent_message_id TEXT,
  actor_id TEXT NOT NULL,
  speaker TEXT NOT NULL,
  text TEXT NOT NULL,
  rewritten_text TEXT,
  session_id TEXT,
  run_id TEXT,
  sequence INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  local_date TEXT NOT NULL,
  timezone_name TEXT NOT NULL,
  visibility_scope TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  mentioned_bot INTEGER NOT NULL DEFAULT 0,
  mention_payload TEXT NOT NULL DEFAULT '[]',
  trigger_type TEXT NOT NULL DEFAULT 'none',
  retention_state TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX idx_turn_records_day ON conversation_turn_records
  (tenant_id, project_id, chat_id, local_date, created_at);
CREATE INDEX idx_turn_records_thread ON conversation_turn_records
  (tenant_id, project_id, chat_id, thread_id, created_at);

CREATE TABLE daily_handoffs (
  handoff_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  chat_id TEXT,
  local_date TEXT NOT NULL,
  timezone_name TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL,
  generated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_daily_handoff_scope_day
  ON daily_handoffs (tenant_id, project_id, chat_id, local_date);

CREATE TABLE context_selection_drafts (
  draft_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  chat_id TEXT,
  actor_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  mode TEXT NOT NULL,
  question TEXT NOT NULL,
  payload TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE context_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  chat_id TEXT,
  actor_id TEXT NOT NULL,
  mode TEXT NOT NULL,
  question TEXT NOT NULL,
  estimated_tokens INTEGER NOT NULL,
  token_budget INTEGER NOT NULL,
  renderer_version TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL UNIQUE,
  payload TEXT NOT NULL,
  completeness_override INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  sent_at TEXT,
  sent_run_id TEXT
);
CREATE INDEX idx_context_snapshots_scope_time
  ON context_snapshots (tenant_id, project_id, chat_id, actor_id, created_at);

CREATE TABLE context_snapshot_items (
  snapshot_id TEXT NOT NULL,
  position INTEGER NOT NULL,
  candidate_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  source_id TEXT NOT NULL,
  local_date TEXT,
  content_hash TEXT NOT NULL,
  estimated_tokens INTEGER NOT NULL,
  status TEXT NOT NULL,
  reason_code TEXT,
  payload TEXT NOT NULL,
  PRIMARY KEY (snapshot_id, position)
);
~~~

迁移必须复用 SQLiteDatabase.run_migrations()，可重复执行；send 在一个事务中完成 Draft 校验、Snapshot/Items 写入、AgentRun 绑定和队列记录。Snapshot 不提供 update/delete 应用接口。

## 11. 服务接口和错误

### 11.1 应用服务

~~~python
class ContextComposerService:
    def start(self, actor_context, question, mode) -> ContextSelectionDraft: ...
    def list_candidates(self, draft_id, actor_context, date, cursor) -> CandidatePage: ...
    def update_selection(self, draft_id, actor_context, revision, patch) -> DraftView: ...
    def preview(self, draft_id, actor_context, revision) -> PreviewView: ...
    def check_completeness(self, draft_id, actor_context) -> ContextCompletenessReport: ...
    def send(self, draft_id, actor_context, revision, override) -> AgentRun: ...
    def get_snapshot(self, snapshot_id, actor_context) -> SnapshotView: ...
    def copy_snapshot_to_draft(self, snapshot_id, actor_context) -> ContextSelectionDraft: ...
~~~

服务层不依赖飞书 SDK，不直接调用模型 provider；它依赖 Store、Scope Resolver、ContextPack Builder、Prompt Renderer 和 RunService。

### 11.2 Action 枚举

~~~text
context_mode_today_focus
context_mode_continue_work
context_mode_history_investigation
context_answer_with_recommended
context_edit_understood_focus
context_include_thread
context_exclude_candidate
context_select_more_history
context_confirm_focus
context_open_history_date
context_toggle_candidate
context_choose_history_mode
context_move_candidate_up
context_move_candidate_down
context_add_recommendation
context_preview
context_send_with_override
context_back_to_edit
context_view_snapshot
context_copy_snapshot
~~~

所有 action 携带 Draft revision。旧卡片点击返回 DRAFT_REVISION_CONFLICT，不覆盖最新选择。

### 11.3 错误码

~~~text
CONTEXT_DRAFT_NOT_FOUND
CONTEXT_DRAFT_EXPIRED
DRAFT_REVISION_CONFLICT
CONTEXT_CANDIDATE_FORBIDDEN
CONTEXT_CANDIDATE_REVOKED
CONTEXT_CHANGED
CONTEXT_TODAY_SCOPE_INVALID
CONTEXT_TOKEN_OVER_BUDGET
CONTEXT_SNAPSHOT_NOT_FOUND
CONTEXT_SNAPSHOT_FORBIDDEN
CONTEXT_SNAPSHOT_IMMUTABLE
CONTEXT_IDEMPOTENCY_REPLAY
~~~

错误卡片只展示安全原因和下一步，不展示被拒绝来源正文。

## 12. ContextPack 与 Hermes 接入

Snapshot 是用户选择的唯一事实。物化时重新绑定 actor、tenant、project、chat、scope，逐项校验状态、有效期、hash 和来源权限，再映射到 L1-L5，使用现有 render_context_prompt() 生成 Prompt，并在 audit refs 中加入 snapshot_id、snapshot_hash、mode、日期和 completeness_override。

Composer 模式下 Hermes 不得自动追加：

- 未选择的 ConversationSession.recent_turns；
- 未选择的 ConversationSummary；
- 未选择的 ProjectMemory；
- 其他日期的聊天；
- 用户未确认的外部状态。

如果 Agent 需要更多信息，只能通过经过 scope 检查的 ProjectLens 工具读取；新读取结果必须产生当前运行的 Evidence/AgentEvent，不能绕过 Snapshot 直接读取完整历史。

如果 replay 发现权限、撤销或 hash 变化，不静默替换内容，不回退默认 session；运行失败或需要刷新，Snapshot 保留审计信息。

## 13. 测试计划

### 13.1 领域和 Store

- ProjectSpace 时区下日期边界、跨午夜和夏令时正确；
- Today 候选不包含昨天原始 turn；History 必须主动选择日期；
- session compression 不删除完整历史；
- DailyHandoff 的 completed、unfinished、next_actions、source_refs 可回放；
- raw、summary、summary_plus_key_turns 的 coverage_key 规则正确；
- Draft revision 冲突不覆盖他人修改；
- Snapshot 不可 update/delete，重复 send 幂等。

### 13.2 完整性

- “继续实现订单管理”但无状态/handoff/run summary 时产生 warning；
- 选择仍然发送后记录 completeness_override；
- 后续原文没有前置背景时产生 info；
- 同 coverage 原文和摘要重复时产生 info；
- stale/conflicted handoff 不当作 confirmed current state；
- 权限、撤销、hash 变化和超预算为 blocking，不能被 override 绕过。

### 13.2.1 群聊 @ 与非@行为

- 普通群聊非@消息只追加 `ConversationTurnRecord`，不创建 AgentRun；
- 可信 mention 元数据显示机器人被 @ 时，创建 `FeishuContextTrigger`；
- `@` 后文本被正确提取为 `ContextSelectionDraft.question`；
- thread 父消息、回复和有限时间窗口内相关消息可成为候选，并保存 `relevance_reasons`；
- 无关时间窗口消息不会被默认选中；
- “直接回答”只发送推荐候选，不带入未确认的全部群聊；
- “编辑上下文”可以取消推荐项，Snapshot 记录最终选择和 `user_confirmed`；
- 发送者身份使用真实 callback actor，而不是被 @ 文本中出现的用户；
- @ 不扩大 `ChatVisibilityPolicy` 或 `EffectiveAccessScope`；
- mention 元数据缺失或无法确认时按普通消息处理，不触发 AgentRun；
- preview 与实际 Provider sections/messages 的来源集合和顺序一致。

### 13.3 飞书卡片

- 首次显示三种模式；
- Today 不展示未授权的历史标题或正文；
- Continue Work 推荐昨日 handoff 但不自动加入全部原文；
- History Investigation 可分页选择日期和条目；
- 卡片明确区分摘要和原始消息；
- Preview、Warning、Send、Run Result、Snapshot Detail 可以连续跳转；
- 旧 revision callback 被拒绝；
- 用户只能回看有权限的 Snapshot。
- 非@群聊消息不会创建 AgentRun；@机器人消息创建 Draft 并保留当前问题锚点；
- @ 后的文本提取、thread 父子链关联和附近时间窗口候选可回放；
- 推荐候选展示来源、相关性理由和 selection_origin，用户确认后才冻结；
- 无关群聊消息不会因为时间接近而自动进入 Snapshot；
- 昨天及更早相关消息不会因 @ 自动进入，必须主动选择；
- 伪造 mention、sender、chat 或跨群 candidate ID 会被拒绝。

### 13.4 Hermes 集成和安全

- Provider 收到的 sections/messages 与 Preview 一致；
- 今天请求不包含昨天未选择的 turn；
- AgentRun 的 snapshot_id、hash、mode 和日期审计正确；
- 运行失败仍可回看 Snapshot；
- 伪造 tenant/project/actor/role、跨项目 candidate ID、私聊 Snapshot 群聊重放全部被拒绝；
- 日志不写入完整敏感 Prompt、凭据或机密工具输出。

## 14. 分阶段实施计划

### Phase 0：契约和基线

新增领域枚举、错误码、renderer contract、ProjectSpace timezone 配置和黄金测试；增加 feature flag，默认关闭 Composer。旧入口行为必须不变。

### Phase 1：完整历史和按日分区

新增 conversation_turn_records Store；在飞书消息和 AgentRun 完成时追加写入；计算 local_date；实现 Today/History 分页和 scope 过滤。完成条件是今天默认不读昨天原始 turn。

### Phase 2：三种模式和每日交接

实现 mode resolver、DailyHandoff Store/Service、按需生成和 stale/conflicted 状态、Continue Work 推荐策略、历史日期选择。完成条件是 Continue Work 不自动加入昨天全部原文。

### Phase 3：摘要/原文粒度

实现 coverage_key、raw/summary/summary_plus_key_turns 三种选择、重复提示和按模式重新估算 Token。完成条件是 Preview 明确显示 Agent 使用的粒度。

### Phase 4：Draft、完整性和飞书交互

实现 Draft revision、TTL、callback nonce、CompletenessChecker、Preview/Warning/Override 卡片、上移/下移和并发冲突。完成条件是 warning 不改选择，blocking 不能 override。

### Phase 5：通用 Snapshot 与 AgentRun

将现有 memory-only snapshot 通过 adapter 接入通用 Snapshot/Item Store；实现 freeze_and_send 原子事务、AgentRun 绑定、失败审计、Snapshot Detail 和 Copy to Draft。完成条件是 Preview 与 Hermes 输入一致。

### Phase 6：Hermes 约束和回归

Hermes context builder 只接受 materialized Snapshot；增加 selected-only 工具读取策略、日期边界审计和 Provider golden tests；验证 Feishu/HTTP/MCP 一致。

### Phase 7：灰度和观测

记录模式使用量、Continue Work 选择率、handoff 冲突率、raw/summary 比例、warning/override、权限拒绝、ContextChanged、Token 超限、Preview/实际 hash 差异、Snapshot 回看和 AgentRun 成功率。按 tenant/project/actor 灰度。

## 15. 回滚和失败降级

关闭 feature flag 后普通入口继续使用旧默认上下文；新 Draft/卡片操作停止；已有 Snapshot、handoff 和原始历史保留；旧运行不伪造成 Composer 运行。

| 失败 | 行为 |
| --- | --- |
| handoff 生成失败 | Today Focus 仍可用，Continue Work 显示无摘要 |
| 历史查询超时 | 返回今日缓存候选，历史按钮可重试 |
| completeness checker 失败 | 不自动补充，记录事件；安全策略下只允许明确 warning |
| Snapshot freeze 失败 | 不启动 AgentRun，Draft 保留 |
| Hermes provider 失败 | Snapshot 保留，AgentRun FAILED，可基于同一 Snapshot 重试 |
| 回看权限变化 | 重新校验权限，禁止展示失效正文 |

## 16. 完成定义

- 今天、昨天和更早历史按项目时区隔离；
- Today Focus 不自动携带昨天原始消息；
- Continue Work 推荐每日交接和未完成事项；
- History Investigation 能按日期、片段和分页选择；
- handoff 包含已完成、进行中、未完成、决策、待确认问题、下一步和来源日期；
- 同一覆盖范围支持 raw、summary、summary_plus_key_turns；
- 完整性提醒区分 info、warning、blocking；
- warning 可显式 override，权限/撤销/hash/预算硬错误不可绕过；
- Snapshot 不可变，AgentRun 绑定正确，失败运行可回看；
- Preview 与 Hermes 实际输入共享 renderer；
- 飞书回看显示模式、日期、来源、Token、提醒和运行状态；
- 取消历史不会删除或修改真实项目状态；
- 跨 tenant/project/chat/actor 权限隔离测试通过；
- feature flag 可以关闭新路径并恢复旧入口。

## 17. 后续版本

后续再做 DerivedArtifact source_refs、从选择重建摘要/计划、模板、上下文分支、Snapshot diff、精确 tokenizer 和加密短保留 Prompt artifact。它们都建立在本期六项核心功能稳定、可审计、权限边界明确的基础上。
