# ProjectLens 问答链路职责分离实施方案

**版本**：V1.0  
**日期**：2026-10-12  
**状态**：待实施方案

## 1. 目标

修正 Hermes 问答链路中“模型表达、证据校验、结果展示”职责混杂的问题：

- Hermes 负责理解问题、调用只读工具、组织自然语言，并按用户要求输出格式，例如“产品 / 研发 / 测试”三段。
- `AnswerDraft` 只负责事实、引用、未知项和后续动作的证据结构化，不负责 Markdown 或角色段落。
- `ProjectAnswer` 同时保存 Hermes 完整回答和经过验证的证据结果。
- 飞书卡片、飞书文本和 API 优先展示完整回答，不再被“**一句话结论**”限制。

三段输出是**表达需求**，不等于每一段都是真实已验证数据；真实性仍由 Evidence 验证链路决定。

## 2. 现状问题

当前链路容易出现以下问题：

1. Hermes 的完整回答被塞进 `business_summary` 或 `conclusion`，缺少独立的正文承载字段。
2. `AnswerDraft` 既承担证据解析，又可能被扩展为展示格式容器，造成职责耦合。
3. 验证层或投影层把模型回答压缩成第一条 fact 或一句业务摘要，丢失上下文和段落结构。
4. 飞书卡片、纯文本和 API 读取不同字段，导致各出口内容不一致。
5. 多角色总结的段落结构容易在解析或卡片渲染阶段被丢弃。

## 3. 目标链路

```text
用户问题
  ↓
Hermes + ProjectLens 只读工具
  ├─ answer_text：面向用户的完整回答，按需求组织格式
  └─ evidence_payload：facts / inferences / citations / unknowns / actions
              ↓
      AnswerDraft（只规范化 evidence_payload）
              ↓
      Evidence 引用解析与验证
              ↓
ProjectAnswer = answer_text + 已验证事实 + 引用 + 未知项 + 动作
       ├─ 飞书卡片
       ├─ 飞书文本
       └─ HTTP / MCP envelope
```

## 4. 组件职责

| 组件 | 负责 | 不负责 |
|---|---|---|
| Hermes | 理解问题、调用工具、生成完整回答、按需求分段 | 自行宣称事实已通过系统验证 |
| Hermes response adapter | 提取回答正文与 evidence payload | 改写回答含义 |
| `AnswerDraft` | 规范化 facts、inferences、citations、unknowns、next_actions | 产品/研发/测试段落、Markdown 布局 |
| verifier | 解析引用并绑定当前 Evidence | 删除或压缩完整回答 |
| `ProjectAnswer` | 并列保存正文和验证结果 | 把正文等同于已验证事实 |
| renderer | 展示完整正文及证据状态 | 重新总结、固定一句话、编造缺失信息 |

## 5. 数据契约

### 5.1 Hermes 输出

建议规范化为：

```json
{
  "answer_text": "产品：...\n\n研发：...\n\n测试：...",
  "evidence": {
    "facts": [
      {"text": "订单接口已增加参数校验", "citations": ["E1"]}
    ],
    "inferences": [],
    "unknowns": ["异常分支是否覆盖全部边界场景"],
    "next_actions": ["补充边界场景回归"]
  }
}
```

兼容现有字段：`answer_markdown`、`answer`、`final_response`、`facts`、`structured_facts`、`citations`、`unknowns`、`limitations`、`open_questions`、`next_actions`、`follow_up_questions`。

### 5.2 `AnswerDraft`

核心字段保持证据语义：

```python
class AnswerDraft:
    facts: list[DraftFact]
    inferences: list[str]
    unknowns: list[str]
    next_actions: list[str]
    citations: list[str]
    tools_used: list[str]
```

不加入 `role_summaries`、`product_summary`、`developer_summary`、`qa_summary` 等展示字段。可以兼容解析这些输入，但不能把它们当作事实验证结构。

### 5.3 `ProjectAnswer`

新增独立正文：

```python
answer_text: str = ""
```

并继续保存 `claims`、`facts`、`inferences`、`evidence`、`citations`、`unknowns` 和 `next_actions`。旧的 `business_summary`、`conclusion` 在迁移期保留，用于兼容历史调用方，但不再作为唯一正文来源。

## 6. 分阶段实施

### 阶段零：问答链路死代码盘点与清理

本次链路调整前后同步审查旧实现，避免旧逻辑继续被误用或在后续开发中复用。重点检查：

- 不再进入生产调用链的旧答案 parser、formatter、fallback 和运行时入口；
- 被新 `answer_text` 取代的摘要投影、重复的“一句话结论”拼装逻辑；
- 已不再使用的角色字段、旧 DTO 字段映射和兼容 helper；
- 重复 renderer、无调用方的私有函数、过时测试 fixture 和未注册的工具适配代码；
- 仅在测试或历史数据迁移中仍有用途的符号，明确标注用途，不误删。

清理流程：

1. 用静态引用搜索、类型/路由注册表、运行时入口和测试覆盖确认候选代码；
2. 区分真正不可达代码与公共 API、插件发现、反射、序列化、迁移、动态注册依赖；
3. 先删除或隔离已确认不再需要的实现和重复分支，再调整引用方；
4. 对保留的旧字段设置兼容边界和弃用说明，避免新代码继续依赖；
5. 运行受影响测试和全量静态检查，检查删除后是否仍存在孤立 import、过时注释和误导性文档；
6. 在实施记录中列出删除项、证据（无调用方/被替代路径）和保留项原因。

