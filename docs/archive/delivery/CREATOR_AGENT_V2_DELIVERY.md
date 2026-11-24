# Creator Agent V2 — 统一交付报告

日期：2026-11-13  
交付位置：主项目 `D:\Dev\Projects\bot`（V2 已部署）。原隔离副本已清理；下文中副本测试记录仅作历史证据。

## 1. 结论

V2 已从“固定搜索→写稿→审稿流水线”改造成模型驱动的有界 Agent：

- 模型逐步选择工具，程序校验参数、权限、预算、幂等，并持久化每一步。
- 12 个工具、结构化契约、artifact 版本、material/claim/review/revision 数据模型已落地。
- 运行 journal、checkpoint、无效决策/无进展上限、入站去重、有界 scheduler 已实现。
- `LiveRuntime` 已接真实搜索、正文抓取、写稿、Pillow 渲染、确定性布局检查、审稿和真实 pending approval 桥。
- live worker 已注册 `creator_agent_run`；Feishu owner 私聊可用 `V2 选题 ...` 触发。

### 已完成的真实 V2 发布验证

- run：`ca-ce0670cbe414c663b98b`
- 8 个决策步，1 次搜索，8 次模型调用
- 期间写稿失败 3 次（输出截断、对比表列数错误、文案段落超限），Agent 读取错误后自主修正
- `review_artifact=pass` → `submit_for_approval`
- 生成草稿 #17、审批 **`XH-2FMN8K`**
- owner 于 `2026-11-13T05:12:07Z` 在飞书批准
- 发布尝试 #12：HTTP 200；feed 自动确认唯一新增同标题笔记
- 笔记：`6af69d16000000002903c3c1`
- URL：`https://www.xiaohongshu.com/explore/6af69d16000000002903c3c1`（不含查询参数）
- 结果：approval/draft/attempt 均为 `published`，`publish_unknown=0`，1 张图片
- 该笔记是 V2 自己生成并获批的，不是旧链路测试稿。

## 2. 实际测试

主项目 V2 targeted：

```powershell
$env:PYTHONPATH="."
D:/Dev/Projects/bot/.venv/Scripts/python.exe -m unittest tests.test_creator_agent_v2 tests.test_creator_agent_live tests.test_migration_v2 -q -b
```

结果：**Ran 37 tests — OK**（N1–N4 验收前收尾；提交键/断点恢复、最终审批事务取消保护、保留图片硬校验、重启预算与 deferred 恢复测试已加入）。  
  
加入旧审批/发布回归后：`test_creator_agent_v2 + test_creator_agent_live + test_migration_v2 + test_approval_publish` → **56/56 OK**（114.205s 初跑；最终复跑见下方 N 组）。

副本全量（历史记录；隔离副本已清理，不再作为运行入口）：

结果：**Ran 340 tests in 642.312s — OK**（归档记录，不再复跑）。

主项目全量（当前最终测试树）：`python -m unittest discover -s tests -q -b` → **Ran 367 tests in 812.120s — OK**，日志 `var/tests/full_suite_r1r2_final.log`。此前 359 项为 N1–N4 基线；570 秒运行是被外部 `timeout 570` 截断，不是测试挂起。

覆盖：

- 动态路径 `search → draft_artifact → render_images → review_artifact → submit_for_approval`；
- 伪造 material ID 被拒；
- 无效决策/无进展/搜索预算上限；
- revision 保留 image hash；
- 聚合摘要不能支撑细节、无引用数字、核心矛盾、缺少 quote；
- migration 9/10 合成旧库升级、`creator_agent_materials` 表与完整性检查；
- crash resume；
- Feishu inbound 去重 → job → runner → artifact / real approval；
- scheduler 有界去重；
- 偏好按 account/scope 隔离；
- 同一 artifact 版本幂等；
- `ask_owner` 终止路径。

## 3. A 组修复（2026-11-13 独立评估后）

