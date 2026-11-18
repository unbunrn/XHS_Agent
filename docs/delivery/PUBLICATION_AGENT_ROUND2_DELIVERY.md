# 发布恢复 Agent：Round-2 返修与离线故障恢复演示

日期：2026-11-18  
任务书：`D:\CodexSync\work\tasks\xhs-publication-recovery\round-2.md`  
状态：**离线实现与演示完成（含回滚副本演练与首次 unknown 自动对账）；未部署、未开启真实发布、未重放历史失败稿。`publication_agent.enabled=false`，live DB 仍为 schema 10。**

本报告只证明离线代码、SQLite、worker、实际 Xiaohongshu HTTP adapter（模拟桥接 HTTP）和飞书出站消费路径；不证明真实飞书、真实扫码登录或真实平台发布。

## 1. 先复现的两个反例

### A. “列表属于谁？”

复现：旧 `_capture_pre_send_baseline` 在 identity 读取失败时仍记录 `read_ok=true`、`account_identity=null`；`_reconcile_from_feed` 对旧同标题笔记只做标题匹配，导致重启对账把**另一个账号/旧笔记**认领为本次成功。

修复后规则：

- adapter 必须返回显式的“已验证账号 + 列表”：`{ok, read_ok, identity_ok, account_identity, notes, reason}`；`XiaohongshuPublisher` 从同一次 `/user/me` 读取 list/identity，identity 必须匹配 `expected_red_id`。
- 预发送基线持久化 `read_ok`、`identity_ok`、`account_identity`、note IDs；identity 失败不会被默认信任。
- 标题匹配要求：当前 identity 已验证、基线 identity 已验证、基线与当前账号相同且非空、基线可读、候选是基线之外**唯一**新增且标题完全一致。
- exact external id 是独立的“已绑定回执”路径，但同样要求当前账号已验证。
- 旧基线缺少 `identity_ok` 或没有基线：不可标题匹配，保持 unknown；不失败、不重发。
- 全程只读对账只在明确证据下改状态；证据不足不写任务/审批/attempt。
- 即时确认与重启对账共用同一规则：`publishers._feed_confirmation` 也调用 `lookup_published_notes_verified`，要求 `read_ok + identity_ok + expected_red_id` 与预发送唯一新增标题；不再直接读取原始 `/user/me` 后按标题认领。
- 回归覆盖实际 `XiaohongshuPublisher` + `FakeBridgeHttp`：预发送账号 A、发送后账号 B 出现同名新帖，POST 返回 200 无 ID 与 504 两种情况都保持 `unknown`、无成功通知、无第二次 POST；同账号唯一新增笔记仍能确认成功。

覆盖：`tests/test_publication_round2.py::Round2EvidenceTests`、`tests/test_publication_round2_demo.py`、`tests/test_publication_handoff_recovery.py`。

### B. “检查原因不能改变发布决定”

复现：任务 `paused`，恢复批次模型预算为 0；飞书 `检查原因` 触发诊断 job，旧 runner 在预算分支把 task 改成 `waiting_owner`。

修复后规则：

- `diagnose_only` 是独立权限：只允许 inspect/health/login/assets/receipt/diagnostic notify/schedule diagnostic；禁止 publish、restore assets、request owner、普通 resume。
- 诊断可写自己的审计 step、模型预留和诊断通知，但不改 task 的 state/wait_reason/next_wake、不改 approval/attempt/assets，不终结取消，不改变“能否发送”。
- 预算耗尽/异常、重复无效动作、步数上限、允许集合外动作：统一走 `_diagnosis_stop`，通知“诊断未完成，作品仍保持原状态，未改变发布安排”。
- `检查原因` 的派生 `schedule_resume` 保持 `diagnose_only`，不会变成普通发布 job；同一飞书事件重复投递只排队一次。

覆盖：`tests/test_publication_round2.py::Round2DiagnosisTests`、`Round2DiagnosisExtraTests`。

## 2. 代码变更

