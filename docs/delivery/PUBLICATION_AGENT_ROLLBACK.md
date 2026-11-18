# 发布恢复 Agent 回滚说明（离线演练版）

日期：2026-11-18  
适用：migration 11 / `publication_agent` 灰度。  
状态：**未部署，未在 live 执行；以下为维护窗口操作手册和离线演练结论。**

## 1. 先判断处于哪一类

### A. 迁移已应用，但没有新的批准、发送或 unknown

1. 停止 console、gateway、worker，确认没有 `publication_agent`/publish job 在运行。
2. 复制当前 DB，校验 `PRAGMA integrity_check`；保存 `publication_tasks`、`publish_attempts`、jobs、notifications 的事实副本。
3. 可以保持 schema 11 并关闭新执行路径（`publication_agent.enabled=false`），不一定要回退 DB。若必须回退到旧代码，先确认旧代码能读 schema 11；不能读时保持服务停止并前向修复。
4. 不要把部署前备份直接覆盖到当前库——即使还没有“新发送”，也可能已有新审批、通知或 queue 记录。

### B. 已出现新批准、发送、或 unknown

1. **停止新执行**：停 worker/gateway/console；不要重放历史 unknown。保留当前 DB 和通知/queue。
2. 冻结一份当前库一致副本并校验；记录所有 `publish_attempts`（尤其 unknown/publishing）、task、approval、draft、event 和 notification。
3. 逐条到平台只读核对：note id/URL/标题/账号/时间。不要用“本地 failed”推断平台未发。
4. 优先**关闭新执行路径、保留兼容数据**，而不是恢复旧 DB。旧库不包含新发送，覆盖会丢证据并可能造成再次发送。
5. 若旧代码不兼容新 schema/状态：保持停止，前向修复；不得为了“回到旧版本”而丢弃 unknown attempt。

### C. 灰度成功后的回滚

1. 先确认新 task/approval 已 `published`/`simulated` 且回执已记录；停新执行。
2. 保留当前 DB；不重放原 approval，不新建“补偿发送”。
3. 若必须回退代码，先验证旧代码不会把已发布审批重新当成可发送；无法验证时保持停止。
4. 重新启用旧路径前，用只读命令检查 `pending`、`publishing`、`unknown`、daily quota 和通知去重键。

## 2. 推荐命令顺序（维护窗口）

```powershell
# 1) 停三服务（按项目实际 PID 文件/脚本）
# 2) 备份并校验
.venv\Scripts\python.exe -m bot --config config/bot.live.toml doctor
# 3) 只读检查 DB（不要写 live）
# 4) 保持 publication_agent.enabled=false 启动旧路径
# 5) 灰度时再单独开启，限额 1 个测试审批/1 次发送
```

具体服务停止/启动以 `scripts/stop.ps1`、`scripts/start.ps1` 和当前 `var/run` 为准；不要依赖裸 PID。

## 3. 离线演练结论

- `var/tests/rehearse_migration11_handoff.py`：v10→v11 两次 init 幂等，行数保留，`integrity_check=ok`；副本恢复 v10 也 `ok`。
- `tests/test_publication_round2_demo.py`：已发布 task 重放同一飞书事件不新增 task/POST；成功 DB 已提交但通知失败后重启只补通知，不重发。
- `GrayDeploymentDemo.test_rollback_after_gray_success_does_not_resend`：SQLite 在线备份生成 `var/tests/rollback_after_gray.sqlite3`，关闭 `publication_agent.enabled` 后重放原批准事件并跑当前 worker；`publication_tasks=1`、`publish_attempts=1`、POST=1，任务/审批仍 `published`，`integrity_check=ok`。证据 `var/tests/rollback_after_gray.json`。
- 未用旧代码对真实 live DB 做回滚演练；“旧代码兼容性”必须在维护窗口单独验证。

## 4. 禁止事项

- 不用部署前 DB 直接覆盖有新增发送/unknown 的当前库。
- 不把 `修复并重试` 当作“确认未发送”。
- 不对 unknown 自动重发。
- 不在回滚时删除 `publication_*`、`publish_attempts`、jobs、notifications 证据。
