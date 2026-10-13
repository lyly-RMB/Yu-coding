# ProjectLens 飞书测试质量报告（片段）

**日期**：2026-09-30  
**范围**：剧本一 `feishu-requirement-change-001`（协议中止于干预 1）  
**群**：`oc_f58ad5cc09f4d9e7b17ea4916e66ec82`

## 结论

- 是否建议进入小范围试运行：**否（有条件阻断）**
- 阻断原因：剧本一在干预总结处出现 **P1**（多角色上下文不连续）；按协议停止，未完成 seq4/5 与结束干预。修复并回归前不建议宣称剧本一通过。

## 剧本结论

| 剧本 | 状态 | 关键通过项 | 关键缺陷 |
|---|---|---|---|
| 01 需求变更 | **blocked (P1 stop)** | seq1–3 普通 `/sim` 可受理并出终答卡；能区分未知/证据不足；未把模型超时重试误当订单服务证据 | 干预总结未使用刚发生的三条虚拟角色发言；种子旧规则缺失；Run ID 未进正文 |

## 缺陷统计

- P0：0
- P1：1（`PL-FEISHU-001` 多角色会话上下文断裂）
- P2：1（`PL-FEISHU-002` 种子知识「重试两次」旧规则缺失）
- P3：1（`PL-FEISHU-003` Run ID 不便记录）

## 已完成回合质量片段

| 步骤 | 状态 | 要点 |
|---|---|---|
| seq1 product | passed | 有 ack + 终答；不承诺今天完成；列待确认口径；引用资料无 |
| seq2 developer | passed | 正确拒用无关「模型超时重试」证据；指出 interval 未知 |
| seq3 qa | passed | 识别验收缺口并建议可验证场景 |
| 干预 after-3 | passed_with_defect → 触发停 | 角色结构像样，但自称无逐句发言，未还原真实讨论 |

## 根因假设（供修复）

会话 `get_or_create(chat_id, user_id)` 使用 **effective** `user_id`：

- `/sim product` → session(virtual-product)
- `/sim developer` → session(virtual-developer)
- `/sim qa` → session(virtual-qa)
- 无 `/sim` 的干预 → session(真实 open_id)，几乎空上下文

因此群协作回放在「总结刚才讨论」时必然看不到各角色发言。

**建议修复**：群测/虚拟成员场景下，会话键使用 `physical_actor_id + chat_id`（或 chat 级共享会话）；`effective_actor_id` 仅用于权限与角色视图，不拆会话。

## 稳定性

- ack 超时：0（观察到的回合有 ack 或未单独计）
- 最终回复超时：0
- Hermes/网关错误：0
- 重复回答：未测
- 重启恢复：未测

## 未覆盖项（本轮因停测未做）

- 剧本一 seq4/5、结束干预
- 剧本二、三
- 权限隔离 / 幂等 / 重启 / 主动提醒

## 建议

1. **先修 P1 会话键**（虚拟成员不拆会话），加单测：同 chat 下 `/sim` 多角色发言后，无 `/sim` 提问能读到近期回合。
2. **补种子知识**：正式资料写入「支付失败重试为两次」旧规则，否则「文档 vs 群聊」验收仍会 `environment_not_ready`。
3. 修复后：`/clear` → **整本剧本一重跑**（不从干预续跑）；再决定是否进剧本二。
4. 可选：干预问题改为 `@飞书 /sim manager …`，但 **不能替代** 会话键修复——否则 manager 仍看不到其他虚拟角色会话。

## 产物路径

- `evaluation/feishu_team_test/results/2026-09-30-feishu-requirement-change-001.jsonl`
- `evaluation/feishu_team_test/results/2026-09-30-defects.csv`
- `evaluation/feishu_team_test/results/2026-09-30-quality-report.md`
- `evaluation/feishu_team_test/results/screenshots/feishu-requirement-change-001-seq0{1,2,3}.png`