- `bot/adapters/publishers.py`：base `lookup_published_notes_verified` 默认拒绝信任；XHS 单次 `/user/me` 返回 identity+notes；保留旧 `lookup_published_notes` 兼容。
- `bot/services/publication_agent/live.py`：严格基线；`preview_result` 只读；`publish` unknown 持久化 `last_error_class=unknown`；`_capture_pre_send_baseline` 记录 identity。
- `bot/services/publication_agent/tools.py`：诊断工具白名单与执行校验；`check_publish_result` 在诊断态走 preview；`schedule_resume` 诊断态不改状态。
- `bot/services/publication_agent/runner.py`：诊断早返回/停止语义；预算、异常、无效决策、步数、取消均不改主任务；正常路径保留 R1–R6 行为。
- `bot/services/publication_agent/entry.py`：`修复并重试` 遇到 unknown 只入队一次正式对账 job（`reconcile_only`），不重置任务、不重新提交；只有 `确认未发布并重试` 可授权新提交。`检查原因` 仍是独立只读诊断。
- `bot/services/publication_agent/live.py`：首次记录 unknown 时，在**同一 SQLite 事务**内更新任务为 `waiting_owner` 并入队 `reconcile_only` 对账 job；不再等待主人发“修复并重试”。自动与手动入口使用同一稳定键（`unknown-attempt:{attempt_id}`）及同一排队函数。
- `bot/services/publication_agent/entry.py`：`wake_unknown_reconciliation` 自动/手动复用同一条 pending/running 对账链；`修复并重试` 会复用当前退避 job，不会另建链。`recover_unknown_reconciliations` 以最新 `publish_attempts.state='unknown'` 的已保存发送记录为准（不依赖 `publication_tasks.last_error_class` 这个旧行提示）：已有 pending/running 对账 job（无论是首个立即查询还是延迟退避查询）就不动；没有有效查询才补一个；`reconcile_wait >= 3` 已达到停止条件，不再自动续跑。
- `bot/services/publication_agent/store.py`：`update(..., conn=...)` 允许任务更新与 job 入队共享调用方事务；这是首次 unknown 原子化的基础。
- `bot/worker.py`：startup 调用 missing-job 恢复；恢复只入队只读对账，不提交、不改变审批/配额。
- `bot/services/publication_agent/runner.py`：新增 `reconcile_only` 执行分支，只调用一次 `check_result`，不调用模型、不提交；查不到时按 30/60/120 秒排队重试并通知主人，三次后停在 `waiting_owner` 并提示人工核实。成功时对原 attempt 写 note ID 并由队列路径发送成功回执。
- `bot/services/publish.py`：`publish_approval(..., before_send=...)`，在 claim 之后、dispatch/POST 之前持久化预发送证据；回调失败不阻断 POST，但会失去标题对账能力。
- `bot/services/publication_agent/store.py`：`record_pre_send_baseline`；任务创建传入配置化资产根目录/备份根目录；`pre_send_baseline_json` 列。
- `bot/services/publication_agent/diagnostics.py`：manifest 复用按调用方 target roots 重新校验；短临时文件名避免 Windows MAX_PATH 260 失败；单个资产备份失败记录 `backup_error` 而不是丢掉整份 manifest。

## 3. 离线故障恢复演示

测试：`tests/test_publication_round2_demo.py`，只替换外部 HTTP 桥接和时钟，不直接改 DB 完成任务。

### 场景 1：资产缺失 → 同字节恢复 → 一次成功 → 通知重启

1. 飞书批准真实 pending approval，生成真实 publication task；冻结包记录账号、审批、标题、正文、图片顺序和 hash。
2. 删除首张已批准图片。worker 用实际 `XiaohongshuPublisher` + `ScriptedLLM([verify_assets, publish])`，`bridge.posts == []`；task `waiting_owner`，飞书通知包含“发布前被拦截 / image missing”。
3. owner 发 `修复并重试 <code>`；runner 用 `restore_approved_assets asset_ids=["1"]` 按程序 manifest 恢复精确 bytes，再 `verify_assets`、publish。结果：`published`，`bridge.posts` 恰好 1 次，note id `note-1`。
4. 模拟成功 DB 已提交但通知发送失败；重启 worker 后发送 pending 成功通知，不产生第二次 POST。
5. 重放同一飞书批准事件：不创建第二个 task，不增加 POST。

### 场景 2：发送超时/unknown → 队列对账退避 → 晚到笔记认领原 attempt

1. bridge 对 POST 返回 504 且 feed 无可读新增；实际 adapter 返回 `PublishUnknown`，task `waiting_owner`，attempt `unknown`，`bridge.posts == 1`。该 unknown 记录与首个 `reconcile_only` job 在同一个事务中落库，**不需要主人发任何消息**。
2. 重启 worker 后，队列中的对账 job 自动运行：第一次仍查不到，任务保持 `waiting_owner`，通知“暂未确认”，并排入 30 秒后的自核对；之后按 60/120 秒退避，不产生新 POST。若三轮仍查不到，停在 `waiting_owner` 并通知主人“结果仍未知”；不报成功、不自行重发。
3. 平台随后出现唯一同标题新笔记；推进冻结时钟并启动新的 worker，队列 job 凭预发送基线认领**原 attempt**，写入真实 note id，`bridge.posts == 1`，成功通知由 notifier 消费。测试不直接调用 `check_result`。
4. 如果主人同时发 `修复并重试`，命令只复用当前 pending/running 对账 job，不再建第二条链；空链时才用该消息的 event id 建一条显式链。回归断言 active reconcile job 始终为 1，POST 始终为 1，成功通知只出现一次。
5. 模拟两个真实中断时机：
   - unknown 已落库、尚无对账 job：startup 恢复后 active 查询数 `0→1`，随后由队列推进并认领原 attempt；POST 仍为 1。
   - unknown 已落库、延迟退避查询已存在：startup 恢复后 active 查询数 `1→1`，job id 与 `next_run_at` 均不变；POST 仍为 1。
   - 已达到 3 次停止条件：startup 恢复新增 `0` 条，不重新启动查询，POST 仍为 1。

