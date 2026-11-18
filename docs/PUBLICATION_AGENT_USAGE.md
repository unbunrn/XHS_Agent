# 发布与故障恢复 Agent：使用

## 前提

1. 已有一个 `pending` 审批，owner 已批准或准备批准。
2. `config/bot.live.toml` 中：

```toml
[publication_agent]
enabled = true
max_steps = 8
max_model_calls = 12
max_daily_model_calls = 100
max_invalid_decisions = 2
max_retries = 3
retry_base_seconds = 30
retry_max_seconds = 1800
login_wait_seconds = 300
owner_wait_seconds = 3600
task_ttl_seconds = 86400
```

3. 先完成 migration 11（本版本尚未在生产执行）。migration 11 增加 `publication_tasks`/steps/owner requests、attempt 结构化失败列和 `work_key` 唯一索引。

## 批准后的行为

`批准 XH-XXXXXX` 成功后，系统创建 `publication_tasks` 和 `publication_agent` job，不再直接入队旧 publish job。

worker 运行模型决策、工具和现有发布闸门：

- 成功：`published`，通知带真实 note id/URL。
- 模拟导出：`simulated`，不是真实发布，不写 `published`。
- 确定未发送的连接失败：`retry_wait`，持久 job 在退避后继续。
- 登录失效：`waiting_login`，通知 owner 重新登录。
- 5xx/发送后超时/无回执：`waiting_owner`/unknown，不自动重发；只有当前账号下发送前基线之外唯一新增的同标题笔记，或 exact 回执能改 `published`。旧同标题、账号切换、基线缺失/读取失败、多候选都不算成功。
- 平台明确拒绝：`waiting_owner`，保留错误/证据。
- 预算耗尽：`waiting_owner`，保留批次/日累计计数；owner 明确继续后创建受限新批次。

## 飞书命令

均为 owner 私聊，必须带审批码：

```text
发布状态 XH-XXXXXX
检查原因 XH-XXXXXX
修复并重试 XH-XXXXXX
已登录 XH-XXXXXX
确认未发布并重试 XH-XXXXXX
暂停发布 XH-XXXXXX
继续发布 XH-XXXXXX
取消发布 XH-XXXXXX
```

`拒绝发布 XH-XXXXXX` 与取消同义。

说明：

- `发布状态`/`检查原因` 只读，不发送；`检查原因` 唤醒只读诊断，不会变成发布入口；诊断派生的 `schedule_resume` 也保持只读，不能发布原审批。
- `修复并重试` 在 `unknown` 状态会被拒绝，必须先确认未发布。
- `确认未发布并重试` 在请求在途时会被拒绝，不能把在途结果当作未发布。
- `已登录` 只唤醒任务，worker 仍会核对账号。
- `取消发布` 在途/unknown 时只记录取消请求，先继续只读对账；晚到成功会标 `published` 并说明取消未能撤回，不会自动删帖。
- 群聊/非 owner 不处理；所有命令经 inbox 去重。

## 运维

- 查看任务：`publication_tasks`、`publication_agent_steps`、`publish_attempts`。
- 查看状态：`发布状态 XH-...`。
- 查看原因：`发布状态` 的 `task.wait_reason`、`last_error_class`、attempt 的 `error`/`dispatch_state`/`failure_class`/`retryable`。
- 资产恢复：程序维护 `var/backups/publication_assets` 清单；工具只接受 `asset_ids`，不信任模型给出的备份路径。
- 不要手工改 `approval`/`draft` 为 published；只有回执或 feed 证据才能确认。
- 不要重发处于 `unknown` 的审批；先只读核对平台，再由 owner 确认。

## 关闭 / 回滚

将 `publication_agent.enabled=false` 并重启 worker 后，新的批准恢复走旧 publish job；已存在的 task 不自动执行。不要删除 `publication_*` 历史表。数据库回滚使用部署前备份，且必须安排维护窗口并先停 worker/gateway/console（三者都可能写库）；不要在上线后直接恢复旧 DB 丢掉新审批/发布事实。回滚后保留历史表并核对 unknown attempt。

## 离线故障注入演示

```bash
PYTHONPATH=. .venv/Scripts/python.exe scripts/demo_publication_recovery.py --jsonl
```

脚本只使用临时数据库和假发布器，不读取 live DB、不调用真实桥接。输出包括：审批创建、第一次 unknown、所有者确认未发布、第二次发布成功、POST 次数、通知数、步骤数和耗时。不会写入真实运营记录。
