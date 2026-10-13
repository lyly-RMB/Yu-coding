# ProjectLens 文档地图

**产品与研发共用总纲**：[`projectlens-product-document.md`](projectlens-product-document.md)

文档分层如下。冲突时以产品总纲为准。

## 现行文档

| 文档 | 用途 |
|------|------|
| [`competition-remediation-plan.md`](competition-remediation-plan.md) | **参赛整改与交付计划**：问题、解决方法、验收标准、评测和材料清单 |
| [`competition-test-plan.md`](competition-test-plan.md) | **参赛专项测试方案**：功能、AI 评测、安全、性能、飞书演示、试点和放行门禁 |
| [`projectlens-answer-pipeline-implementation-plan.md`](projectlens-answer-pipeline-implementation-plan.md) | **问答链路职责分离实施方案**：Hermes 回答正文、AnswerDraft 证据结构、死代码清理、验证与飞书展示 |
| [`projectlens-product-document.md`](projectlens-product-document.md) | 产品定义、目标/非目标、概念、MVP、路线图 |
| [`feishu-test-quickstart.md`](feishu-test-quickstart.md) | **新人飞书测试**：要配什么、怎么配、怎么冒烟（推荐 `scripts/start-projectlens-feishu-ws.ps1`） |
| [`projectlens-feishu-real-test-plan.md`](projectlens-feishu-real-test-plan.md) | **真实团队测试计划**：角色、脚本、阶段、门禁、缺陷和报告模板 |
| [`../evaluation/feishu_team_test/README.md`](../evaluation/feishu_team_test/README.md) | **飞书群聊测试资产**：角色配置、三个剧本、执行日志和缺陷模板 |
| [`../config/hermes/README.md`](../config/hermes/README.md) | Hermes `projectlens-safe` profile（CLI/MCP 调试；群聊以 WS ingress 为准） |
| [`feishu-setup.md`](feishu-setup.md) | 飞书应用、长连接 / HTTP 回调、权限与联调检查 |
| [`pilot-launch-config.md`](pilot-launch-config.md) | `.env` 配置剖面（Stub / Safe Live / 测试群） |
| [`projectspace-onboarding-guide.md`](projectspace-onboarding-guide.md) | 新项目 ProjectSpace 接入 |
| [`projectlens-tool-envelope.md`](projectlens-tool-envelope.md) | 工具与回答信封契约（实现对照） |
| [`projectlens-obsidian-hermes-knowledge-base.md`](projectlens-obsidian-hermes-knowledge-base.md) | Obsidian 团队知识库与 Hermes 管理方案 |
| [`projectlens-obsidian-hermes-development-plan.md`](projectlens-obsidian-hermes-development-plan.md) | Obsidian + Hermes 分阶段开发实施方案 |
| [`projectlens-long-term-memory-architecture.md`](projectlens-long-term-memory-architecture.md) | 长期记忆架构、治理、检索与评测设计 |
| [`projectlens-long-term-memory-development-spec.md`](projectlens-long-term-memory-development-spec.md) | 长期记忆具体开发规格、迁移、接口与测试 |
| [`projectlens-knowledge-retrieval-vector-and-evaluation-plan.md`](projectlens-knowledge-retrieval-vector-and-evaluation-plan.md) | 知识库统一向量检索、混合召回、治理与评测门禁方案 |
| [`projectlens-knowledge-retrieval-vector-development-plan.md`](projectlens-knowledge-retrieval-vector-development-plan.md) | 知识库向量检索分阶段开发、测试、验收、灰度与回滚计划 |
| [`projectlens-context-manager-development-plan.md`](projectlens-context-manager-development-plan.md) | **上下文管理器**开发实施方案（飞书群 `@`：滑动窗口/引用、fork、预览即实发、GenAI trace 落盘） |
| [`projectlens-context-composer-product-design.md`](projectlens-context-composer-product-design.md) | 上下文管理器产品设计：按天隔离、历史选择、摘要、状态与完整性提醒 |
| [`projectlens-context-composer-development-spec.md`](projectlens-context-composer-development-spec.md) | 上下文管理器开发规格：六项核心功能、数据模型、飞书卡片、快照、测试与分阶段实施 |
| [`projectlens-context-compression-quick-development-plan.md`](projectlens-context-compression-quick-development-plan.md) | Hermes 上下文压缩快速落地：预算、裁剪、增量摘要、会话轮换、失败恢复与评测 |

## 目标架构

| 文档 | 用途 |
|------|------|
| [`enterprise-architecture-v2.md`](enterprise-architecture-v2.md) | 企业版目标架构（**目标 ≠ 现状**） |
| [`phase4-projectspace-pluggable.md`](phase4-projectspace-pluggable.md) | ProjectSpace 可插拔设计说明 |

## 规划与历史（勿当安装指南）

以下文件可能描述尚未落地能力或已被总纲覆盖的旧表述。阅读前请先看产品总纲。

| 文档 | 说明 |
|------|------|
| [`projectlens-pilot-product-blueprint.md`](projectlens-pilot-product-blueprint.md) | 试点产品蓝图（历史） |
| [`projectlens-mvp-requirements.md`](projectlens-mvp-requirements.md) | 早期 MVP 需求（历史） |
| [`projectlens-pilot-development-spec.md`](projectlens-pilot-development-spec.md) | 试点研发规格（历史） |
| [`projectlens-change-log.md`](projectlens-change-log.md) | 分阶段变更记录 |
| [`hermes-conversational-answer-experience-plan.md`](hermes-conversational-answer-experience-plan.md) | 对话体验规划 |
| [`hermes-context-snapshot-development-plan.md`](hermes-context-snapshot-development-plan.md) | 上下文快照规划（**与现行「上下文管理器」冲突，勿当实现依据**；见代办清理） |
| [`projectlens-context-composer-development-plan.md`](projectlens-context-composer-development-plan.md) | Context Composer 规划（**同上，以** [`projectlens-context-manager-development-plan.md`](projectlens-context-manager-development-plan.md) **为准**） |
| [`projectlens-long-term-memory-development-plan.md`](projectlens-long-term-memory-development-plan.md) | 长期记忆规划 |

## 推荐阅读顺序

1. [`projectlens-product-document.md`](projectlens-product-document.md) — 理解产品边界
2. 仓库根目录 [`README.md`](../README.md) — 快速开始
3. [`feishu-test-quickstart.md`](feishu-test-quickstart.md) — 飞书群联调（新人优先）
4. [`pilot-launch-config.md`](pilot-launch-config.md) + [`feishu-setup.md`](feishu-setup.md) — 配置细表
5. [`projectspace-onboarding-guide.md`](projectspace-onboarding-guide.md) — 接入真实项目
6. [`competition-test-plan.md`](competition-test-plan.md) — 参赛专项测试方案
7. [`projectlens-obsidian-hermes-knowledge-base.md`](projectlens-obsidian-hermes-knowledge-base.md) — Obsidian 与 Hermes 知识库方案
8. [`projectlens-obsidian-hermes-development-plan.md`](projectlens-obsidian-hermes-development-plan.md) — 分阶段开发实施细节
