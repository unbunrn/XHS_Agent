# 精简交付报告（2026-11-19）

## 结论

本轮只做精简：删除已确认无运行入口的旧研究编排模块、旧 Worker 采集/草稿处理函数及其专属测试，归档已完成的历史任务书/审查/交付报告，并重写文档入口。主线仍是：

**飞书/定时选题 → Creator Agent 创作 → 本人审批具体版本 → Publication Agent 提交与对账 → 平台回执**

未改数据库结构或数据，未迁移，未重启/部署服务，未发送真实消息或帖子，未提交 Git。

## 复核证据

- 全仓 Python 引用搜索：`research_plan`、`research_orchestrator`、`adaptive_research`、`topic_research` 除彼此外，只被 `tests/test_research_s2.py`、`tests/test_adaptive_research.py` 引用；`bot/services/topics.py` 不导入它们；CLI、console、scripts、demo 中没有入口，也没有 `importlib` 字符串注册。
- Worker 旧函数 `ensure_collect_scheduled`、`_handle_collect`、`maybe_enqueue_draft`、`_handle_draft` 在 `handlers()` 和 `tick()` 中都没有调用；`tests/test_topic_auto_isolation.py` 与 `tests/test_collect_isolation.py` 的第二项是仅有的直接调用者。
- 只读查看 live DB（schema 11）：`collect` done 164/failed 19、`draft` done 5，均无 pending/running，因此删除旧处理函数不会留下无法执行的任务。未调用 `doctor`、`db.init()` 或迁移。
- 删除前 `git status` 基线为：`M README.md`、`M bot/services/approvals.py`、`M bot/services/publication_agent/entry.py`、`M live.py`、`M runner.py`、`M bot/worker.py`；未跟踪 `docs/delivery/PUBLICATION_SILENCE_FIX.md`、`docs/delivery/SESSION_HANDOFF_PUBLICATION.md`、`tests/test_feishu_help.py`、`tests/test_publication_silence_fix.py`。这些现有未提交改动均保留。

## 删除清单

### 旧研究编排模块

| 文件 | 依据 |
| --- | --- |
| `bot/services/research_plan.py` | 只被旧测试和其他待删模块引用；无运行入口。 |
| `bot/services/research_orchestrator.py` | 同上；V2 创作 Agent 不调用。 |
| `bot/services/adaptive_research.py` | 同上；`adaptive_research` 仅剩旧配置键。 |
| `bot/services/topic_research.py` | 同上；当前选题由 Creator Agent 发现/创作流程处理。 |

同时移除仅服务该组的：

- `bot/adapters/llm.py`：`RESEARCH_PLAN_SYSTEM_PROMPT`、FakeLLM 的 `purpose=="research_plan"` 分支、`_research_plan()` 方法、真实调用的 system prompt 分支。
- `bot/config.py`：`model_plan`、`model_plan_max_output_tokens`、`adaptive_research`、`adaptive_max_queries` 默认值。
- `config/bot.example.toml`：上述四个示例键；注释改为只描述来源身份判定。

### 旧 Worker 入口

`bot/worker.py` 删除：

- 常量 `NEXT_COLLECT_KEY` 与 `startup()` 中初始化 `next_collect_at` 的块；
- `ensure_collect_scheduled()`、`_handle_collect()`、`maybe_enqueue_draft()`、`_handle_draft()`；
- 只服务它们的 import：`create_draft`、`DraftError`、`drafts_created_today`、`pending_approval_count`、`ensure_research_queue`、`ingest_source`、`next_researched_event`、`recurring_sources`、`local_date`、`get_setting`。

保留：

- `sync_sources`：启动时仍同步 topic 来源，`test_topic_source_sync` 覆盖；
- `ingest_source`：仍是有效维护/来源入口，`test_collect_isolation` 直接覆盖其“单个无 query 来源不抛异常”的约束；
- `rng` 构造参数和 `self.rng`：`tests/test_worker_e2e.py` 仍显式传入 `rng=random.Random(...)`，保留公开构造参数兼容，不再用于已删除的采集抖动。

### 退休测试

| 测试 | 处理 | 原因与接替 |
| --- | --- | --- |
| `tests/test_adaptive_research.py` | 删除 | 只测已删除的自适应研究循环，无运行入口。 |
| `tests/test_topic_auto_isolation.py` | 删除 | 只测 `maybe_enqueue_draft` 的旧自动草稿选择，V2 无该入口。 |
| `tests/test_research_s2.py::PlanTests` | 删除 | 只测 `build_research_plan`；`IdentityTests` 全部保留。 |
| `tests/test_collect_isolation.py::test_recurring_collect_skips_commissioned_topic_rows` | 删除 | 只测旧 `_handle_collect`；同文件保留的 `test_ingest_source_returns_error_instead_of_raising` 继续覆盖查询缺失时的错误隔离。 |

