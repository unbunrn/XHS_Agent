# 发布任务静默故障修复（XH-HMP6VB）

状态：代码修复与离线验证完成，全量测试通过；gateway/worker 已更新，等待主人手机验收。
日期：2026-11-19
范围：仅修复发布任务的可见回执、停止解释和模型观察；不重发、不修改 XH-HMP6VB。

## 根因

1. 飞书发布命令分支在 `bot/services/approvals.py` 只把结果写入 `inbound_messages`，从未调用
   `notifications.queue_outcome`。因此“发布状态 / 检查原因”在数据库里已有结果，手机收不到回复。
2. `bot/services/publication_agent/runner.py` 的步骤上限、连续无效决策等退出分支只更新
   `publication_tasks.wait_reason`，没有生成停止通知。XH-HMP6VB 在 8 个只读动作后用尽步骤，
   任务变成 `waiting_owner`，但飞书没有收到任何消息。
3. 模型观察只取最后 8 条步骤且混入 `model_attempt/model_decision`，剩余步数按整条任务历史
   计算；模型看不到本轮已完成的检查，容易反复查询并耗尽步骤。

## 修改

- `bot/services/approvals.py`
  - 发布命令结果与 `inbound_messages` 状态在同一事务内写入，并排入 `inbound:<message_id>`
    回执；崩溃后重投可补发一次，不重复执行命令。
  - `processing` 重复回调在 120 秒内不误报“处理中断”，超时才补兜底回执。
  - 保留工作区原有未提交的帮助命令。
- `bot/services/publication_agent/entry.py`
  - 新增确定性中文回执：XH 码、当前状态、是否真正提交、平台笔记 ID、等待原因、最后更新、
    下一步；已发布时不显示残留的等待原因，也不展示 PB 编号。
  - 修复、取消、暂停、继续、登录、确认未发布等命令都有明确接受/拒绝说明；unknown 不谎称未发布。
- `bot/services/publication_agent/runner.py`
  - 步骤上限、模型预算、连续无效决策、无进展统一走停止回执；文本说明“尚未提交”或
    “可能已提交，先核对”，并列出本轮已检查项。
  - `steps_remaining` 只按当前批次扣减；工具 schema 按 allowed_actions 过滤。
  - 观察增加 `checks`、`last_observation`、`has_send_record`；只读检查第三次重复时停止并解释。
  - 正常只读诊断结束发“检查完成”，不改变任务状态。
- `bot/services/publication_agent/live.py`
  - 通知去重按“任务 + 批次”计算，同一批同文只发一次，新批次仍可发新回执。

## 测试

- 新增 `tests/test_publication_silence_fix.py`，先复现：8 个只读动作后无停止回执、
  状态命令无回复；修复前 6 个用例中 4 失败 1 错误。
- 当前 18 个定向用例全部通过：停止原因、状态回执、unknown、重复消息、非本人/群聊、
  诊断完成、已发布任务的“检查原因”、步骤预算、无进展、停止后修复仅一次 POST。
- 旧定向 21 个用例在边界修复后重跑通过。
- 合并定向 39 个用例通过（180.234s）。
- 全量测试 374 个用例通过（1058.740s，`OK`）。

## 最小服务更新

- 无需数据库迁移。
- 更新前检查是否有在途发布；只能在没有正在提交的任务时重启。
- 需重启 `gateway`（命令回执）和 `worker`（runner/通知）；console 不需要因本次改动重启。
- 已执行（2026-11-19 15:46–15:47）：停止旧 gateway 30432 / worker 15364，启动新
  gateway 25488 / worker 23856；console 24976 保持不变。启动后 worker 为 live 模式，
  gateway 已连上飞书长连接，无待执行或运行中的发布 job。
- 重启后由主人在手机发送：

  `发布状态 XH-HMP6VB`

  预期回执包含“已发布”、note ID `6afe96b0000000002b03343c`，不包含“尚未提交”，
  不会产生第二次发送。不要发送“修复并重试”或“确认未发布并重试”。

## 未做

- 未重发 XH-HMP6VB，未修改其审批/任务/发送记录。
- 未提交 Git，未推送。
