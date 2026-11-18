# 发布恢复 Agent：handoff 补充返修交付报告

日期：2026-11-18  
依据：`D:\CodexSync\work\tasks\xhs-publication-recovery\handoff.md`、`docs/DSH_PUBLICATION_RECOVERY_AGENT_TASK.md`  
范围：两个已知反例（诊断派生 job 越权发布、旧同标题笔记误认成功）、诊断只读、发送前证据基线、补充集成测试。  
状态：**离线代码与测试完成；未部署、未迁移 live、未真实发布、未重放历史失败稿。`publication_agent.enabled=false` 和 live schema 10 保持不变。**

## 1. 两个反例：先失败，再修复

### A. 检查原因派生的 schedule_resume 不应变成正常发布

**修复前**：`检查原因` 入队的诊断 job 带 `diagnose_only=true`；模型调用 `schedule_resume` 后，`runtime.schedule` 只写 `{"task_id": ...}`。job 到期时 worker 以普通 runner 运行，模型可以发布原审批。  
**测试**：`tests/test_publication_handoff_recovery.py::DiagnosisIsolationTests::test_diagnose_schedule_resume_cannot_publish_a_normal_job`。把原审批 job 推到 2030 隔离，跑完诊断派生 job 到到期，断言 `publisher.calls=0`、没有 publish attempt、任务未发布。修复前失败，修复后通过。

**修复**：

- `PublicationContext.diagnose_only` 显式传递；
- 诊断只允许 `inspect_task`、`check_bridge_health`、`check_login`、`verify_assets`、`inspect_failure`、`check_publish_result`、`notify_owner`、`schedule_resume`；
- `PublicationTools.execute` 在工具执行处拒绝发布、资产恢复、owner request 等写操作；
- 诊断中的 `schedule_resume` 不改任务状态，调用 `runtime.schedule(..., diagnose_only=True)`；派生 job 的 payload 保留 `diagnose_only`，并用 `:diagnose` 去重；
- runner 诊断模式不写 `diagnosing`，可读取 `waiting_owner/login/approval/paused/retry_wait`，不发布、不空转。

### B. 旧同标题笔记不能被重启对账当成新成功

**修复前**：发送返回 2xx 但无 note id；`_reconcile_from_feed` 读账号 feed，只要恰好一条标题相同就标成 published。账号里早已存在同标题旧笔记时，会误认成功。  
**测试**：`RestartReconciliationTests::test_preexisting_same_title_note_is_not_success_after_restart`。实际 `XiaohongshuPublisher` + `FakeBridgeHttp` 返回 2xx 无 id，feed 有旧同标题笔记；首次 attempt 为 unknown，重启后对账仍为 unknown/waiting_owner，审批不发布。修复前失败，修复后通过。

**修复**：

- migration 11 的 `publish_attempts.pre_send_baseline_json` 保存发送前账号、时间、feed note ID 基线和 `read_ok`；
- `publish_approval` 在 claim 后、真正 dispatch 前调用 `before_send`；baseline 写失败不阻塞 POST，但后续无法用标题认领；
- `_reconcile_from_feed` 只接受：当前账号匹配，且（a）exact external id，或（b）基线可读、基线外**唯一**新增且标题完全匹配的笔记；
- 账号切换、baseline 缺失/读取失败、0 个或多个候选都保持 unknown；不把“没找到”当成“未发布”；
- 旧 attempt 没有 baseline 时只能靠 exact id，不能靠标题。

## 2. 补充测试

`tests/test_publication_handoff_recovery.py` 共 7 项，全部通过：

- 诊断派生 job 不能发布原审批（`POST=0`）。
- 旧同标题笔记在首次/重启后都不被认领。
- baseline 之外唯一新增笔记可以认领（`POST=0`）。
- 两个同标题候选保持 unknown。
- baseline 读取失败不能按标题认领。
- baseline 账号与当前账号不同不能认领。
- 飞书真实入站批准 → SQLite queue → worker → 实际 `XiaohongshuPublisher` + `FakeBridgeHttp`：`POST=1`、任务/审批 published。

