# P0 发布结果闭环交付报告

- 日期：2026-11-10
- 范围：仅 P0「发布结果闭环」
- 状态：**离线代码实现、独立验收返修与离线测试完成；未部署、未重启生产服务、未发送飞书消息、未真实发帖。**
- 生产数据访问：无。未读取 `config/secrets.toml`、Cookie、`var/live` 数据库或生产日志。
- 返修依据：[P0 独立验收与返修清单](P0-independent-review-20261110.md)；三项问题均已修复并补回归测试。

## 1. 本次目标

解决“发布后用户仍要追问到底发没发”的问题，具体包括：

1. 发布业务结果必须持久化、可查询，不能只藏在 job 是否执行成功里。
2. 成功、确定失败、发布前被阻止、结果未知都要形成清晰、幂等、可送达本人的飞书回执；回执包含草稿/审批码、状态、原因和下一步。
3. 结果未知时只允许只读对账，不允许自动重发；没有可靠证据时保持 `unknown`。
4. 只读对账必须先信平台回执；回执缺少 ID/URL 时，只能在时间、标题、正文等特征都足够强且唯一匹配时认领，标题相同或查不到都不能证明成功或失败。
5. 不改变既有配额和逐篇审批规则，不通过删除记录、清空计数或修改审批绕过限制。

## 2. 实现内容

### 2.1 持久化业务结果

新增 append-only 迁移 5（`bot/schema.py`）：

- `publish_results`：每次审批/尝试对应一条可查询的业务结果；发布前被阻止、过期、成功、模拟、失败、未知都会落库。字段包含审批、草稿、尝试、阶段、状态、原因码、中文说明、平台 post ID/URL、回执、通知 ID。
- `publish_reconcile_checks`：只读对账检查记录，含时间、结果、原因码、说明和脱敏证据 JSON；用于限制频率、保留证据。
- 不删除旧 attempt/approval/draft/event 记录；迁移只新增表/索引。

结果状态包括：`published`、`simulated`、`failed`、`unknown`、`blocked`、`expired`。其中 `simulated` 明确表示本地模拟、未发布，不能展示成真实成功。

### 2.2 幂等回执

`bot/services/publication_results.py`：

- `record_result(...)` 在发布状态变化或首次落库时创建/更新结果，并在同一事务内用 `notifications.queue_outcome` 排队一条持久化通知。
- 通知去重键由审批、尝试和状态构成；重复点击、重复回调、worker 重入不会产生第二条相同回执。
- 回执是中文文本，包含审批码、草稿标题/编号、状态、阶段、尝试编号、原因、回执/下一步。`simulated` 文案明确“未发布”。
- 通知仍走既有 `notifications` 表；worker 在发布处理完成后立即尝试投递，失败则保留为 `retry`，由后续 tick 继续投递。

### 2.3 发布门禁与恢复

`bot/services/publish.py`：

- `publish_approval` 的所有早退路径（审批未批准、过期、发布器未配置、账号/来源/产物校验失败、claim 竞争失败）都会记录 `publish_results`；发布前被阻止不再只体现在一次函数返回值里。worker 发现发布器未配置时也走统一门禁结果，写入 durable `blocked` 结果和回执，不创建 attempt、不调用发布器。
- `_finish` 在同一个事务中写入 attempt、approval、draft、event 和业务结果；attempt 更新带 `state='publishing'` CAS。若另一个写入方已先确认终态，旧完成回调不会覆盖，返回已有终态并标记 `transition_blocked`。
- 业务结果增加终态保护：`published`/`simulated`/`failed` 不允许被更晚到达的旧 unknown 覆盖；结果 `stage` 按最后一次写入更新。
- 只有发布器返回真实 post ID 或 URL 才允许写 `published`；HTTP 2xx 但没有回执、传输异常等写 `unknown`。
- 启动恢复：`publishing` 状态的 attempt 只转为 `unknown`，记录“重启时正在发布，绝不自动重发”，不会重新调用发布器。没有 attempt 但审批卡在 `publishing` 的情况也会记录未知结果。
- 重复审批/重复回调返回已有业务结果，不新增 attempt。

### 2.4 只读对账

`bot/services/reconcile_readonly.py`：