- **A1 已修（含 parent 恢复）**：`V2 修改 ...` 现在按账号定位最近一个可修订 V2 作品，写入 `parent_run_key`、`base_artifact_version`、原 `topic`、`generation=旧代+1` 和 `must_keep`；旧 pending approval 在同一事务内变为 `revision_pending`，取消旧 publish job，再入队新 run。worker 会加载父 artifact 的正文，并从父 event 的 evidence 重建 `P<id>` 材料；`LiveRuntime.revise_artifact` 对显式保留资产复用原 image 记录，不再无条件重画。
- **A2 已修（核心 + 断点恢复）**：`save_artifact` 对同一 `(run_key, version)` 的正文/claims 做不可变比较，冲突写入抛错；`submit_for_approval` 先按 `artifact.draft_id` 查已有审批，重复调用返回同一 code，不重复建 draft/approval；新增 `_resume_existing_draft`，draft 已建但 approval 未建的崩溃窗口会 finalize 同一 draft。runner 在工具执行前先写 `running` step，执行后更新同一 step。
- **A3 已修（确定性 + 可选语义审稿）**：修正了数字正则的转义错误；校验 claim quote 确实出现在 material quote 中，并逐一比较 claim 数字与 quote 数字（`10 percent` 对 `999 percent` 会拒绝）。`submit_for_approval` 每次重新执行确定性 `submission_issues`，不依赖模型先前的 review 结论。新增 `creator_agent.semantic_review` 开关：在确定性检查通过后追加一次独立 `draft_review` 模型审稿，失败时阻止提交；默认关闭，live 尚未重启用该开关，因此当前 live 仍以确定性审稿为准。
- **A4 已修（manual/revision）**：手动/修订 V2 提交会创建真实 `topic_requests` lineage 行，draft 带 `topic_request_id`，发布侧可继续使用旧的手动/自动配额与 pause 判定；scheduled V2 不伪造 manual lineage。
- **A5 部分完成**：每次 run 共享一个 `PhysicalFetchQuota`；搜索/读取的物理抓取数回传 runner；writer 模型调用计入 runner 的 `model_calls`；提交前做 generation CAS。材料持久化、deferred 恢复、checkpoint 与副作用完全原子化仍未完成。

## 4. T1–T4 收尾（2026-11-13 第二轮独立复查后）

- **T1 观察面**：`runner._observation` 现在显式给出 `current_artifact`、`parent_artifact`、`materials`、`revision`；父 artifact 的正文/图片/claims 与父 event evidence 重建的 `P<id>` 材料都进入观察，模型不再需要猜 material ID。worker 会 `store.artifact(parent_run_key, base_artifact_version)` 后调用 `runtime.load_parent_artifact()`。
- **T2 最终图片清单**：`revise_artifact` 只携带被明确保留的图片；`render_images` 渲染后用父文件覆盖保留序号；`submit_for_approval` 必须带最终 `artifact.images`，并由 `_persist_artifact_images()` 复制到 draft 目录、校验 sha256/Pillow 尺寸，再更新 `draft_images` 和 `images_sha256`。最终审批通知使用同一份 manifest。
- **T3 代际与恢复**：generation CAS 在 `_existing_submission`/`_resume_existing_draft` 之前执行；旧代、已失效审批、重复 draft 都会被拒绝。`_resume_existing_draft` 只 finalize 已保存的同一 draft，并回填 `topic_requests.draft_id/approval_id/result_code/progress/error`。已测 stale generation 与 draft-only 崩溃窗口；render/notification 崩溃窗口仍是剩余测试项。
- **T4 材料与预算**：migration 10 增加 `creator_agent_materials`；`search`/`read_source` 持久化材料与正文，重启后 `LiveRuntime` 从 store 恢复；`runner.model_calls` 改为读取 runtime 的真实模型调用增量（含 writer/语义审稿）；`resume_deferred` 受配置控制，默认 false。
- 配置：`creator_agent.semantic_review`、`creator_agent.resume_deferred` 默认均 false；未改 live 开关。
- R1/R2 已补真实 `handle_inbound_message → creator_agent_run → AgentRunner → 修订 → pending approval` 穿透测试，并把旧审批/通知双唯一性、恢复 package 变化拒绝、每次 transport attempt 预扣纳入回归。
- 仍未完成：render/notification 的额外崩溃注入、语义审稿结果复用、checkpoint 与副作用完全原子化。

## 5. N1–N4 验收前收尾（2026-11-13）

