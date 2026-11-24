# 发布与故障恢复 Agent：R1–R6 返修交付报告

日期：2026-11-18  
依据：`docs/delivery/PUBLICATION_AGENT_INDEPENDENT_REVIEW_20261118.md`  
范围：R1–R6 返修、反例/穿透测试、migration 11 临时副本演练、文档更新。  
状态：**代码返修与离线验证完成；未部署、未迁移 live、未真实发布、未重放历史失败稿。`publication_agent.enabled=false` 保持不变。**

> 这份报告不把“302 项旧回归通过”当作本轮返修完成。旧基线是返修前的证据；本轮新增了针对审查反例的测试，结果见第 4 节。真实飞书/桥接/平台仍未验收。
>
> **2026-11-18 补充返修**：按 handoff 又修了“诊断派生 job 变成正常发布 job”和“重启对账把账号里旧同标题笔记当成新成功”两个反例，并给每次发送增加预发送账号/feed 基线。新增报告见 [`PUBLICATION_AGENT_HANDOFF_DELIVERY.md`](PUBLICATION_AGENT_HANDOFF_DELIVERY.md)；最新定向 **71/71**、主全量 **333/333** 通过。旧 64/326 数字保留为上一轮证据。
>
> **2026-11-18 Round-2**：又修复“identity 缺失仍按旧同标题认领”和“检查原因改变 paused/waiting_owner 发布决定”，新增身份+列表同源核验、严格预发送基线、诊断只读及完整离线演示。最新报告见 [`PUBLICATION_AGENT_ROUND2_DELIVERY.md`](../../delivery/PUBLICATION_AGENT_ROUND2_DELIVERY.md)，回滚见 [`PUBLICATION_AGENT_ROLLBACK.md`](../../delivery/PUBLICATION_AGENT_ROLLBACK.md)。本页 71/333 是 Round-2 之前证据。

## 1. 本轮修复摘要

### R1：确定失败与未知结果分离

- `PublishOutcome` 增加 `failure_class`/`retryable`；`publish_attempts` 增加同名列（migration 11）。
- 登录失效、账号不匹配、资产缺失、平台明确 4xx 拒绝使用稳定代码；只有 `not_dispatched`、`connection_before_send`、`bridge_unavailable` 等确定未发送结果可进入发送重试。
- 5xx、发送后读超时、无回执、2xx 但无 note id/URL 一律 `unknown_result`，`retryable=False`；live runtime 不再对所有 `failed` 自动 bump/重排。
- 实际 XHS adapter 的 500 + feed 延迟出现笔记测试断言 `POST=1`；同一链路经 gate/agent 后晚到笔记被记为 `published`，不再发第二次 POST。
- 旧测试 `test_5xx_with_readable_unchanged_feed_is_failed` 已改为 `..._is_unknown`；`FakePublisher` 的失败测试显式提供 `failure_class="not_dispatched", retryable=True`，不再依赖“所有 failed 都重试”。

### R2：最终执行闸门

- `publish.py` 的 claim 事务增加 task 级 fence：task 状态必须为 `ready`/`diagnosing`/`retry_wait`，`cancel_requested=0`，generation、冻结包 hash、lease owner 必须匹配，且不能有活动/未决 attempt。
- `validate_publish`、`claim_publish`、`publish_approval` 贯通这些参数；`claim_publish` 在写事务内重新核验，不能只信工具预检快照。
- runner 对 `paused`、`waiting_*`、`retry_wait` 未到期、`published` 的旧 job 不再无条件改 `diagnosing`；每次循环和 claim 前重查取消/等待状态。
- 反例覆盖：暂停后旧 job `POST=0`；工具预检后取消 `POST=0` 且不建 attempt；stale generation 不能 claim；未知取消不终结任务；晚到成功优先且 `POST` 不增加。

### R3：平台对账与 unknown 取消

- `check_result` 在 attempt 为 `unknown`/`publishing` 时调用平台账号 feed（`lookup_published_notes`）做只读对账；发送前保存账号/package 时间/feed note ID 基线，只有“基线之外唯一新增且标题完全匹配”的笔记，或 exact external id，才能把 attempt/task 标为 `published`。账号切换、基线缺失/读取失败、多个候选都保持 unknown。
- feed 不可读/无匹配时保持 `waiting_owner`，不猜测失败；晚到成功写真实 note id/URL、审批/草稿/事件状态，不自动删帖。
- `取消发布/拒绝发布` 遇到 unknown/in-flight 只设置 `cancel_requested`，并唤醒只读对账，不再把 task 直接 `cancelled`。
- `确认未发布并重试` 绑定最新 unknown attempt id 与 task generation，CAS 失败返回“已变化”；确认前若出现成功证据则阻止重发。