- 通过 `EvidenceProvider.lookup(attempt, approval, draft)` 获取候选笔记；不直接抓取平台页面。
- 严格匹配（返修后）：provider 必须给出**与审批 `account_id` 完全一致的本人账号证明**；规范化标题必须相同；候选正文必须非空且与审批正文相似度达到阈值；发布时间只用于窗口校验，不能替代正文或账号证明；候选必须有真实 post ID/URL。缺账号证明、正文为空、账号不符、正文不符都保持 unknown。
- 0 个候选：`no_unique_match`，保持 unknown，说明“不能证明未发布”。
- 多个候选：`ambiguous_multiple_matches`，保持 unknown，交人工。
- provider 不可用或异常：`lookup_unavailable` / `lookup_failed`，保持 unknown，绝不把查不到当成未发布；异常文本在写入检查记录、结果和回执前会脱敏/截断。
- 唯一强匹配：才调用既有 `reconcile_unknown(confirmed_published)` 认领，并写一条对账证据；不调用发布器。
- 频率/时间限制：最多 5 次、72 小时内、间隔 10 分钟；检查记录先在事务内预占再查询，避免并发绕过；限制不通过时不产生新检查记录。
- 并发保护：只读对账只接受 `unknown`，拒绝正在 `publishing` 的 attempt；provider 返回后重新读取 attempt，若人工对账或原发布已完成，则保留当前终态，不回写旧 unknown。
- 默认 provider 为 `UnavailableEvidenceProvider`。因此 CLI `reconcile-readonly` 在当前无可靠平台查询接口时会明确返回 unknown，而不会猜测。

小红书适配器只新增了 `lookup_published_notes` 扩展点，明确抛出“没有可靠文档化只读接口”的 `NotConfigured`。没有编造接口字段、没有抓取页面。

### 2.5 可查询入口

`bot/cli.py`：

- `bot status` 新增 `发布结果: {state: count}`。
- `bot results --limit N`：只读列出结果（审批码、草稿、状态、阶段、原因、时间、post ID/URL、中文说明）。
- `bot reconcile-readonly <审批码>`：只读对账入口；当前无平台 provider 时返回 unknown，不发布、不重试。
- 原有 `bot reconcile ...` 仍是人工凭真实 post ID/URL 认领的路径。

## 3. 变更文件

| 文件 | 变更 |
| --- | --- |
| `bot/schema.py` | 迁移 5：`publish_results`、`publish_reconcile_checks` |
| `bot/config.py` | 新增 `reconcile.*` 上限：5 次 / 72h / 10 分钟 / 120 分钟匹配窗口 / 0.65 正文相似度 |
| `bot/services/publication_results.py` | 新增：结果持久化、终态保护、幂等通知、状态中文标签、查询 |
| `bot/services/publish.py` | 接入结果落库；门禁中文原因；attempt CAS；恢复/人工对账写结果；worker 未配置发布器统一 blocked 结果 |
| `bot/services/reconcile_readonly.py` | 新增：只读证据 provider、账号+正文强匹配、并发重读、事务预占限流、异常脱敏 |
| `bot/worker.py` | 启动恢复带 config；发布后立即投递；发布器未配置走统一 blocked 结果与回执 |
| `bot/adapters/publishers.py` | 小红书只读查询扩展点，明确未验证/不可用 |
| `bot/cli.py` | `status` 结果计数、`results`、`reconcile-readonly` |
| `tests/test_publication_results.py` | 20 个离线验收/返修回归测试 |
| `docs/README.md`、本报告 | 文档索引与交付报告 |

## 4. 验证结果

运行解释器：`.venv/Scripts/python.exe`（Pillow 环境）。未调用真实平台、未发飞书、未访问生产数据。

| 验证 | 命令 | 结果 |
| --- | --- | --- |
| P0 结果闭环测试（含返修回归） | `python -m unittest tests.test_publication_results -v` | **20 tests，OK，46.015s** |
| 发布相关回归 | `python -m unittest tests.test_approval_publish tests.test_worker_e2e tests.test_concurrency tests.test_publish_quota_recovery` | 26 tests，OK，56.630s |
| 全套离线测试（返修后只跑一次） | `python -m unittest discover -s tests` | **194 tests，OK，250.179s**，exit 0 |
| 全套日志 | `var/final-test-run-20261110-p0-repair.log` | 完整输出已保留 |
| CLI 空库查询 | `bot results --limit 5` | `暂无发布结果记录` |
| CLI 状态 | `bot status` | 正常显示发布结果计数；offline/demo |

返修回归重点：