- **N1 提交断点与幂等**：migration 10 给 `drafts` 增加 `submission_key` / `submission_hash` 和唯一部分索引。`submit_for_approval` 先按 `submission_key` 找已存在 draft，再决定是否插入；`insert draft -> artifact.draft_id -> store.save_artifact` 崩溃时，重试可从提交键恢复同一 draft，不触发 `drafts.topic_request_id` 唯一约束。已测 draft 指针保存失败、审批已创建后崩溃、finalize 后崩溃，均保持 1 draft / 1 approval。
- **N2 最终事务取消保护**：`_finalize_with_approval(..., guard=...)` 在审批事务内、创建 approval 之前检查 run 存在性、`status='running'`、account、generation 和 `draft.submission_hash`；取消/删除/换代后重试会被拒绝，合法重试仍复用已有审批。
- **N3 保留图片硬约束**：`render_images` 对被保留的资产先检查文件存在与 sha256；渲染后若对应序号缺失直接报错，不再静默重画。最终 manifest 使用 `ordinal-asset_id-source` 唯一文件名，`_persist_artifact_images` 始终用 Pillow `verify()` 解码并读取真实尺寸，拒绝重复序号、缺失、损坏和 hash 不符。
- **N4 重启预算**：`LiveRuntime` 从 run counters 恢复 `_runtime_model_calls` 与物理抓取次数；模型调用在 provider 调用前预扣，包含 writer/critic/transport 重试；`resume_deferred` 仅在显式提高 `max_model_calls` 或 `resume_budget_deferred=true` 时允许预算类 deferred 继续，超时类仍可恢复。

N 组定向：`test_creator_agent_v2 + test_creator_agent_live + test_migration_v2` → **37/37 OK**；加 `test_approval_publish` → **56/56 OK**。主项目全量结果见下方测试记录。

## 5.1 R1–R2 独立复核返修（2026-11-14）

- **R1 提交包绑定**：恢复提交前重新计算当前 `submission_key` 与不可变包 hash（brief、run、version、正文、图片序号/sha/`preserved`），不再用旧 draft 的 hash 自证。`_existing_submission()` 只接受 key/hash 完全一致的旧提交；`_resume_existing_draft()` 在复制文件前拒绝 package 变化。最终审批事务的 `guard` 再次校验 run 状态/代际/账号、draft 的 `submission_hash` 与最终 `images_sha256`，并拒绝同一 draft 已有 pending/approved 审批。图片清单、正文或顺序变化都会返回失败，不会复用旧审批。
- **R2 单任务模型调用**：`LiveRuntime._reserve_model_call()` 在实际 provider 请求前检查 `model_limit`、递增并持久化 run counters；`DeepSeekLLM.complete_json()` 对每次 transport retry 调用 `before_attempt`，在 HTTP 发出前拒绝超限；decision/writer/语义审稿共用同一预算。`FakeLLM` 也调用回调，避免测试绕过。
- **真实入口链路**：新增 `test_inbound_worker_revision_uses_parent_and_preserves_image`，走 `handle_inbound_message → creator_agent_run job → AgentRunner → revise_artifact → render_images → review → submit`；断言观察含父 artifact/`P*` 材料，旧审批变 `revision_pending`，新审批为 `pending`，最终草稿第二张图片 hash 与父稿一致、第一张变化。
- 新增 R1/R2 测试还包括：changed package/body/order、幂等审批与通知唯一、最后一次模型调用后立即阻断下一次、transport retry 逐次预扣。
- 定向：`test_creator_agent_v2 + test_creator_agent_live + test_migration_v2` → **48/48 OK**；加 `test_approval_publish` → **67/67 OK**。
- 主项目全量：**Ran 367 tests in 812.120s — OK**（`var/tests/full_suite_r1r2_final.log`）。
- migration 10 先在 live DB 的临时副本上演练：版本 9→10、`drafts.submission_key/hash`、唯一部分索引、重复键拒绝、二次初始化幂等、`integrity_check=ok`。R1/R2 测试通过后，已按 owner “通过后进入部署”授权应用到 live：live version=10、`integrity_check=ok`；迁移后备份 `var/live/backups/post-r1r2-migration-20261114-0237.sqlite3`。worker/gateway 已重启为当前代码。

## 5.2 手机验收首轮故障修复（2026-11-16）