## 保留候选与原因

- `bot/services/source_identity.py`：当前没有运行入口，但它不在任务点名的四个候选模块内；其测试记录了“自述官方不算证明、伪造 evidence_id 永不通过、缓存需精确材料”等证据规则。本轮保留，测试保留在 `tests/test_research_s2.py::IdentityTests`。
- 历史 schema 字段 `topic_requests.research_plan_json`、`source_identity_cache` 表和迁移：属于历史兼容，不随代码删除。`tests/test_migration_s8.py` 继续断言该列存在。
- `config/bot.live.toml` 中旧键 `adaptive_research` 等：未修改。代码已不读取，但未知键被配置加载器容忍，避免部署时因旧本机配置拒绝启动。
- `bot/services/aihot.py`、`drafts.py`、`research.py`、`critic.py`、publication/publish/approvals/notifications 等：任务明确不得整块删除，且仍有有效入口或测试覆盖。
- `agent_eval.py`、`reconcile_readonly.py`：分别由 `scripts/agent_eval.py` 和 CLI 使用，保留。

## 文档整理

- 将 45 份已完成任务书、旧轮次审查、旧交付报告和过时状态文档移到 `docs/archive/`（旧交付报告在 `docs/archive/delivery/`），保持原文件名与内容；新增 `docs/archive/README.md` 标明“历史资料，不代表当前状态”。
- 重写 `docs/README.md`：只保留根 README + Creator Agent 使用 + Publication Agent 使用三份首读入口，再列架构、学习、配置/桥接、回滚与当前交接；删除“当前新开发任务”等过时段落。
- 保留当前文档：根 `README.md`、`docs/PROJECT_ARCHITECTURE_WALKTHROUGH.md`、`docs/CREATOR_AGENT_V2_USAGE.md`、`docs/PUBLICATION_AGENT_USAGE.md`、`docs/PUBLICATION_AGENT_ARCHITECTURE.md`、`docs/CREATOR_AGENT_V2_ARCHITECTURE.md`、配置/桥接指南、`docs/delivery/PUBLICATION_AGENT_ROUND2_DELIVERY.md`、`PUBLICATION_AGENT_ROLLBACK.md`、`PUBLICATION_SILENCE_FIX.md`、`SESSION_HANDOFF_PUBLICATION.md`。
- 修正 `docs/PROJECT_LEARNING_GUIDE.md` 中指向已归档研究文档和已删除 `topics.py` 的链接；全仓 70 个 Markdown 文件的相对链接检查结果为 0 个断链（含新增的精简报告链接）。

## 配置兼容

- `config/bot.example.toml` 已删除 `model_plan`、`model_plan_max_output_tokens`、`adaptive_research`、`adaptive_max_queries`。
- `config/bot.live.toml` 未修改；其中的旧键仍被加载器容忍，不参与新逻辑。
- `bot/schema.py` 未改；没有新增或删除表/列，没有执行迁移。

## 验证

- 相关定向测试：`tests.test_research_s2 tests.test_collect_isolation tests.test_worker_e2e tests.test_feishu_help tests.test_config_budget tests.test_creator_agent_v2 tests.test_creator_daily_discovery tests.test_topic_source_sync tests.test_setup_metadata` → **64 tests，52.086s，OK**。
- 导入检查：`import bot.worker, bot.config, bot.adapters.llm` → OK；编辑文件 `py_compile` → OK。
- 配置加载：`load_config('config/bot.example.toml')` 与 `load_config('config/bot.live.toml')` 均成功；live 中已不读取的 `topics.adaptive_research=true` 仍被容忍，不影响加载。
- 公开 CLI：`python -m bot --help` → exit 0，帮助命令保留。
- 最终全量：`.venv/Scripts/python.exe -B -m unittest discover -s tests -v` → **363 tests，969.699s，OK**；日志 `var/logs/full-tests-simplification.log`。比上一轮少 11 个用例，均为本轮退休的旧入口测试或 `PlanTests` 中的计划用例。

## 最短学习路径

1. 根 [README](../../README.md)：日常使用、启动、命令速查。
2. [Creator Agent V2 使用说明](../CREATOR_AGENT_V2_USAGE.md)：选题、修改、审批边界。
3. [Publication Agent 使用说明](../PUBLICATION_AGENT_USAGE.md)：发布、状态、unknown 与恢复。
4. 需要理解内部结构时读 [项目架构走读](../PROJECT_ARCHITECTURE_WALKTHROUGH.md)。

## 明确未做

- 未迁移、重写或删除数据库数据；未调用 `doctor`/`db.init()`。
- 未重启、停止或部署任何服务。
- 未发送真实飞书消息或小红书帖子，未消耗真实模型额度做删除验证。
- 未提交、未推送 Git。