**约束**：不以“看起来没用”作为删除依据；不删除用户已有未提交改动；不清理与本问答链路无关的死代码。对无法确认是否动态调用的代码，先标为待核查，不直接删除。

**验收**：生产问答链路只有一条明确的正文提取与展示路径；已删除代码无静态调用、注册或迁移依赖；保留的兼容路径有测试并标注退役条件。

### 阶段一：拆分 Hermes 正文与证据 payload

1. 在 Hermes loop result 中显式保存原始终稿和 `answer_text`。
2. 新增 response adapter，分别提取完整回答和证据字段。
3. `AnswerDraft` 只接收证据 payload。
4. 对 `answer_text`、`answer_markdown`、`answer`、`final_response` 的优先级增加单元测试。

**验收**：三段 Markdown 原文在 adapter 中完整保留；AnswerDraft 不包含角色展示字段。

### 阶段二：验证层并列保存正文与证据

1. verifier 接收独立的 `answer_text` 和 `AnswerDraft`。
2. 只对 facts、inferences、citations 做 Evidence 绑定。
3. `answer_text` 原样写入 `ProjectAnswer.answer_text`。
4. 无效引用不能标记为已确认事实，但不能因此丢弃可展示正文。
5. 清理或迁移此前混入 AnswerDraft 的角色展示字段，保留历史数据兼容。

**验收**：长回答、三段回答逐字保留；验证后的 facts 与正文可分别读取。

### 阶段三：统一飞书和 API 展示

1. 卡片主内容优先读取 `answer.answer_text`。
2. 移除通用问答路径中固定的“一句话结论”标签和单字段压缩逻辑。
3. 后置展示“已验证事实、引用来源、当前未知、建议下一步”。
4. 没有 `answer_text` 时才使用旧摘要和 facts 做确定性兜底。
5. 卡片和纯文本共用同一正文来源。
6. API/MCP envelope 同时返回 `answer_text` 和独立 evidence 字段。

**验收**：普通问答、多段回答和三角色总结在各出口内容一致。

### 阶段四：Hermes 格式提示

1. 普通问题保持自然回答，不强制三段。
2. 用户明确要求总结时，提示 Hermes 输出“产品 / 研发 / 测试”三段。
3. 每段可以包含已确认、待确认和建议，但不得要求无资料角色编造内容。
4. 段落组织方式不作为事实真实性标签，真实性只由 evidence payload 决定。

### 阶段五：真实飞书验证

由用户手动在飞书群中发送测试消息，Agent 只分析结果，不代发消息。

## 7. 测试矩阵

| 场景 | 预期 |
|---|---|
| 普通项目问答 | 保留自然多段回答，不强制角色模板 |
| 三角色总结 | 正文保留产品、研发、测试三段，顺序不被卡片改写 |
| 仅返回 `answer_markdown` | 正文仍可展示，证据状态单独显示 |
| facts 带有效引用 | 绑定当前 Evidence |
| 引用无效或缺失 | 不标记为已验证事实，但正文仍可展示并说明边界 |
| 某角色无资料 | 显示信息不足，不编造 |
| 长回答 | 不静默丢失尾部；如受平台限制需明确提示 |
| API/MCP | `answer_text` 与飞书正文一致，证据字段独立可查 |

死代码清理额外检查：候选符号无引用、无动态注册/反射依赖、无持久化迁移用途；删除后 import、类型检查、相关测试和全量回归通过。

## 8. 安全与兼容要求

1. 展示 Hermes 正文不等于确认其中每个事实，界面必须区分“回答正文”和“已验证事实”。
2. 引用只能绑定当前权限范围内的 Evidence；不能按 `E1/E2` 顺序盲目补证据。
3. 上下文继承的 evidence 必须重新进行 ACL 校验。
4. 历史 `ProjectAnswer` 没有 `answer_text` 时使用旧字段兜底，新字段默认空字符串。
5. 飞书长度限制导致分段或截断时，必须显式提示并保留运行详情中的完整正文。

## 9. 放行门禁

- 新增 adapter、AnswerDraft、verifier、ProjectAnswer、卡片和文本渲染测试全部通过。
- 现有权限、脱敏、引用、记忆审批和上下文测试无未解释失败。
- `compileall`、Ruff、`git diff --check` 通过。
- 本地健康检查和飞书长连接正常。
- 用户完成至少一条普通问答和一条三角色总结真实测试，记录输入、完整回复、Run ID、trace ID 和截图。

## 10. 重点代码位置

| 层 | 文件 |
|---|---|
| Hermes 运行结果 | `src/project_lens/integrations/feishu/hermes_loop_runner.py` |
| 输出解析 | `src/project_lens/agent/hermes_answer_parser.py`、新增 response adapter |
| 证据草稿 | `src/project_lens/agent/draft.py` |
| 引用验证 | `src/project_lens/agent/hermes_answer.py`、`src/project_lens/agent/verifier.py` |
| 领域答案 | `src/project_lens/domain/models.py` |
| 飞书展示 | `src/project_lens/integrations/feishu/cards.py`、`role_views.py` |
| 外部投影 | `src/project_lens/application/answer_envelope.py`、Hermes tool envelope、API schemas |
| 测试 | `tests/test_hermes_answer_parser.py`、Feishu answer/card tests |

