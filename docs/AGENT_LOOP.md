# P0 内容 Agent：审稿、自适应检索与离线评估

日期：2026-11-12  
状态：**P0 代码完成，离线全量 322 项通过（778.082s）；2026-11-12 追加“写稿证据不足自动补检”并有 77 项定向测试通过。worker 已于 21:40 重启加载 auto_rescue（PID 19632），live 已按 owner 要求关闭 `critic_enabled`（审稿代码/测试保留），`adaptive_research` 与 `auto_rescue` 开启。** 未做真实 20-topic 评测，不报告效果提升；审批、来源身份、SSRF、发布闸门不变。

## 1. 为什么加这三层

原有链路是固定工作流：检索 → 身份判定 → 写稿 → 审批 → 发布。它可靠，但没有
“观察结果 → 判断缺口 → 再行动”的闭环，也不会在发布前独立审稿。P0 增加三层
**有界**能力：

1. **Editor/Critic**：写稿后独立审稿，可 `pass` / `revise` / `reject`，最多一轮重写。
2. **Adaptive research**：首轮计划后检查覆盖缺口，最多补 2 个检索式；补检仍走正文、
   时效、来源身份、物理抓取和预算上限。
3. **Evaluation harness**：用统一指标比较“固定工作流 / +自适应检索 / +审稿”三种
   arm，避免只凭感觉说“更智能”。

它们都是质量层，不是安全边界：不能跳过 owner approval，不能把摘要变成官方证据，
不能凭 HTTP 2xx 认定发布成功。

## 2. Critic 流程

`bot/services/critic.py` 只做结构化审稿：

- 输入：事件、最多 6 条 evidence、已通过确定性校验的草稿、标题/正文限制。
- 输出：`pass | revise | reject`、问题列表（`kind`、`evidence_ids`、`instruction`）、
  置信度。
- `revise` 时调用写稿模型，携带审稿意见和 `current_draft`，重新跑
  `validate_draft_payload`，再审一次；超过 `agent.max_critic_revisions` 即失败。
- `reject` 直接失败，不生成 draft/approval。
- 审稿服务临时不可用（`LLMError`）只记录 `draft.critic_unavailable`，仍交给人工审批；
  但**格式错误/语义不通过**不会被静默当成 pass。
- 审计：`draft.critic_review`、`draft.critic_revised`、`draft.critic_failed`。

配置（live 因 owner 要求关闭，示例默认关闭；代码保留）：

```toml
[agent]
critic_enabled = false
critic_max_output_tokens = 900
max_critic_problems = 5
max_critic_revisions = 1
```

## 3. Adaptive research 流程

`bot/services/adaptive_research.py` 在首轮 `run_plan` 后做确定性覆盖检查：

| gap | 触发条件（示例） | 补检查询 |
|---|---|---|
| `official_source` | 配置了可信域，但没有任何命中来源的正文 | `topic site:configured-domain` |
| `independent_verification` | 只有 1 个独立主机 | `topic 独立 报道 评测` |
| `key_numbers` | 主题涉及价格/成本/参数/版本，正文没有数字 | `topic 参数 价格 版本` |
| `counter_evidence` | 主题是对比/选择，独立来源少于 2 个 | `topic 限制 问题 争议` |

`coverage_decision()` 只提出 0–2 个查询；`run_followups()` 复用同一个 research
callable、同一物理抓取配额和同一 topic/时效/身份链路，并把结果合并进 `PlanRun`。
停止原因写入 `diagnostics.adaptive`：`coverage_sufficient`、`fetch_limit`、
`deferred`、`source_error`、`max_queries`。

配置：

```toml
[topics]
adaptive_research = true
adaptive_max_queries = 2
```

## 4. Failure auto-rescue（写稿证据不足）

当 writer 因“证据不足/缺少某类事实”拒绝，且 `topics.auto_rescue=true` 时，
`process_topic_request` 做**一次**有界补检：

1. 从 writer 错误中提取 `缺少 …` 的缺口描述，生成 1 条查询；
2. 用独立的小物理配额 `topics.rescue_max_fetch`（默认 2）抓取候选正文；
3. 仍走正文长度、主题匹配、时效、来源身份、共享预算和 SSRF 闸门；
4. 只把 `verified` 或 `usable` 的证据写回**同一个事件**；
5. 用新证据重试**一次** writer；第二次仍失败就按普通失败返回，不再循环。

它不创建新选题、不重发旧审批、不绕过 owner。审计
`topic.writer_rescue_started/completed/failed`；禁用时保持原来的失败行为。

```toml
[topics]
auto_rescue = true
rescue_max_fetch = 2
```

## 5. Evaluation harness

`bot/services/agent_eval.py` 是**离线 replay 指标层**，不调用模型或平台。每条记录
代表一次运行结果：

```json
{"case_id":"t01","arm":"critic","passed":true,"official_found":true,"claims":12,"unsupported_claims":0,"queries":5,"cost_usd":0.04,"latency_s":55,"human_edited":false}
```

指标定义：

- `first_pass_acceptance`：无需人工修改即通过的比例。
- `official_source_recall`：找到至少一个官方/一手来源的比例。
- `unsupported_claim_rate`：无证据 claim 数 / claim 总数。
- `human_edit_rate`：人工修改/拒绝比例。
- `avg_queries`、`avg_cost_usd`、`avg_latency_s`。

命令：

```bash
.venv/Scripts/python.exe scripts/agent_eval.py --records docs/examples/agent_eval_records.jsonl
```

`docs/examples/agent_eval_records.jsonl` 只是格式样例，不是模型质量结论。正式评估
应固定约 20 个近期选题，分别跑三种 arm，记录真实模型调用和人工修改结果；同一
`case_id` 在三个 arm 中必须使用相同输入。没有真实记录时不得声称“评测证明提升”。

## 6. 测试

新增：

- `tests/test_critic_agent.py` 6 项：pass、revise→pass、reject、畸形审稿、修订轮数上限、
  关闭 critic 不调用。
- `tests/test_adaptive_research.py` 6 项：缺口、覆盖充分、抓取上限、deferred、补检合并、保留 gaps。
- `tests/test_failure_rescue.py` 5 项：只在证据拒绝时触发、缺口查询、关闭不触发、
  端到端“失败→补检→一次重试→完成”、审计记录。
- `tests/test_agent_eval.py` 3 项：指标、非法 arm/claim、空输入。

回归：`tests.test_failure_rescue + test_critic_agent + test_adaptive_research +
test_agent_eval + test_approval_publish + test_research_s2 + test_aihot +
test_xhs_identity` = **77/77 OK**（97.366s）。P0 全量 322 项通过
（778.082s，日志 `var/tests/full_suite_rescue.log`）。live worker 已加载
rescue（PID 19632，console/gateway 未变），`/healthz` 正常；
live `critic_enabled=false`，审稿未启用；`auto_rescue=true`、
`adaptive_research=true`；未做真实 20-topic 评测。

## 7. 限制

- 自适应查询是规则驱动的，不会自己发明新的检索策略；后续可让模型在受控 schema
  下提出查询，但必须保留同样的上限和证据闸门。
- Critic 只判断“稿子是否被 evidence 支持”，不判断外部事实真伪；官方来源仍需
  source identity 和 owner 审阅。
- 评测样例不能代替真实 20-topic 评测；未跑真实模型前不报告提升数字。
- 没有编辑反馈记忆、工具注册表/MCP 或微调；这些是后续可选项，不是 P0 的完成条件。