以上演示记录的是“模拟桥接 HTTP 成功/失败”，不是真实小红书已发布；飞书出站由测试 notifier 消费，不是真实飞书收件。

## 4. 测试与迁移演练

```text
PYTHONPATH=. PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m unittest \
  tests.test_publication_round2 \
  tests.test_publication_round2_demo \
  tests.test_publication_handoff_recovery \
  tests.test_publication_recovery_fences \
  tests.test_publication_agent \
  tests.test_publication_r4_r6 \
  tests.test_publication_schema10_compat \
  tests.test_xhs_identity -q -b
```

实际：`Ran 77 tests in 517.425s`，`OK`，退出码 0。  
主全量：`Ran 354 tests in 943.785s`，`OK`，退出码 0；仅 asyncio ResourceWarning。日志：`var/tests/full_suite_round2_startup_attempt_source.log`。  
定向覆盖：8 个 publication/xhs 模块（含新增自动对账、单链、startup 0→1/1→1 和停止条件测试）。

Migration 11 临时副本演练（`var/tests/rehearse_migration11_handoff.py`）：

```text
PRE {'version': 10, 'approvals': 40, 'publish_attempts': 26, 'integrity': 'ok'}
POST {'version': 11, 'approvals': 40, 'publish_attempts': 26, 'integrity': 'ok'}
baseline_column True
task_table True
work_index True
MIGRATION_REHEARSAL_OK
```

live DB 未被修改；副本/备份在 `var/tests/migration11_handoff.sqlite3`、`var/tests/migration11_handoff.before.sqlite3`。

## 5. 回滚说明

独立步骤见 [`PUBLICATION_AGENT_ROLLBACK.md`](PUBLICATION_AGENT_ROLLBACK.md)。核心：

- 迁移后尚无新业务写入：可停服务、保存当前库副本，再评估回退；不要在不核对 queue/notification/attempt 的情况下直接恢复部署前库。
- 已出现新批准、发送或 unknown：停止新执行并冻结当前 DB；逐条 platform 对账；保留新表/attempt/notification 事实；不要用旧库覆盖并丢掉新发送。
- 旧代码无法兼容新 schema/状态时：保持服务停止，前向修复；不以“恢复旧 DB”作为默认回滚。

## 6. 回滚副本演练：“灰度已发成功、回退后不再发”

测试：`tests/test_publication_round2_demo.py::GrayDeploymentDemo.test_rollback_after_gray_success_does_not_resend`。

1. 在离线环境批准并实际走一次发布，任务/审批为 `published`，`publish_attempts` 1 条、桥接 POST 1 次。
2. 用 SQLite 在线备份 API 生成临时副本 `var/tests/rollback_after_gray.sqlite3`，校验 `integrity_check=ok`；原 live/测试库不再参与后续步骤。
3. 关闭 `publication_agent.enabled`，重复投递原飞书批准事件，并让当前 worker 处理该副本的队列。
4. 结果：`publication_tasks` 仍 1、`publish_attempts` 仍 1、桥接 POST 仍 1，任务和审批仍 `published`。副本不是用旧代码回退，但证明了“已成功落库 + 关闭新执行 + 重放事件”不会二次发送；旧代码兼容性仍需维护窗口验证。

证据：`var/tests/rollback_after_gray.json`（task 1，attempt 1，POST 1，replay outcome `approved`，任务/审批均 published，integrity ok；具体 approval code 以该 JSON 为准）。

## 7. 未完成/未验证

- 未部署；migration 11 未应用于 live；`publication_agent.enabled=false`。
- 未真实飞书收发、未真实扫码登录、未真实平台发布；演示中的 HTTP/飞书均为模拟。
- 登录失败后的 `waiting_login` → `已登录` 续跑已有离线演示（同任务、同批准、一次 POST）；真实手机扫码、真实飞书出站仍需部署后验收。
- 取消后晚到成功、暂停旧 job、重复事件等由单元/穿透测试覆盖，未全部重复进单一大演示。
- 自然语言/无审批码唯一目标、真实桥接阶段日志/截图/toast/2 分钟 deadline 仍未交付。
- 两个并发自然语言任务不串线尚无真实飞书演示；依赖规范命令和显式审批码。
- 回滚已完成离线副本演练（见第 6 节）；未用旧代码对真实 live DB 演练，旧代码兼容性仍需维护窗口验证。
