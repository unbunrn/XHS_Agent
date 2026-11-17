# 从内容工作流到有边界的运营 Agent：课程大作业设计

日期：2026-11-12  
定位：**single-account, human-in-the-loop content operations agent**。不是“全自动发帖机器人”，而是把“发现 → 检索 → 证据判断 → 写作 → 审稿 → 人工批准 → 发布 → 对账”做成可观察、可停止、可评估的闭环。

## 1. 现在已具备什么

| 层 | 当前实现 | 为什么算 Agent 能力 |
|---|---|---|
| 感知 | 热点/RSS/AIHot/检索、正文抓取、时效过滤 | 从环境取观察，不是只接受一个 prompt |
| 决策 | 研究计划、来源身份判定、覆盖缺口检测 | 根据观察决定“还缺什么”，补 0–2 个查询 |
| 行动 | 补检、写稿、失败后一次证据补救、渲染 | 行动受预算/抓取/时效/来源/审批约束 |
| 反思 | 可选 Editor/Critic、`revise/reject` | 对产物做独立检查，而不是一次生成即结束 |
| 记忆 | 审计、身份缓存、事件/证据库、发布对账 | 可追溯；编辑反馈记忆尚未实现 |
| 治理 | 审批绑定、幂等、unknown 不重发、SSRF、配额 | Agent 不能绕过人类和平台边界 |

**边界**：审稿、补检、补救都是质量层，不是安全边界。审批、来源身份、发布闸门仍由确定性代码执行。

## 2. 目标架构

```text
owner topic
   │
   ▼
Planner ──► Research tools ──► Evidence store ──► Identity gate
   ▲              │                  │
   │              └── coverage gap ──┘
   │
   ▼
Writer ──► Critic (optional) ──► deterministic validation ──► owner approval
   │                                  │
   └──── evidence-gap rescue ◄────────┘
                                      │
                                      ▼
                              Publish + reconcile
                                      │
                                      ▼
                              audit / eval / memory
```

关键点：每个箭头都有**上限、停止条件和审计**。没有“模型想继续就继续”。

## 3. 课程实验设计

### 3.1 研究问题

1. 自适应补检是否提高官方来源召回与事实覆盖率，而不显著增加成本？
2. 独立审稿是否降低 unsupported claim 与人工返工率？
3. 失败补救是否减少“证据不足直接失败”，同时不增加错误发布？

### 3.2 数据

- 固定 20 个近期选题（10 个新闻、5 个分析、5 个对比/选购）。
- 同一 `case_id` 在四个 arm 使用相同输入、相同模型版本、相同预算上限。
- 每个 case 记录：运行时间、调用次数、费用、抓取次数、证据 ID、审批结果、人工修改、是否发布。

### 3.3 四组消融

| arm | adaptive | critic | rescue | 目的 |
|---|---:|---:|---:|---|
| A baseline | off | off | off | 固定工作流 |
| B +adaptive | on | off | off | 观察→补检 |
| C +critic | on | on | off | 产物反思 |
| D +rescue | on | off | on | 失败恢复 |

若算力有限，至少 A/B/D 三组；critic 单独小样本。

### 3.4 指标

- `official_source_recall`：至少一个官方/一手来源的 case 比例。
- `unsupported_claim_rate`：无 evidence 支持的 claim / 总 claim。
- `first_pass_acceptance`：无需人工修改即通过。
- `human_edit_rate`：人工修改或拒绝比例。
- `failure_recovery_rate`：失败后经补救得到可用稿的比例。
- `cost_per_accepted_draft`、`p50/p95 latency`。
- 失败分类：检索为空、正文抓取失败、时效、来源身份、证据不足、JSON、渲染、审批/发布。

### 3.5 统计与诚实结论

- 20 个 case 只能做描述性统计；报告均值、范围、失败明细，不做夸张显著性。
- 没有真实记录就不写“提升 X%”。工程测试通过不等于模型质量提升。

## 4. 如果老师要求“微调”

不要微调整条发布链路。优先选一个**边界清晰、可评测**的子任务：

1. **claim-support 分类器**：输入 `(claim, evidence quote)`，输出 supported / partial / unsupported。用现有 evidence 与人工修订构造数据；目标是替代/辅助规则化的 claim 检查。
2. **query planner 小模型**：输入 topic + 当前 coverage，输出 1 条补检查询；用 LoRA，评估 official recall / cost。
3. **风格/结构适配**：只微调 writer 的标题与卡片结构，不碰事实与审批。

数据构造：从审计表导出成功/失败 case；人工标注 300–1000 条；严格按 topic 划分 train/val/test，避免同一事件泄漏。基线必须是**同一 prompt 的非微调模型**，否则无法说明微调贡献。

## 5. 两周执行计划

**第 1 周**
- 固定 20-topic 测试集与运行脚本；补齐 eval 记录的自动导出（从 DB/审计生成，不再手写样例）。
- 跑 A/B/D（至少 20×3），保存原始 JSONL、日志、费用。
- 人工盲评 20 份稿件；记录修改类型。

**第 2 周**
- 根据失败分类修 1–2 个最高频问题（优先检索/证据，不先调文风）。
- 若做微调：claim-support 小模型 + 同 prompt 基线对比。
- 写报告：系统图、Agent 循环、消融表、失败案例、限制。

## 6. 答辩时最值得讲的三个点

1. **不是多 Agent 角色扮演，而是可验证的闭环**：每次补检/审稿/补救都有上限、停止原因和审计。
2. **安全边界与质量层分离**：模型可以建议，但不能批准、不能绕过来源身份、不能把 2xx 当发布成功。
3. **诚实的评估**：工程可靠性 + 小样本消融 + 失败分类；不把“能跑”说成“更聪明”。

## 7. 风险与不做的事

- 不开放无上限自主循环；不做自动审批/自动发布。
- 不让聚合摘要冒充官方；不把旧文当新闻。
- 不为了“Agent”标签堆多角色；没有评测收益的动态工具选择暂不引入。
- 不把 20 个 case 的均值包装成统计显著。