### R4：预算、恢复与唤醒

- 新增 `PublicationBudgetExceeded`；runner 区分每轮/恢复批次/日累计预算，并在模型 transport 前持久预留 `model_attempt`（`before_attempt`），成功后记录 `model_decision`，失败/重试不会把历史步骤误当真实调用。
- 预算耗尽进入 `waiting_owner` 并生成确定性、脱敏的待协助通知；不会静默把 job 标完成。
- `继续发布`/`已登录`/`修复并重试` 创建受限新批次并保留总消耗；`检查原因` 只入队 `diagnose_only`，禁止发布动作。
- 唤醒 job 以 inbound message/event ID 去重，不再用固定 generation；worker 异常返回 `retry`，不伪装成功完成。

### R5：资产不可变备份与原子恢复

- 程序在批准包冻结时生成不可变 manifest（asset id、序号、hash、目标、备份、可用性）；模型工具只传 `asset_ids`，不能传任意备份路径。
- 备份/恢复校验源和目标都在批准根目录，拒绝符号链接/路径穿越/hash 不符；先写临时文件、核验 hash 和图片、再 `os.replace` 原子替换。
- 缺少备份时保持等待，不重画/不替代；`verify_assets` 通过才算恢复成功。
- 新增“缺文件→按 manifest 恢复相同字节→`validate_publish` 通过”的穿透测试。

### R6：来源归属、配额、作品身份、包与 simulated

- `research.sync_sources` 保留 V2 `v2-{run_key}` commissioned 来源，不再只按旧 `topic_requests` 名称判断。
- 发布配额按 task/work 计数，安全重试不重复消耗作品名额；另有 `limits.max_publish_attempts_per_task`（默认 3）限制单任务尝试数。
- `publication_tasks.work_key` 使用账号/平台/草稿版本/标题/正文 hash/有序图片 hash 的语义身份，并建未取消任务的唯一索引；同一作品换审批码不能创建并行发送任务。
- claim 时重新核验冻结包 title/tags/visibility/有序图片/正文 hash/审批 hash，不只是“JSON 存在”。
- `simulated` 从 `published` 分离：模拟导出只写 `simulated`，界面/通知不得声称真实发布。

## 2. 反例与穿透测试

新增/更新的关键测试：

- `tests/test_publication_recovery_fences.py`
  - 暂停后旧 runner：`POST=0`。
  - 工具预检后取消：无 attempt、`POST=0`。
  - stale generation：不能 claim。
  - unknown 取消：task 非终态、attempt 保持 unknown。
  - 取消后 feed 晚到成功：`published`、`POST=0`。
  - 实际 XHS adapter 500 + feed 晚到笔记，经 gate/agent：`published`、`POST=1`。
- `tests/test_publication_r4_r6.py`
  - 恢复批次预算、owner 新消息恢复、同消息去重、诊断只读、worker 异常重试。
  - manifest 备份/恢复/越界拒绝/恢复后发布校验。
  - 冻结包 visibility/图片顺序、跨审批 work 冲突、simulated 与 published 分离。
  - 脱敏通知不编造成功、owner request 关闭/过期、V2 来源保留、安全重试不重复占配额。
- `tests/test_xhs_identity.py`：5xx 且 feed 无可读新增 → `PublishUnknown`；5xx 且唯一新增同标题 → published。
- `tests/test_publication_handoff_recovery.py`
  - `检查原因` 派生的 `schedule_resume` job 到期后仍只读，不能发布原审批，`POST=0`。
  - 发送 2xx 无回执、账号已有旧同标题笔记：首次和重启对账都不能认领成功。
  - 预发送基线之外唯一新增笔记可以认领；两个候选、基线读取失败、账号切换都保持 unknown。
  - 飞书真实入站批准 → worker → 实际 `XiaohongshuPublisher` + FakeBridge：`POST=1`。

## 3. 实际测试结果