- **P1-1 弱匹配**：同标题+接近时间但账号未知、正文为空，保持 unknown；错误账号、正文不符、缺时间但正文强且账号一致等场景分别验证。
- **P1-2 并发旧写覆盖**：provider 查询期间人工确认 published 后，只读对账不回写 unknown；结果表保留 published 和真实回执；终态不可被旧 unknown 降级。
- **P2-3 worker 无发布器**：通过 Worker 真实入口产生 1 条 `blocked` 结果和 1 条回执，0 条 attempt；重复处理不重复通知。
- **脱敏**：provider 异常中的伪 token/cookie/bearer 文本在检查记录、结果和回执中替换为 `[REDACTED]`。

覆盖的验收场景：

- 审批未批准、发布前被阻止：有 durable `blocked` 结果与通知，发布器调用 0 次。
- 真实回执成功（测试中用 mock/模拟回执）：只认带 post ID/URL 的结果，状态为 `published`（真实模式）或 `simulated`（mock 模式），二者不混淆。
- 无 ID/2xx/断连/超时：`unknown`；重复调用只返回已有结果，不新增 attempt。
- 重复审批、重复回执、通知重放：同一结果只产生一条通知；重复消息不重复发送。
- 重启：`publishing` 恢复为 `unknown`，记录“绝不自动重发”，attempt 数不增加。
- 只读对账：唯一强匹配可认领；同标题多候选、0 候选、provider 异常、间隔限制全部保持 unknown。
- 未知和普通失败继续占用配额；只有人工/唯一强证据确认“未发布”才释放（沿用既有 `reconcile_unknown` 语义）。
- 审批正文/图片/时间/账号变更仍会使旧审批失效（既有测试继续通过）。

## 5. 部署步骤（本次**未执行**）

本次没有部署、没有重启服务、没有改生产库。后续授权部署时建议：

1. 先备份 `var/live/bot.sqlite3`（使用现有 `scripts/backup.ps1`），确认完整性。
2. 部署前在**合成的旧版数据库**上演练迁移 4→5，确认原有 approvals/attempts/drafts/notifications 数据保留、可重新初始化；本次未做真实生产库迁移验证。
3. 部署本次代码；迁移 5 会在 worker/console 初始化数据库时自动应用，无需手工改库。
4. 在无待发布任务时重启 worker/console（需要真实发布时按既有四开关规则单独授权）。本次不授权，故未做。
5. 运行 `python -m bot --config config/bot.live.toml doctor`、`bot status`、`bot results` 验证迁移、结果查询和配置。
6. 观察下一次真实发布：若平台返回 post ID/URL，应产生 `published` 结果与回执；若桥接只返回 2xx 而无 ID/URL，应保持 `unknown` 并提示人工提供证据，不得重发。

## 6. 限制与未验证项

- **本项目尚未实现并验证可靠的小红书只读查询 provider**；已核对的桥接文档只提供登录状态和发布接口，发布成功响应不保证包含 note_id/URL，也没有文档化的“列出本人笔记/按标题查询”接口。因此自动只读对账适配器保持“未验证/不可用”，`reconcile-readonly` 当前会返回 unknown。不能把该命令说成已具备真实平台自动对账能力。
- 真实发布传输、飞书实际投递未在本次验证：仅用 mock publisher、FakeNotifier/本地通知文件验证协议和幂等。未发送飞书、未发真实帖。
- 未读取生产数据库或日志，因此没有在生产数据上验证迁移/结果回填；只验证了全新临时 DB 和离线测试库。生产迁移前仍需按部署步骤在合成旧库上演练。
- 结果通知依赖 worker 正常运行；如 worker 停止，通知留在 `notifications` 队列，重启后由 tick 投递，不丢结果。
- 只读对账的证据质量取决于未来 provider 返回的标题、正文、时间和账号；当前没有生产 provider，不能用“查不到”推断未发布。
- 生产上已经存在的历史 unknown/失败记录不会被本报告修改；本次没有做数据修复或重置。

## 7. 下一步真实验收所需

需要一次**明确授权**的真实发布验收，并至少满足其一：

- 平台/桥接返回真实 post ID 或 URL；或
- 用户提供真实 post ID/URL，人工执行 `bot reconcile <code> confirmed_published ...`；或
- 未来出现可靠的平台只读查询接口后，实现并验证 `lookup_published_notes`，再启用 `reconcile-readonly`。

在此之前，系统保持“无证据即 unknown、绝不自动重发、逐篇审批、配额不变”的安全默认。