- 故障 run `ca-303e64d725142be1d2b1` 的输入是小写 `v2 选题 ...`；旧前缀剥离大小写敏感，导致 `v2` 进入 brief topic。已改为 `strip_command_prefix()` 大小写不敏感、最长前缀优先，入口与 `accept_revision` 共用。
- 修订只保留显式 `must_keep` 图片，文本修订后 `images=[]`；review 拒绝“没有图片”，再补渲染时 8 步上限已用完。`revise_artifact` 现在默认携带父稿全部图片（仅显式 `must_keep` 标 `preserved=True`），因此可先提交旧图，也可在后续 `render_images` 中只替换非保留图。
- 失败/暂停/需补充的 run 原先没有 owner 可见消息，看起来像“发了没反应”。`entry.run_job` 现在为 `failed`/`deferred`/`needs_owner` 写一条幂等 `creator-outcome:<run>:<status>` 通知（含 run key、步数、错误），由 worker 投递；成功后仍走正常审批卡。
- 新增测试：小写 `v2` 前缀剥离、无 `must_keep` 修订保留图片、失败 run 恰好一条 owner outcome。定向 `test_creator_agent_live + test_creator_agent_v2 + test_migration_v2` → **48/48 OK**；加 `test_approval_publish` → **67/67 OK**（132.861s）。
- 全量 **367/812.120s** 是本次修复前记录；修复后尚未重跑主项目全量，报告不把它当作本次修复后的结果。
- live worker/gateway 已重启加载修复；重新发送 `V2 选题 ...` 即可验收。旧失败 run 不会自动重试，也不会自动发布。

## 5.3 正文超限改为可审批软限制（2026-11-16）

- owner 反馈“字数超了别前面卡死，我可以拒绝或让修改”；V2 不再因渲染正文超过 1000 字而中止整个 run。
- `validate_draft_payload(..., soft_body_limit=True)` 仅由 `LiveRuntime._write` 使用：先保留完整来源块，按段落/句子边界压缩 caption，追加“注：正文超过平台上限，已自动压缩；请审批时核对关键信息。”并把同一提示写入 `uncertainties`；正文仍不超过平台上限。
- 旧链路 `soft_body_limit=False` 保持原有硬失败；标题 20 字仍由 `_fit_title` 自动收口；来源、证据、数字校验、审批和发布闸门没有放宽。
- 若来源块本身已占满、无法安全压缩，仍会失败而不是切掉来源。
- 新增 `tests/test_content_soft_limit.py` 2 项；`test_content_soft_limit + test_creator_agent_live` = **30/30 OK**（53.520s）。
- 2026-11-16 14:53 已重启 live worker（PID 25180）加载本修复；未自动发布。

## 5.4 小红书正文去除来源 URL 与报告腔（2026-11-16）

- owner 反馈“图文不需要标来源网址，会限制推流，而且 AI 味道很重”；V2 的公开正文不再拼接 `【来源】` 和 URL。来源仍保存在 `draft.body_json.sources` 与私有审批卡中，用于核对和审批绑定，不进入发布正文。
- `DraftContent.render_body(include_sources=False)` 仅供 V2 公开正文使用；旧链路默认 `include_sources=True`，历史草稿与旧审批不变。
- V2 不再把 `DEFAULT_LIMITS`（“以上为公开资料整理…”)写进公开 caption；该提示保留在 `uncertainties`/审批元数据。模型偶发的同类报告腔句子也会在 `LiveRuntime._write` 中移出公开 caption。
- V2 writer 增加小红书风格指令：第一人称/口语化判断与感受，禁止“本文/综上/首先其次”、参考文献、来源列表和 URL；热点用自然转述，不编造亲测。
- 来源、数字、证据、审批和发布闸门没有放宽；只是公开表达形式变化。
- 测试：`test_content_soft_limit + test_creator_agent_live + test_creator_agent_v2 + test_migration_v2` = **52/52 OK**（78.940s），新增公开正文无 URL / 无报告腔断言。live worker 已于 15:21 重启为 PID **8524**。

## 5.5 V2 每日自动发现（2026-11-16）

- owner 确认每日自动选题不能丢失；旧 `collect` 仍在空转，但 `discover_topics()` 返回空，V2 没有自动发现入口。
- 新增 `bot/services/creator_agent/discovery.py`：读取 AIHot `/feed.xml`，按时效过滤、按标题去重，优先使用原文链接。
- worker 新增 `creator_discovery` 每日任务与 `creator_agent.daily_discovery_*` 配置；每个候选写入 `CreativeBrief.source_url`，worker 预取一次原文作为 agent 初始材料，之后仍由模型选择工具。
- 每日任务只创建 V2 run 和 pending approval，不自动批准、不自动发布；旧 `collect`/`draft` 自动排期已停用，历史数据不删除。
- 测试：`tests/test_creator_daily_discovery.py` 4/4 OK（含时效过滤/去重、brief 原文链接、每日只入队一次、候选有界）。
- 迁移 10 已在 live DB；本功能不需要新迁移。