```text
PYTHONPATH=. PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m unittest \
  tests.test_publication_agent \
  tests.test_publication_recovery_matrix \
  tests.test_publication_recovery_fences \
  tests.test_publication_schema10_compat \
  tests.test_publication_r4_r6 \
  tests.test_publication_handoff_recovery \
  tests.test_xhs_identity -q -b
```

实际：`Ran 71 tests in 217.391s`，`OK`，退出码 0。

主项目全量：**Ran 333 tests in 799.335s, OK**（本轮最新代码；日志 `var/tests/full_suite_publication_handoff_final.log`；仅有 asyncio ResourceWarning，不是失败）。上一轮 326/645.702s 是补充返修前证据；旧 302/465.024 基线不作为本轮通过证据。

## 4. Migration 11 临时副本演练

- 源：`var/live/bot.sqlite3`（schema 10；handoff 演练时 approvals 40、publish_attempts 26，无 publication 表）。
- 复制：`var/tests/migration11_handoff.sqlite3`；演练前备份：`var/tests/migration11_handoff.before.sqlite3`。
- 执行 `Database.init()` 两次：版本 11；`publication_tasks.work_key`、`ux_publication_task_work_live` 索引、`publish_attempts.failure_class/retryable/pre_send_baseline_json` 存在；行数保留；`PRAGMA integrity_check='ok'`。
- 演练前副本仍为版本 10、`integrity_check='ok'`；live 未被修改。
- 结论：`MIGRATION_REHEARSAL_OK`。这只证明离线 schema 演练，不代表已部署。

## 5. 未实现 / 未验证 / 边界

- **未部署**：`publication_agent.enabled=false`；migration 11 未应用到 live；live schema 仍 10；未真实发布/未重放历史 unknown 稿。
- 自然语言飞书意图、无审批码的唯一目标解析未实现；当前只支持规范命令。
- 真实桥接阶段日志、截图、toast、2 分钟 deadline 调查未交付；`inspect_failure` 仍以本地 attempt/feed 证据为主，部署时需按实际桥接版本核对能力边界。
- 维护窗口部署/回滚未执行；回滚必须停 worker/gateway/console 三者并使用上线前备份，不能上线后直接恢复旧 DB 丢失新事实。
- R5 的“恢复后 POST”已在 Round-2 离线 demo 的实际 XHS adapter + FakeBridge 链路上验证到一次 POST；真实桥接仍未验证。
- R4 通知去重使用 runtime 的确定性 digest，未做多进程/多重启并发压力测试。
- 真实飞书、桥接登录扫码、真实平台结果仍需部署后验收。

## 6. 部署方案（未执行）

1. 安排维护窗口，停止 worker、gateway、console；确认无运行中 job/审批/发布。
2. 备份 live DB 并校验 `PRAGMA integrity_check`。
3. 执行 `Database.init()` 应用 migration 11；验证版本 11、publication 表/列/索引、`integrity_check='ok'`。
4. 先保持 `publication_agent.enabled=false`，启动服务验证旧路径；再以 `enabled=true`、小预算（`max_model_calls=6`、`max_daily_model_calls=20`、`max_retries=1`）灰度。
5. 灰度只允许一个新建批准、一个测试作品、一次真实发送；用**新批准且未发布**的测试审批验证 task；不要用历史 unknown 直接重放；确认 note id/URL、审批、任务、通知一致后再扩大。
6. 回滚：停三服务，恢复上线前备份，保留 `publication_*` 历史表供审计；核对 unknown attempt 后再决定后续。

## 7. 主要文件

- `bot/adapters/types.py`、`bot/adapters/publishers.py`
- `bot/services/publish.py`
- `bot/services/publication_agent/{contracts,store,runner,live,entry,tools,diagnostics}.py`
- `bot/services/approvals.py`、`bot/worker.py`、`bot/services/research.py`
- `bot/schema.py`（migration 11）
- `tests/test_publication_recovery_fences.py`、`tests/test_publication_r4_r6.py`、`tests/test_publication_handoff_recovery.py`、`tests/test_xhs_identity.py`
- `var/tests/rehearse_migration11_handoff.py`（离线演练脚本）
- 本报告、`docs/PUBLICATION_AGENT_ARCHITECTURE.md`、`docs/PUBLICATION_AGENT_USAGE.md`
