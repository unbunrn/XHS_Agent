# 发布与故障恢复 Agent：架构

## 目标

把“已批准的作品”变成可恢复的发布任务，而不是一次 HTTP 调用：

```
approval(approved)
  -> publication_tasks(ready)
  -> publication_agent job
  -> model decision (publication_decide)
  -> tool: publish_approved_artifact
  -> 既有 publish gate (包校验/账号/配额/来源/批准)
  -> bridge
  -> 只读结果/feed 对账
  -> published / retry_wait / waiting_owner / waiting_login / cancelled
```

审批、来源安全、账号、配额和发布闸门没有被绕过。模型只能在代码注册的只读/受控工具中选择；文件恢复、发布和取消最终由程序校验。

## 状态

`ready`、`diagnosing`、`submitting`、`verifying`、`retry_wait`、`waiting_login`、`waiting_owner`、`waiting_approval`、`paused`、`published`、`simulated`、`cancelled`。

原则：

- `published` 必须有平台回执，或当前账号下“发送前基线之外唯一新增”的同标题笔记；旧同标题、账号切换、基线缺失/读取失败、多候选都不算成功。
- `retry_wait` 只用于**确定未发送**的连接/派发失败，且有次数上限。
- 发送后超时、HTTP 5xx 而 feed 不可读、bridge 未返回 note id 且回执不足：一律 `unknown`，进入 `waiting_owner`，不自动重发。每次 claim 后在 dispatch 前保存账号身份/feed note ID 基线（`pre_send_baseline_json`）；基线必须同时有 `read_ok`、`identity_ok` 和非空账号。标题认领只接受同一账号、基线之外唯一新增且标题一致的笔记；身份缺失/账号变化/旧基线无 identity 均保持 unknown。
- `cancel_requested` 在途只表示“请求取消”，最终仍以平台结果为准；若存在 `unknown`/`publishing` attempt，runner 先只读对账，晚到成功优先。
- `simulated` 是本地模拟导出，不是 `published`；界面和通知不得把模拟状态写成真实发布。
- `retry_wait` 只用于确定未发送的失败；5xx/发送后超时/无回执一律 `unknown`，不自动重发。

## 持久化

Migration 11：

- `publication_tasks`：批准包快照、hash、generation、状态、active attempt、下一次唤醒、租约、重试次数、最终证据引用。
- `publication_agent_steps`：每次模型动作、参数、观察、证据引用。
- `publication_owner_requests`：需要 owner 登录/确认时的持久问题。
- `publish_attempts` 新列：`publication_task_id`、`attempt_no`、`dispatch_state`、`retry_basis`、`request_correlation_id`、`quota_reservation_json`、`failure_class`、`retryable`。
- `publication_tasks.work_key`：作品语义身份；部分唯一索引只约束未取消任务。

`publish_attempts` 的 task/attempt 唯一索引保证同一任务同一尝试号只落一次；`work_key` 防止同一作品换审批码后创建并行任务。最终 claim 事务同时核验 task 状态、cancel_requested、generation、冻结包 hash 和 lease owner。

## 工具与边界

只注册本任务需要的工具：

`inspect_task`、`check_bridge_health`、`check_login`、`verify_assets`、`restore_approved_assets`、`inspect_failure`、`publish_approved_artifact`、`check_publish_result`、`request_owner_action`、`schedule_resume`、`notify_owner`。

- `check_login` 不返回 Cookie/token，只返回登录状态与账号匹配结果。
- `restore_approved_assets` 只按程序维护的不可变资产清单和 `asset_ids` 恢复；源/目标都必须位于批准根目录，拒绝穿越/链接逃逸/hash 不符；临时写入核验后原子替换，恢复后再次 verify。
- `publish_approved_artifact` 每次只创建一个闸门内 attempt；不会调用通用 HTTP 客户端自动重试 publish POST。
- `request_owner_action` 只持久化问题并通知，不在回调里跑模型或浏览器。
- `schedule_resume` 只写 `next_wake_at`，不 sleep worker。
- `notify_owner` 不使用模型任意文本；通知由持久 task 状态/等待原因生成并脱敏。

## 与旧发布路径的关系

- `publication_agent.enabled=false` 时，批准后仍走原 `publish` job，行为不变。
- 开启后，批准不直接入队旧 publish job，而是创建 `publication_tasks` 与 `publication_agent` job；旧 publish handler 保留给历史任务/兼容入口，但不会形成第二个自动发送入口。
- 旧 `publish_approval` 的包校验、账号、来源、配额和 unknown 保护不变；task 模式只增加“同一任务/尝试号”的幂等键和状态映射。

## 失败分类

- 登录失效：`waiting_login`，通知 owner 重新登录；登录后必须重新核对账号，不能凭“已登录”直接发。
- 资产问题：可恢复时只恢复同 hash 字节；无备份/hash 不符/路径穿越 -> `waiting_owner` 或 `waiting_approval`，不重画、不降图片数。
- 确定未发送的连接失败：`retry_wait` + 指数退避 + 持久 job；复用同一任务，不增加作品发布名额。
- 发送后失败：先只读对账；`unknown` 不自动重试；owner 明确确认未发布后才允许新 attempt。
- 平台明确拒绝：记录证据，停止自动重试，等待 owner 修改/重新批准。

## 预算与唤醒

- 预算分恢复批次和日累计；每次模型 transport 前先持久预留 `model_attempt`，成功后再记 `model_decision`。
- owner `继续发布`/`已登录`/`修复并重试` 创建受限新批次，并保留总消耗；唤醒以 inbound message/event key 去重，同一消息不会重复命中旧 job。
- `检查原因` 只唤醒只读诊断，不允许 `publish_approved_artifact`/资产恢复/生命周期写入；诊断中的 `schedule_resume` 派生 job 仍保持只读。

## 未启用 / 未验证

- `publication_agent.enabled` 默认关闭，live 未开启。
- Migration 11 只在 live DB 临时副本演练，live 仍为 10。
- 未接真实飞书/桥接登录扫码/真实发布；真实平台行为仍需部署后验收。
- 自然语言/无审批码的唯一目标解析未实现，当前只支持规范命令。
- 真实桥接的阶段日志/截图/toast/2 分钟 deadline 能力边界未交付，部署时需按实际桥接版本核对。
- 部署/回滚尚未执行；migration 11 只在临时副本演练。