原有 late-success、unknown 取消、暂停 gate、duplicate event、登录恢复、资产恢复、work identity、配额测试保留；对人工插入的 unknown attempt 增加了合法 baseline，未放宽断言。

## 3. 实际结果

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

主项目全量：

```text
PYTHONPATH=. PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m unittest discover -s tests -q -b
```

实际：**Ran 333 tests in 799.335s, OK**，退出码 0；日志 `var/tests/full_suite_publication_handoff_final.log`。仅有 asyncio 未关闭 ProactorEventLoop 的 ResourceWarning，不是测试失败。

## 4. Migration 11 临时副本演练

- 源：`var/live/bot.sqlite3`（schema 10；演练时 approvals 40、publish_attempts 26）。
- 副本：`var/tests/migration11_handoff.sqlite3`；演练前备份：`var/tests/migration11_handoff.before.sqlite3`。
- 连续两次 `Database.init()`：版本 11；`publication_tasks`、`work_key`、`ux_publication_task_work_live`、`publish_attempts.failure_class/retryable/pre_send_baseline_json` 存在；approvals/publish_attempts 行数保持；`PRAGMA integrity_check='ok'`。
- 演练前副本仍为版本 10、`integrity_check='ok'`；live 未修改。
- 结论：`MIGRATION_REHEARSAL_OK`。这只是副本演练，未部署。

脚本：`var/tests/rehearse_migration11_handoff.py`。

## 5. 尚未完成 / 未验证

- **未部署**：live schema 10、`publication_agent.enabled=false`；未真实发布、未重放历史 unknown。
- 真实飞书网络、真实桥接进程、真实登录扫码、真实平台 2 分钟 deadline 未验收；测试使用离线 SQLite/FakeBridge。
- 自然语言飞书意图、无编号多候选消歧仍未实现；当前主要依赖规范命令和审批码。
- 真实桥接的阶段日志、截图、toast、超时诊断未接入；`inspect_failure` 仍是本地 attempt/feed 证据。
- 资产恢复只在 fake 链路验证到恢复后 `validate_publish`，没有真实 POST。
- 通知去重/重启重发已有单进程测试，未做多进程并发压力；真实维护窗口回滚未执行。
- R5/R6 的既有离线边界仍成立；不能把本报告当作真实发布验收。

## 6. 单作品灰度部署顺序（未执行）

1. 维护窗口停 worker、gateway、console；确认无运行中 job、pending approval、publishing/unknown attempt。
2. 备份 live DB，校验 `PRAGMA integrity_check`。
3. 在 live 副本先跑 migration 11，再对正式库执行；验证版本 11、publication 表/列/索引、行数和 integrity。
4. 保持 `publication_agent.enabled=false` 启动，确认旧路径、飞书命令、worker/gateway 正常。
5. 只开一个新建批准、一个测试作品、一次真实发送；`max_model_calls=6`、`max_daily_model_calls=20`、`max_retries=1`。不要用历史 unknown 直接重放。
6. 核对 note id/URL、attempt、task、approval、通知一致后，再决定是否扩大；出现 unknown 先只读对账，不自动重发。
7. 回滚：停三服务，恢复上线前备份，保留 `publication_*` 审计表，核对 unknown 后再处理。

## 7. 主要文件

- `bot/services/publication_agent/{runner,tools,live,entry,store}.py`
- `bot/services/publish.py`
- `bot/schema.py`（migration 11：`pre_send_baseline_json`）
- `tests/test_publication_handoff_recovery.py`
- `tests/test_publication_recovery_fences.py`
- `var/tests/rehearse_migration11_handoff.py`
- 本报告、`docs/delivery/PUBLICATION_AGENT_DELIVERY.md`
