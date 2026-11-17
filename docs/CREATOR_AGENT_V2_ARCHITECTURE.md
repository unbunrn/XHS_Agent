# Creator Agent V2 — 架构

## 目标

把“固定搜索→写稿→审稿”的流水线，改造成有边界的模型驱动工具循环：

```
用户选题/链接/修改反馈
  → 入站去重 + CreativeBrief + durable job
  → AgentRunner: 观察 → 选择一个工具 → 程序校验/执行 → 记录 → 再观察
  → 内部 artifact（可不完整）→ render/layout/review → submit_for_approval
  → 真实 pending approval（旧审批表）→ owner 批准 → 旧发布闸门
```

模型只提出 `{action,args,intent_summary,expected_result,base_version}`；工具注册表、预算、幂等和终止状态由程序执行。V2 不把 `create_draft` 的自动审批路径当作内部写稿步骤。

## 主要模块

| 模块 | 职责 |
|---|---|
| `contracts.py` | 稳定数据契约：brief、decision、material、claim、artifact、review/revision |
| `store.py` | `creator_agent_runs/steps/artifacts/preferences` 的 SQLite journal |
| `context.py` | 单次 run 的材料、最新 artifact、runtime 适配器 |
| `registry.py` | 工具 schema、未知/缺失参数校验、结果封装 |
| `runner.py` | 动态循环、硬上限、checkpoint、gen/base_version 校验 |
| `tools.py` | 12 个工具 handler；不直接访问网络或发布 |
| `policy.py` | 提交前的声明/数字/来源/核心矛盾检查 |
| `entry.py` | 飞书入站、jobs、scheduler、artifact outbox |
| `live.py` | 真实 adapter：搜索/抓取/写稿/Pillow 渲染/布局检查/真实审批桥 |
| `agent_eval.py`（既有 P0） | replay 指标，不是 V2 runner 的在线评估 |

## 工具

`discover_topics`、`search`、`read_source`、`inspect_claims`、`recall_context`、`draft_artifact`、`revise_artifact`、`render_images`、`inspect_layout`、`review_artifact`、`submit_for_approval`、`ask_owner`。

`submit_for_approval` 是终止工具，创建真实 pending approval 行并绑定 draft/version/body/image hash；是否发布仍由 owner 批准和旧发布闸门决定，V2 不自动发布。

## 上限

`creator_agent`：`max_steps=8`、`max_revision_steps=6`、`max_review_cycles=2`、`max_searches=5`、`max_physical_fetches=6`、`max_model_calls=14`、`max_invalid_decisions=2`、`max_no_progress_steps=2`、`max_run_seconds=600`。隐藏传输重试也计入物理抓取。

## 恢复与幂等

- run 以 `run_key` 唯一；步骤按 `seq` 追加；artifact 按 `(run_key, version)` 保存。
- 每一步保存 counters/checkpoint；崩溃后从最后步骤继续。
- 入站按 Feishu `message_id` 去重；scheduler 按 `topic|url` 确定性 key 去重。
- artifact outbox 按 `run_key|version` 幂等；它不是 approval。

## 与旧系统的关系

V2 已复制到主项目并接入 live：旧审批、发布闸门、SSRF、迁移历史和真实数据仍保留；V2 的 `LiveRuntime` 是适配器，`FakeRuntime` 只用于离线测试。旧 `process_topic_request` 仍可运行，作为回退路径。