## 6. 离线 demo

```powershell
cd D:/Dev/Projects/bot
$env:PYTHONPATH="."
D:/Dev/Projects/bot/.venv/Scripts/python.exe scripts/creator_agent_v2_demo.py
```

输出 `status=completed`、`artifact_version=1`、5 个动作；demo 不触碰审批或发布。

## 7. 飞书使用

只接受 owner 私聊、带 `V2` 前缀的消息：

1. `V2 选题 Claude Code 如何选择模型`
2. `V2 修改 少用“最强”，保留：Fable 5.2 / Opus 5.5 / 价格对比`

完成后发送普通审批卡片，仍需 `批准 XH-...`；不会自动批准或发布。

## 8. 迁移、部署与回滚

- migration 9 已在 live `var/live/bot.sqlite3` 执行；migration 10（`creator_agent_materials`、`drafts.submission_key/hash` 及唯一索引）已于 2026-11-14 02:37 +08 应用到 live，version=10、`integrity_check=ok`，备份 `var/live/backups/post-r1r2-migration-20261114-0237.sqlite3`。合成旧库测试覆盖 v8→v9→v10。
- 部署前备份：`var/live/backups/pre-v2-20261113-125*.sqlite3`；主代码备份：`var/backups/v2-pre/bot-125841`。
- live `config/bot.live.toml` 已设置 `[creator_agent] enabled=true`；worker 已重启（PID 2200，随后由当前进程接管）。
- 回滚：恢复主代码备份与 live DB 备份；V2 表是追加表，旧流程不依赖；不要删除历史 migration。
- 不做自动迁移回滚；恢复备份前先停 worker，避免写入。

## 9. 清理

见 [CREATOR_AGENT_V2_CLEANUP.md](CREATOR_AGENT_V2_CLEANUP.md)：本轮保留旧模块/表/历史迁移，因为旧发布链路仍是 live 的最终闸门；未删除生产数据。

## 10. 已知限制

1. `discover_topics` 仍返回空；热点发现继续走旧调度。
2. 发布复用旧审批/发布闸门和 feed 回执确认，V2 没有另造发布器。
3. 没有真实视觉模型，布局检查为确定性规则，`visual_review=not_available`。
4. 主项目全量本轮已实际跑完：`Ran 367 tests in 812.120s, OK`；日志 `var/tests/full_suite_r1r2_final.log`。
5. A3 的语义审稿是可选开关，默认关闭；live 未重启用该开关，因此 live 的 A3 仍是确定性校验。
6. T4 已补材料/正文持久化、deferred 恢复与真实模型调用的计数；N1–N4 已补提交键、最终事务取消保护、保留图片硬校验和重启预算。R1/R2 又补恢复时 package 变化拒绝、真实 inbound→worker 的父材料观察、审批/通知唯一性，以及每次 transport attempt 前预扣。语义审稿结果复用、checkpoint 与副作用完全原子化仍未完成。migration 10 已应用到 live（version=10，integrity ok），迁移后备份见上。
7. 旧模块作为回退路径保留；V2 目前是“有界、人审”的内容 Agent，不是全自动发布器。

## 11. 文件

- `bot/services/creator_agent/{contracts,store,context,registry,runner,tools,policy,entry,live}.py`
- `bot/adapters/{llm,sources}.py`（agent prompts / open-domain search）
- `bot/worker.py`、`bot/services/approvals.py`、`bot/schema.py`、`bot/config.py`
- `config/bot.example.toml`、`config/bot.live.toml`
- `tests/test_creator_agent_v2.py`、`tests/test_creator_agent_live.py`、`tests/test_migration_v2.py`
- `scripts/creator_agent_v2_demo.py`
- `docs/CREATOR_AGENT_V2_ARCHITECTURE.md`、`docs/CREATOR_AGENT_V2_USAGE.md`
- `docs/delivery/CREATOR_AGENT_V2_PROGRESS.md`、`docs/delivery/CREATOR_AGENT_V2_CLEANUP.md`
