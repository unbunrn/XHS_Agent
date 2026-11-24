# 实现状态（IMPLEMENTATION_STATUS）

## 最新状态（2026-11-18）

- **发布恢复 Agent Round-2 反例与离线演示（离线）**：已复现并修复“预发送 identity 缺失却按同标题认领成功”和“暂停任务在 0 预算下被 `检查原因` 改成 waiting_owner”。新增显式 `lookup_published_notes_verified`（identity+notes 同源）、严格预发送基线、诊断只读/独立工具集/预算与异常不改主状态、unknown `修复并重试` 只排正式对账、不重置；即时确认与重启对账共用账号证据规则。离线演示 `tests/test_publication_round2_demo.py` 覆盖资产缺失→同字节恢复→一次 POST→成功通知重启→重复事件不重发，以及跨账号同标题不认领、超时 unknown→同事务自动入队→队列退避→重启 worker 认领原 attempt（无需主人发消息）；主人同时“修复”时复用同一 active 对账链，crash-window 由 startup 恢复缺 job。定向 **77/77**（517.425s）、主全量 **354/354**（943.785s，`var/tests/full_suite_round2_startup_attempt_source.log`）通过；migration 11 临时副本 `MIGRATION_REHEARSAL_OK`。**未部署、未迁移 live、未真实发布。** 详见 [Round-2 交付报告](../delivery/PUBLICATION_AGENT_ROUND2_DELIVERY.md)、[回滚说明](../delivery/PUBLICATION_AGENT_ROLLBACK.md)。
- **发布与故障恢复 Agent R1–R6 返修 + handoff 反例（离线）**：已完成 R1（确定失败/unknown 分离）、R2（claim 事务最终 fence）、R3（预发送基线 + feed 对账、unknown 取消）、R4（批次/日预算、预留、唤醒去重、只读诊断）、R5（不可变资产 manifest/原子恢复）、R6（V2 来源归属、work_key、配额、冻结包、simulated 分离）。另修复“诊断派生 job 越权发布”“旧同标题笔记误认成功”两个 handoff 反例。定向 publication/xhs 回归 **71/71 OK（217.391s）**；主全量 **Ran 333 tests in 799.335s, OK**（日志 `var/tests/full_suite_publication_handoff_final.log`）。migration 11 仅在 live 临时副本演练（v10→v11、二次 init 幂等、integrity ok；新增 `pre_send_baseline_json`）。
- 仍**未部署**：`publication_agent.enabled=false`，live DB 仍为 10，未真实发布/未重放历史失败稿；自然语言/无编号目标、桥接阶段日志/截图/toast/2 分钟 deadline、维护窗口回滚仍未完成。详见 [返修交付报告](delivery/PUBLICATION_AGENT_DELIVERY.md)、[handoff 补充报告](delivery/PUBLICATION_AGENT_HANDOFF_DELIVERY.md)、[架构](../PUBLICATION_AGENT_ARCHITECTURE.md)、[使用](../PUBLICATION_AGENT_USAGE.md)、[独立审查](delivery/PUBLICATION_AGENT_INDEPENDENT_REVIEW_20261118.md)。

## 状态（2026-11-16）

- **V2 每日自动发现已接回**：`creator_agent.daily_discovery_enabled=true`，worker 每 24h 入队 `creator_discovery`；AIHot feed → 过滤时效/去重 → `CreativeBrief.source_url` 原文链接 → 每候选一个 `creator_agent_run`。worker 会在 run 开始前预取一次原文作为种子材料，模型仍自行决定搜索/写稿/审稿；只生成 pending approval，不自动发布。旧 `collect`/`draft` 自动排期已停用，旧 handler 不再注册；历史数据保留。
- 新增 `bot/services/creator_agent/discovery.py`、worker `creator_discovery` handler、`creator_agent.daily_discovery_*` 配置；`tests/test_creator_daily_discovery.py` 4/4 通过。
- 当前 `XH-4989WZ` 为 `revision_pending`，无自动发布；V2 上次真实发布笔记仍为 `6af69d16000000002903c3c1`。

- **选题默认进入 Creator Agent V2**：`bot/services/approvals.py` 将不带 `V2` 前缀的 `选题` / `写一篇` 也路由到 `creator_agent.entry.accept_inbound`，不再创建旧 `topic_requests`。`V2 选题` 仍是等价别名；旧固定编排只保留历史请求/回滚，不接收新选题。新增 `test_plain_topic_routes_to_v2_without_legacy_request`。
- **Owner 提供的日期/优惠不再被误删**：`validate_draft_payload(..., repair_caption_claims=True, owner_context=...)` 会保留来自选题/修改说明的未抓取事实，并在审批中标注“来自选题或修改说明，未在抓取来源中核实”；未提供的无来源数字/促销句仍被移除或拦截。V2 写稿使用该路径。
- 最新定向：`tests.test_content_numbers tests.test_creator_agent_live tests.test_creator_agent_v2` = **59/59 OK**（80.001s）；删除旧 topics 测试后主项目全量 **267 tests in 441.712s, OK**（日志 `var/tests/full_suite_agent_only.log`，仅保留一个 asyncio ResourceWarning）。已重新部署全部三个服务：worker **PID 14392**、gateway **24272**、console **1796**（均加载删除 topics 后的代码）；health `pending=0`、`publish_unknown=0`、real armed。
- **旧固定选题编排已物理删除**：`bot/services/topics.py` 删除；`worker` 不再注册 `topic` handler，`approvals.py` 不再调用 `handle_topic_message/_classify`。未知旧命令只回复“旧固定选题命令已退役”，不创建新请求。对应旧 topics 测试已移除；V2 主路径、审批/发布/安全回归测试保留。
- 尚未做：旧 research_plan/topic_research 等历史模块的进一步清理、V2 默认路由的主项目全量回归、`XH-4989WZ` 修订恢复验证。当前 `XH-4989WZ` 为 `revision_pending`，无自动发布。

## 最新状态（2026-11-13）

- **Creator Agent V2 已部署并完成真实发布验证**：V2 从隔离副本复制到主项目，追加 migration 9（live DB 已执行，`integrity_check=ok`），接入 live worker 的 `creator_agent_run` handler、Feishu `V2` 前缀入站和真实 pending approval 桥。`LiveRuntime` 已接真实搜索/抓取/写稿/Pillow 渲染/确定性布局检查/审稿；`submit_for_approval` 生成真实审批，批准后仍走旧发布闸门与 feed 回执确认。N1 追加 migration 10（`creator_agent_materials` 与 `drafts.submission_key/hash`），**已于 2026-11-14 02:37 +08 应用到 live DB，version=10，integrity_check=ok**；迁移后备份 `var/live/backups/post-r1r2-migration-20261114-0237.sqlite3`。
- V2 首次真实 run `ca-ce0670cbe414c663b98b`：8 步、1 次搜索、8 次模型调用；写稿失败 3 次（截断、对比表列数、段落超限）后自主修正；`review_artifact=pass` → 草稿 #17 / 审批 `XH-2FMN8K`。owner 飞书批准后发布尝试 #12 成功，feed 自动确认笔记 `6af69d16000000002903c3c1`，URL `https://www.xiaohongshu.com/explore/6af69d16000000002903c3c1`；approval/draft/attempt 均为 `published`，`publish_unknown=0`。
- 独立评估后的 A 组修复：V2 修改绑定旧 artifact/审批并保留图片；submit 幂等、artifact 不可变、journal-before-side-effect；claim quote/数字校验；manual lineage；共享物理抓取、writer 调用计数、generation CAS。T1–T4 收尾：父 artifact/材料进入观察；最终图片 manifest 经 hash 校验写入 draft；generation CAS 前置、断点恢复复用同一 draft 并回填 lineage；migration 10 持久化材料/正文，`resume_deferred`、语义审稿调用计数已接入。N1–N4：`submission_key/hash` 唯一提交键、审批事务内取消/代际 guard、保留图片缺失/丢页/解码硬校验、重启后模型/抓取预算续接。
- V2 主项目定向测试（R1/R2 返修后）：`test_creator_agent_v2 + test_creator_agent_live + test_migration_v2` → **48/48 OK**；加 `test_approval_publish` → **67/67 OK**。R1 覆盖恢复时换图片/正文/顺序拒绝、审批与通知唯一；R2 覆盖每次 transport attempt 前预扣、最后一次调用后阻断；新增 `handle_inbound_message → creator_agent_run → AgentRunner → 修订 → pending approval` 穿透测试。修复手机验收首轮小写 `v2` 前缀、文本修订清空图片、失败 run 静默：`strip_command_prefix()` 大小写不敏感、`revise_artifact` 默认携带父图、`run_job` 为 failed/deferred/needs_owner 写幂等 outcome。正文超限改为 V2 可审批软限制：保留来源块、按段落/句子压缩 caption，追加自动压缩提示；`tests/test_content_soft_limit.py` + `test_creator_agent_live` = **30/30 OK**（53.520s），live worker 已加载；未自动发布。小红书公开正文现已去除 `【来源】`/URL 和“以上为公开资料整理”报告腔，来源保留在审批元数据；V2 写稿指令改为第一人称口语化，禁止参考文献/URL/报告体；`test_content_soft_limit + test_creator_agent_live + test_creator_agent_v2 + test_migration_v2` = **52/52 OK**（78.940s），worker 8524 已加载。主项目全量：**Ran 367 tests in 812.120s, OK**（`var/tests/full_suite_r1r2_final.log`，本次手机修复前记录，尚未重跑全量）。migration 10 已应用到 live（version=10），worker/gateway 已重启为当前代码。
- **2026-11-16 V2 步数失败修复**：真实选题 run `ca-ec5e169d1a4df0f926dd` 因 `max_steps=8` 和 claim quote 只取正文前 300 字，在审稿/修订循环中耗尽步数。live `[creator_agent]` 调整为 `max_steps=16`、`max_revision_steps=10`、`max_review_cycles=3`、`max_model_calls=30`、`max_invalid_decisions=3`、`max_no_progress_steps=3`、`max_run_seconds=900`；新增 `_quote_for_claim()` 按数字定位原文窗口。重试 run `ca-8cd0a41d4f6014de39d7` 6 步完成，草稿 #19、审批 **`XH-V4XEQD`** pending，通知已发送；V2 定向测试 **53/53 OK**（103.481s）。未自动发布。随后 16 步 run `ca-021f8cf1fa242f207775` 暴露更深设计问题：claim quote 由正文重新生成，正文删掉数字后仍把旧 claim 标成 `supported`，审稿要求“让 quote 出现在材料中”，模型无法修复，连续修订至步数上限。已改为：当前正文不再包含 claim 数字时，该 claim 标记 `uncertain` + 空 quote，不再生成不可修复的阻断项。复跑 run `ca-036045a15f031a0ca924` 8 步完成，审批 **`XH-8MXKJX`** pending（草稿 #20），通知已发送；V2 定向 **54/54 OK**（96.505s）。未自动发布。随后 run `ca-38663e9bed94630562d0` 又暴露真正根因：`Material.quote` 只保存正文前 900 字，而 claim quote 从完整 body 生成；审稿只看到 900 字，于是永远判定 quote 不在材料中，模型无法修复。已改为从审稿实际可见的 `material.quote` 生成 claim quote，并将 live 抓取 excerpt 扩到 3000 字；不在 excerpt 的数字标 `uncertain`，不再阻断。复跑 run `ca-f94b471d05e0653a76c1` 6 步完成，草稿 #22、审批 **`XH-4989WZ`** pending（通知 167 sent）；V2 定向 **55/55 OK**（125.964s）。未自动发布。
- V2 使用方式：owner 私聊发送 `V2 选题 <主题/链接>`，完成后收到普通 `批准 XH-...` 卡片；不会自动批准或发布。旧流程仍可运行，作为回退路径。
- 限制：`discover_topics` 仍返回空，热点发现继续走旧调度；没有视觉模型；发布仍复用旧门和 feed 确认。详见 [Creator Agent V2 交付报告](delivery/CREATOR_AGENT_V2_DELIVERY.md) 与 [使用说明](../CREATOR_AGENT_V2_USAGE.md)。

## 状态（2026-11-12）

- **P0 内容 Agent 已实现并加载（2026-11-12）**：新增有界审稿 `bot/services/critic.py`、自适应补检 `bot/services/adaptive_research.py`、失败自动补救 `_rescue_topic_evidence`（writer 因证据不足拒绝时最多补检一次/再抓 2 次，仍走正文、时效、身份、预算与审批）和离线评估指标 `bot/services/agent_eval.py`。审稿可 `pass/revise/reject`，最多一轮重写；补检最多 2 个查询；评估层只汇总 replay 记录，不调用模型。owner 于 21:15 关闭 live 审稿（`agent.critic_enabled=false`），审稿代码/测试保留；`topics.adaptive_research=true`、`topics.auto_rescue=true`，示例配置默认关闭。全量 `Ran 322 tests in 778.082s, OK`（日志 `var/tests/full_suite_rescue.log`），worker 21:40 重启加载 rescue（PID 19632）；rescue 追加后 77 项定向测试通过；未做真实 20-topic 评测。详见 [P0 内容 Agent](../AGENT_LOOP.md) 与 [课程大作业设计](../COURSE_AGENT_DESIGN.md)。
- **AIHot 发现层已实现并部署**：`bot/services/aihot.py` 读取 `https://aihot.news/feed.xml`，优先改用原文；原文未取到才使用 `据 AIHot 汇总（原文未取到）：` 摘要。AIHot 摘要强制 `secondary/usable`、永不 `source_verified`；审批卡标“AIHot 聚合摘要/原文未取到（非官方）”。
- live 已开启 `topics.aihot_enabled` / `topics.aihot_auto_match`；适配器只对配置 feed 主机放行（本机解析到非公网地址），仍从 `/feed.xml` 直接读取，普通 SSRF/重定向策略未放宽。
- 链路测试 `XT-EFKZF9` 使用 AIHot 条目 `OpenRouter 实测六模型...`，解析到原文 `openrouter.ai`，生成草稿 13 与审批 **`XH-DXQC9Z`**；未自动发布，已作为链路测试拒绝（`rejected`），不会发布。
- 回归：`test_aihot + test_research_s2 + test_s3_modes + test_remote_topics` 共 70 项通过；随后 `test_aihot + S1 + S1-review + S2 + S3 + approval_publish` 共 80 项通过；全量离线套件 `Ran 295 tests in 708.289s, OK`（日志 `var/tests/full_suite_aihot.log`）。
- **小红书 2xx 无回执自动核对（2026-11-12）**：桥接经常在真实发布成功后仍只返回 HTTP 2xx、不带 note id/URL，旧逻辑会把成功误报为 `publish_unknown`。现发布前先读取 `/api/v1/user/me` 的账号 feed 快照；POST 2xx 无回执时只读复查 feed，只有“恰好新增一条 note id、标题与草稿完全一致”才写 `published` 并记录真实 id/xsec URL。无新增、标题不一致、多条歧义或旧同标题笔记都保持 `unknown`，绝不凭 2xx 猜测，也绝不重发。`tests/test_xhs_identity.py` 增至 11 项；`test_xhs_identity + test_approval_publish + test_aihot` 共 37 项通过。worker 已重启为新代码（PID 12752）。
- 早前 `XH-8Q9256`（草稿 12）已由桥接 2xx 后的真实 feed 对账为 `published`，note `6af5694500000000240303e1`；没有重发。`XH-MJD95P` 仍为 `revision_pending`，不要当作可发布审批。
- 详见 [AIHot 发现层](../AIHOT_DISCOVERY.md) 与 [S1–S3 交付报告](delivery/S1-S3-research-delivery.md)。

## 历史状态（2026-11-11）

- S1 收尾、S2 研究计划/来源身份、S3 news/analysis 模式与 owner 私聊运维命令已离线实现；独立验收 R1–R4 已返修并补端到端回归；共享托管站机构/路径作用域已返修；迁移 8 已在合成 v7 库演练，未接触生产数据库。
- 独立验收 R1–R4 已返修：新来源正文进入单批身份核验；可信链接改为真实 URL/exact host/上下文检查；预算与模型异常不再产生 verified evidence；analysis 使用独立发现日期策略。详见交付报告的“独立验收 R1–R4 返修”节。
- 共享托管作用域已返修：GitHub/Bitbucket/Gitee 按 owner/repo（含子页）授权，其他站点按 exact resource；编码路径/前缀碰撞被拒；旧 host 级缓存因策略 fingerprint 失效；只有真实链接到候选的可信关系页进入 identity。详见交付报告“独立复验：共享托管来源作用域返修”节。
- 第三方来源策略已调整：不再要求所有可用来源预先列入可信域名；有实际正文支持且置信度达标的 `secondary` 媒体可作为草稿来源，但 `source_verified` 保持 false，审批卡片标“非官方媒体/转述”，writer 必须用“据/媒体称/报道”措辞；官方/一手仍需严格核验。新增端到端测试。
- **标题校验修复（2026-11-11 18:00 +08）**：`XT-GNAZE6` 的 22 字标题被 20 字平台上限拒绝，导致整篇草稿失败。`bot/content.py` 新增 `_fit_title()`，超长标题确定性地收短到 20 字以内（保留省略号），不再让整篇草稿失败；`tests/test_title_fit.py` 4 项通过，另跑 `test_editorial_autonomy`/`test_render` 共 21 项通过。
- **立即发布与额度（2026-11-11 18:18 +08）**：live 配置 `limits.publish_immediately=true`，批准后草稿排期取当前时间，不再等到晚间槽位；审批/正文/图片/发布闸门不变。`topics.max_per_day` 从 5 提高到 10；`limits.max_manual_drafts_per_day` 仍为 5，今日草稿计数不是 5/5 的原因。`tests/test_immediate_publish.py` 2 项通过；`test_immediate_publish + test_approval_publish + test_title_fit` 共 25 项通过。18:23 已用新配置重启 worker。
- **发布未知（2026-11-11 18:19 +08）**：已批准的 `XH-MUEAA5` 在改为立即发布后由 worker 发起 attempt #3；小红书桥接返回 HTTP 500，记录为 `publish_unknown`，没有重发。审批/草稿均为 `publish_unknown`，通知 57 已明确提示“不会自动重试；请只读核对平台，确认未发后再重新生成并审批，切勿直接重发”。必须先在平台人工确认是否已发出，不能把这次 unknown 当失败重发。备份为 `var/live/backups/pre-immediate-20261111-181834.sqlite3`（`integrity_check=ok`）。
- **已部署（2026-11-11 17:46 +08，18:00/18:23 重新拉起）**：重新拉起 console/worker/gateway；console `/healthz` 为 `ok=true, mode=live, demo=false, real_publish_armed=true, pending_approvals=0, publish_unknown=1`，飞书长连接已连上 `msg-frontier.feishu.cn`，worker 已开始 tick。`scripts/launch.py` 的原 `CREATE_NO_WINDOW` 在这套内嵌 Python 上触发 `0xC0000142`，已改为 `STARTUPINFO` 隐藏窗口后通过 `scripts/start.ps1` 启动。部署前已备份 live 库到 `var/live/backups/secondary-policy-20261111-1741.sqlite3` 并验证 `integrity_check=ok`。
- 最近离线测试：102 项针对性测试通过；全量 `Ran 266 tests in 599.348s, OK`；全量日志 `var/tests/full_suite_secondary_verbose.log`。部署只重启进程并修改启动脚本/额度/立即发布配置，未改发布闸门。
- 真实飞书私聊和真实发布均已发生，但 `XH-MUEAA5` 的结果是 unknown，不能宣称成功；真实 provider、跨语言检索质量和官方文章召回仍需继续验证。
- 详见 [S1 收尾 + S2/S3 交付报告](delivery/S1-S3-research-delivery.md)。

## 历史状态（2026-11-09T20:27+08:00）

- **真实小红书发布已确认**：审批 `XH-ACEPGR` 对应的发布尝试已由用户提供真实帖子链接/ID，人工对账为 `confirmed_published`；`publish_attempts.external_post_id = 6af1b81f0000000036023c27`，`external_url = https://www.xiaohongshu.com/discovery/item/6af1b81f0000000036023c27`。审批、草稿、事件均为 `published`，该审批只有 1 次发布尝试，没有重发。
- 当前完整离线套件为 **174 项测试通过**（`Ran 174 tests`, `OK`，约 273 秒；2026-11-10 复跑）；此前 166/104 项是历史验收数字。
- 控制台 `/healthz` 当前为 `publish_unknown: 0`、`pending_approvals: 0`、`real_publish_armed: true`；live 服务仍在运行，未配置开机自启。
- 最新运行、编辑自主性升级、首次真实发布超时与第二次 ambiguous 回执的完整时间线见 [SESSION_HANDOFF_20261108.md](SESSION_HANDOFF_20261108.md) 的末尾更新。

## 2026-11-10 采集隔离与选题来源复核

- **定时采集停摆已修复**：`bot/services/research.py` 新增 `is_commissioned_topic_source()` 与 `recurring_sources()`，30 分钟采集明确跳过 `deepseek-search://topic/<code>` 这类委托选题遗留的临时来源；`ingest_source` 把 `adapter_for()` 移入 `try`，单个来源缺少 query 时只记录该来源错误并返回，不再让整个 collect job 失败。`bot/worker.py::_handle_collect` 改用 `recurring_sources()`。
- **每日总配额与人工委托配额分开检查**：`bot/services/topics.py::_quota_reason` 先检查 `limits.max_drafts_per_day`（自动 + 人工合计），再检查 `limits.max_manual_drafts_per_day`，避免自动草稿用掉当天额度后人工选题仍继续生成。
- **模型只做“一次性来源建议”，不获得长期信任**：新增 `[topics] model_source_review`（默认 `false`，live 配置显式开启）、`model_source_min_confidence=0.7`、`model_source_max_domains=3`。配置可信域名未命中时，模型最多建议 3 个官方主机名；本地校验（禁止 URL 路径/端口/通配符/IP/私网/本地域）后，仅对**当前这条**委托选题追加一次搜索。建议写入 `topic_requests.suggested_domains_json` / `source_review_json` 供审计，不写入 `[sources].allowed_domains`，不影响之后的选题；审批消息会显示“来源范围说明”，最终仍必须由 owner 看正文和图片后批准。
- 新增测试：`tests/test_collect_isolation.py`（2 项，来源隔离/错误不扩散）、`tests/test_source_review.py`（5 项，域名校验/审计/失败降级/审批提示/一次性扩展搜索成功）。
- 全量离线测试：`Ran 174 tests in 273.360s, OK`（`var/final-test-run-20261110.log`）。
- 运行处置：确认 live 库无 pending 审批、无 unknown 发布、无 `publishing` 尝试后，已禁用遗留的无 query 动态来源 `id=2`（保留行和审计记录，`enabled=0`），避免旧 worker 继续被它拖死；旧 worker 已停止，新 worker 已用新代码重启（本会话的受管前台进程，日志走会话作业输出）。DSH 沙箱仍不允许 `launch.py` 的隐藏子进程稳定存活（退出 `0xC0000142`），因此本会话没有宣称后台服务已持久化；在普通 Windows 终端使用 `scripts/start.ps1`，并确认它写入 `var/run/worker.pid`/`worker.json` 后再依赖 stop/health 脚本。（2026-11-11 更新：已定位为 `CREATE_NO_WINDOW` 在 embedded Python 上触发 `0xC0000142`，改用 `STARTUPINFO` 隐藏窗口后已能启动并保持运行。）
- 仍需观察下一次 30 分钟采集（约 15:58 +08）是否不再出现 `NotConfigured: deepseek_search source requires a query`；代码和隔离测试已通过，但这属于运行观察项，不是已完成的外部验收。

> 以下保留 2026-11-06 的原始 DSH 实现记录与当时的外部阻塞说明；其中“真实发布未完成”“104 项测试”等表述是历史状态，不是当前状态。

## 主控真实验收补充（2026-11-06T17:40:19+08:00）
以下原文保留 DSH 实现会话的范围；其中本会话未联网/无凭据/外部未配置等，是 DSH 当时的限制，不是当前整项目状态。主控随后完成普通 .venv 的 104 项测试、正式 DeepSeek 写作与单轮 Search 采集、飞书通知送达及本人私聊事件接收。完整图文手机审批彩排也已通过（2026-11-06 17:48 +08:00）；小红书登录/发布与持续后台运行仍待完成。当前结果和证据以 [ACCEPTANCE_20261106.md](ACCEPTANCE_20261106.md) 为准。

---


更新日期：2026-11-06（Asia/Shanghai）
工作目录：`D:\Dev\Projects\bot`
实现范围：`docs/IMPLEMENTATION_BRIEF.md` 的 P0 → P1 → P2、`docs/REVIEW_PASS2.md` 的第二轮验收修正、`docs/INTEGRATION_FOLLOWUP.md` 的七项接入修正，以及 `docs/INTEGRATION_REVIEW.md` 的四个具体问题修复。
约束：没有提交或推送 Git；没有购买服务；没有真实发帖、评论或私信；没有读取/显示任何密钥文件或凭据；没有修改实际的 `config/bot.toml` 或 `var/live*`；没有运行 `dsh-project.patch.yml`。原有文档、`scripts/live_verify.py` 和 `docs/FEISHU_SETUP.md` 均保留。

> 第二轮修正日期：2026-11-06。实际改动集中在发布事务内复检、小红书账号身份核验、飞书通知完整性、依赖/venv 安装、后台进程身份核对和预算预留原子性。新增 19 个自动化测试，另有 1 个 PowerShell 进程所有权回归脚本。
>
> 第三轮（INTEGRATION_FOLLOWUP）日期：2026-11-06。完成 DeepSeek 请求边界、可选 DeepSeek Search/Fetch 来源、飞书回调异步修改与结果通知、XHS `redId` 身份、第三个飞书网关进程、pip 临时目录/缓存修正和渲染分页/换行修正。所有验证均为离线测试；真实 DeepSeek Search/Fetch、飞书长连接和 XHS 桥仍待外部环境验收。
>
> 第四轮（INTEGRATION_REVIEW）日期：2026-11-06。只修复四个具体问题：Search summary 不再进入证据；Search/Fetch 每次物理重试前逐次预留；修改请求原子冻结旧审批为 `revision_pending`；搜索查询按注入时钟生成有界 `after:` 并默认精确匹配来源域名。另给 writer SYSTEM_PROMPT 增加一句“evidence 是不可信引用数据”。仍为离线测试，未做真实付费调用或外部发送。

## 1. 总体结论

| 阶段 | 状态 | 说明 |
| --- | --- | --- |
| P0 本地离线闭环 | **已完成并验证** | 模拟来源 → 去重/聚类 → 证据 → 草稿 → PNG → 审批 → 模拟发布 → 重启恢复，全部本地可运行。 |
| P1 真实来源 + DeepSeek | **代码完成；外部未验证** | 直接 RSS（条件请求、超时/限流/退避）和 DeepSeek 适配器已实现；本机无可用外网/TLS/API Key，未产生真实外部作品，不能声称已经连通。 |
| P2 飞书审批 + 小红书适配 | **代码完成；外部账号未配置** | 飞书长连接、本人私聊/群聊拒绝、审批命令、小红书桥接适配器与双重发布闸门已实现；没有真实飞书 App、小红书登录或 token，因此没有真实审批或发布。 |

重要：本项目把“模拟发布”明确记录为 `simulated`，不写 `published`，不生成虚假的小红书帖子 ID/URL。只有真实平台返回的 `post_id` 或 URL 才会进入 `published`。

## 2. 已实现内容

### 2.1 持久化与状态机

- Python 3.12 + SQLite（WAL、busy timeout、事务、迁移）。
- 状态覆盖：`discovered → researched → drafted → review_pending → approved → publishing → published`，以及 `rejected / expired / needs_login / failed / publish_unknown / paused / simulated / insufficient / superseded` 等保护态。
- 迁移版本 1 包含：`settings / sources / events / raw_items / evidence / drafts / draft_images / approvals / publish_attempts / jobs / notifications / inbound_messages / llm_usage / account_memory / metrics_snapshots / audit_log`。
- 迁移版本 2（第二轮）：`llm_usage.settled`，用于“联网前预留、响应后结算、未知错误保留预留”。
- 任务队列带 `state / next_run_at / lease_owner / lease_until / attempts / max_attempts / dedupe_key`；重启会回收过期租约。

### 2.2 来源、去重与证据

- RSS/RDF/Atom 解析、条件 GET（ETag / Last-Modified）、304、相对链接、HTML 清洗、大小/超时/重试限制、每主机限速。
- 同源重复抓取不重复入库；转载/联合发布通过标题相似度与内容指纹去重；同一事件聚合多来源证据。
- 未知发布时间会显式标记，不会伪装成“今天”；过期新闻（默认 72 小时）不进入选题队列。
- 所有外部网页内容只作为数据存储，不能改变权限；提示注入文本会被保留用于审计/人工查看，但不会触发工具、规则、密钥读取或发布。
- 采集 SSRF 防护：默认阻止私网/回环/云元数据地址；新闻 URL 永远不能访问本机小红书桥。仅小红书桥适配器在显式配置下拥有自己的 loopback 客户端。
- 第三轮新增可选 DeepSeek Search/Fetch 来源（`kind="deepseek_search"`）：固定 `https://api.deepseek.com/v1/tools/search|fetch`，复用 DeepSeek key，不直连结果 URL。来源要求非空 query 与 `allowed_domains` 允许列表；结果 URL 必须是 http(s)、无内嵌凭据、非 localhost/私网/元数据字面量且域名匹配。第四轮起：provider 的合成 `summary` 不再写入 `SourceEntry.summary`，空 content 仍跳过，证据只来自真实 content；每次物理 search/fetch HTTP 尝试前分别用 `BudgetGuard.reserve` 预留固定 0.00075 USD，模糊尝试各自保留保守预留，官方空结果仍结算 0；`lookback_days`（默认 3，限制 1..30）在每次 fetch 时按注入时钟生成 `after:YYYY-MM-DD`，结果原始发布时间原样保留，缺失日期仍走 unknown，旧闻仍由既有 freshness gate 拒绝；`allowed_domains` 默认精确匹配主机名，`*.example.com` 才是显式子域放行。

### 2.3 写作、渲染与人工审批

- 草稿强制包含「发生了什么 / 对谁有用 / 具体怎么用 / 限制与不确定性 / 来源」；事实必须引用证据 ID；厂商自述自动标为“厂商称”；数字必须能在证据/日期中出现；禁止第一人称实测、收益承诺、违禁表达；未知日期和“未做个人实测”会自动补入限制段。
- Pillow 本地字体渲染 PNG（1080×1440，微软雅黑；自动分页，超过 `max_cards` 直接报错而不是截断）；每张图按内容计算 SHA256。
- 第三轮渲染修正：章节标题带 `keep_with_next`，分页时若标题和第一条正文不能同页，则标题移到下一页，避免页底孤立标题；拉丁词（Fetch/RAG/lang 等）在整词能放进一行时不再被逐字截断。封面移除内部 `draft_key` 和重复的账号标签，续页头也不再重复“AI 工具观察（续）”；模板其余部分保持原样。
- 审批码格式 `XH-XXXXXX`；审批指纹绑定：平台、账号、draft_id、draft_version、正文 SHA256、图片内容 SHA256、计划发布时间、审批过期时间。
- 修改正文、替换/追加图片、切换账号或平台、修改计划时间/过期时间，均会使原审批失效。
- 审批只接受配置的 owner（`LARK_OWNER_OPEN_ID`，不能误用 chat_id），并要求 p2p 私聊；群聊、非 owner、无指令文本、重复消息 ID 均不会触发发布。
- 审批到期会同时处理 `pending` 和 `approved` 状态，避免“已批准但过期后仍可发布”。
- 本地控制台：仅回环地址，登录口令 + HttpOnly cookie + CSRF；GET 只读，所有决定走 POST；预览图需要登录；审批按钮与飞书命令复用同一审批服务。
- 飞书适配器：官方 SDK 可选依赖 + 长连接（无公网入站端口），文本通知包含最终正文、图片数量、正文/图片哈希、计划时间、过期时间和命令；支持随后上传图片消息。
- 飞书通知完整性（第二轮修正）：正文或任意一张图片未确认送达时，`send_approval` 返回 `ok=False`，通知保持 `retry`（最多 5 次）并在错误字段/控制台审批页显示失败原因；飞书（`channel=lark`）的“批准”只有在通知状态为 `sent`（正文 + 全部图片）时才生效，提前回复会返回 `delivery_incomplete` 且审批保持 `pending`。已接受的本地决定不会被重发通知覆盖或重复执行；经认证的本地控制台和本机 CLI 仍是独立有效路径。
- 飞书回调时限（第三轮）：`handle_inbound_message` 不再同步调用模型；收到 owner 私聊的“修改”后只写 `inbound_messages` 幂等记录并入队 `revise` job，worker 稍后调用 `revise_draft`。回调结果（批准/拒绝/未完整送达/失效/过期/修改已排队等）写入 `notifications` 的 `kind=outcome` 持久队列，只由 notifier 发往已绑定 owner 的私聊；回调 payload 不包含可指定收件人的字段。重复 `message_id` 返回已有结果，重复修改不会重复建 job。
- 第四轮修改冻结：回调在**同一个事务**内把 pending/approved 旧审批改为 `revision_pending`，旧草稿/事件同步冻结，取消已排队的 publish job，并原子入队 revise job；`revision_pending` 不可批准/发布。已进入 `publishing/published/publish_unknown` 的请求拒绝修改；模型调用仍在事务外。修订失败只留下 `revision_pending`（不会恢复 approved）；成功时旧审批转 `superseded` 并创建新审批。重复 message_id 或同一审批的第二条修改消息不会重复建 job。

### 2.4 发布闸门与幂等

- 发布前重新检查：审批状态、审批/草稿版本、正文哈希、图片文件内容哈希、计划/过期时间、账号启用与核验状态、来源/事件状态、每日发布上限、全局暂停。
- 第二轮修正：上述所有适用 DB 条件（含每日发布配额、过期、正文/图片哈希、来源、账号、已有尝试）现在都在 `claim_publish` 的同一个 `BEGIN IMMEDIATE` 写事务内重新检查；发布请求使用该事务内重新验证后的审批/草稿/图片，而不是事务外的旧快照。两个不同审批同时抢 `max_publish_per_day=1` 时，只有一个能插入尝试。
- `publish_attempts.dedupe_key` 唯一约束 + 事务内抢占（`claim_publish`）；并发点击/回调重试只会产生 1 次尝试。该保证只覆盖同一个 SQLite 数据库，**不是跨机器/跨库的分布式 exactly-once**；网络发布仍可能进入 `unknown`。
- 已批准但超时 → 不发布；未明确批准/未回复 → 不发布。
- 请求已发出但没有回执 → `publish_unknown`；不盲目重试，提供 `reconcile` 人工确认。重启时 `publishing` 也进入 `unknown`，绝不重发。
- 小红书真实发布前先只读检查 `/api/v1/login/status`，再调用 `/api/v1/user/me` 确认当前登录账号。上游文档只证明了 `data.data.userBasicInfo.redId`（公开账号号），没有稳定内部 `user_id`；因此只按显式配置的 `publisher.xiaohongshu.expected_red_id`（或 `XHS_EXPECTED_RED_ID`）精确匹配该 `redId`。不把 `redId` 标成不可变 `user_id`，也不接受猜测的 `user_id`/`id`/昵称形状；缺失期望值、返回 401/403、未登录、`redId` 缺失/不可解析或不匹配都会在 POST 之前拒绝。本地别名（如 `xhs-main`）不能作为期望值。
- 只有真实适配器返回明确的帖子 ID/URL，或 2xx 后账号 feed 出现“唯一新增且标题与草稿完全一致”的笔记，才写 `published`；文档所述 `POST /api/v1/publish` 的 `data.status=发布完成` 不含 note id/URL，普通 `data.id` 也不当作 note id，因此仍为 `publish_unknown`；即使适配器错误声称成功，也会降级为 `unknown`。
- 模拟发布写 manifest/文本/图片副本，状态为 `simulated`，manifest 明确 `"simulated": true`，不包含外部帖子 ID/URL。

## 3. 实际验证结果

### 3.1 自动化测试

命令：

```powershell
$env:PYTHONPATH="."
$env:PYTHONIOENCODING="utf-8"
.venv-clean\Scripts\python.exe -m unittest discover -s tests -v
```

**结果：104 个测试全部通过（最终一次 Ran 104 tests in 136.790s, OK；命令实际退出码 0）。** 第四轮新增 9 个针对性测试：Search 证据/逐次预算/时钟 after/精确域名/旧闻 6 个，修订冻结与失败保护 3 个；未重装依赖、未做真实付费调用或外部发送。`tests/test_process_ownership.ps1` 的既有结果保留；本轮未重复启动真实服务。

| 测试文件 | 数量 | 覆盖 |
| --- | ---: | --- |
| `tests/test_approval_publish.py` | 19 | 未批准不调用发布、owner/p2p、重复回调、正文/图片/账号/时间改动失效、过期、异步 `revise` 队列、修订原子冻结/发布闸门取消/失败不恢复 approved、unknown/reconcile、重启恢复 |
| `tests/test_ingestion.py` | 5 | 重复抓取、过期新闻、联合转载去重、未知日期、恶意来源文本仅作数据 |
| `tests/test_render.py` | 5 | PNG 可打开/尺寸/哈希、预览图与发布图一致、长文自动分页、溢出报错、拉丁词不截断、标题不孤立 |
| `tests/test_security_network.py` | 9 | SSRF、DNS、重定向、429/503 重试、大小上限 |
| `tests/test_config_budget.py` | 14 | live/fake 守卫、armed 条件、owner 独立性、预算未知用量预留、预算耗尽后审批仍可用、USD 字段、`BOT_PUBLISHER_URL` 嵌套覆盖、live 拒绝 fixture/fake 内容、并发预留、DeepSeek 先预留后联网、未知错误保留预留 |
| `tests/test_console.py` | 4 | 未登录跳转、CSRF、媒体鉴权、控制台决定只落审批不直接发布 |
| `tests/test_worker_e2e.py` | 4 | 重启保留待审批、批准后仅一次模拟发布、中断发布恢复为 unknown、真实适配器未配置守卫 |
| `tests/test_concurrency.py` | 2 | 同一审批两线程只产生 1 次尝试；两个不同审批抢 `max_publish_per_day=1` 只允许 1 次 |
| `tests/test_rss_source.py` | 2 | ETag/304、相对链接、标题/正文清洗、fixture 不得用于 live |
| `tests/test_metrics.py` | 2 | 24h/72h 指标缺失显式标记、模拟作品不采集 |
| `tests/test_notify.py` | 6 | 通知含最终正文/图片、飞书健康检查不谎报、畸形事件、图片部分失败 `ok=False`、提前手机回复被拒、完整重试后只接受一次、控制台已接受决定不重发通知 |
| `tests/test_xhs_identity.py` | 11 | 只接受文档化的 `data.data.userBasicInfo.redId`；不匹配/无 redId/猜测 user_id/401/未登录/缺期望 redId/本地别名均在 POST 前拒绝；2xx 无 note id 时仅接受 feed 中唯一新增且标题一致的笔记，未变/旧同标题/异标题仍为 unknown |
| `tests/test_setup_metadata.py` | 3 | 依赖 pin、`.gitignore` 覆盖、普通 venv 安装命令；无 `--upgrade pip`、项目内 temp/cache、`--no-cache-dir` 与有界网络等待 |
| `tests/test_launch_identity.py` | 1 | `launch.py` 写入 PID/可执行文件/命令行/项目根目录/配置路径身份文件 |
| `tests/test_deepseek_transport.py` | 4 | 默认 `thinking=disabled`、`finish_reason=length` 拒绝、错误体脱敏/截断、重定向不转发 Bearer |
| `tests/test_deepseek_search.py` | 14 | 来源配置贯通、summary 不进入证据、空 content 跳过、日期缺失、Fetch 计费、逐次物理重试预留/预算阻断、时钟 after 窗口、原始日期保留/旧闻不 freshen、精确域名与显式子域、私网/凭据/非白名单 URL 拒绝、空结果 0 结算、模糊错误保留预留、预算失败不联网、Search 来源入库不依赖本机 DNS |
| `tests/test_gateway_lifecycle.py` | 3 | notify=fake 不启用、lark+app+owner 才启用、start/stop/health 共用身份核对并管理 gateway |

### 3.1.1 首批复核项落地

| 复核项 | 落地方式 | 验证 |
| --- | --- | --- |
| 预算必须是美元且标注正确 | 配置只用 `daily_usd` / `input_usd_per_million` / `output_usd_per_million`，快照返回 `currency: USD`；不设人民币字段、不做隐式汇率 | `test_budget_is_explicit_usd_and_prices_are_usd` |
| `BOT_PUBLISHER_URL` 必须写到嵌套叶子 | 环境变量覆盖按完整路径 `("publisher","xiaohongshu","base_url")` 写入，不再写到错误层级 | `test_publisher_url_env_override_uses_nested_leaf` |
| 飞书 owner 不能由 chat_id 代替 | `configured_owner()` 只认 owner open_id；回调同时校验 sender、p2p/private 和 `message_id` 重放 | `test_owner_status_is_not_chat_id`、approval 套件 |
| fake/demo 不得进入真实发布 | 配置层 `demo_pipeline` / `is_live_publish_armed` 之外，发布闸门再按 source kind 和 draft generator 拒绝；测试用自定义 `mode=live` publisher 验证 | `test_live_publish_refuses_fixture_source_and_fake_generator` |
| 小红书 token 只从 env/secret 读取且只连本机桥 | 适配器要求 armed + token，base_url 必须 loopback；新闻采集使用独立 `SSRFGuard()`，不能借道本地发布服务 | 配置/发布守卫测试与适配器代码审查 |
| 审批绑定图片内容哈希；unknown 不盲重试 | 审批指纹和发布前复算覆盖正文/图片内容哈希、计划时间、账号；`publish_unknown` 只允许 reconcile，重启恢复不重发 | approval/worker e2e 套件 |

### 3.1.2 第二轮复核项落地（2026-11-06）

| 复核项 | 实际落地 | 实际验证 |
| --- | --- | --- |
| 每日配额/审批/内容/来源检查必须在 claim 事务内 | `_validate_publish_conn()` 通过 `claim_publish` 的同一写事务连接复检全部 DB 条件；请求改用 `PublishClaim` 里的 fresh approval/draft/images；claim 后未再使用事务外快照 | `test_two_distinct_approvals_race_for_one_daily_publish_quota`、`test_expiry_or_content_edit_between_validation_and_claim` |
| 真实 XHS 必须确认当前登录账号 | 第三轮按上游证据改为独立 `expected_red_id`（配置或 `XHS_EXPECTED_RED_ID`），只匹配 `data.data.userBasicInfo.redId`；发布前 GET `/api/v1/login/status` + `/api/v1/user/me`，缺失/不可解析/401/未登录/不匹配均在 POST 前拒绝；本地别名和猜测的 `user_id` 不能作为期望值。`allow_unarmed=True` 仅用于 doctor 只读健康检查，`publish()` 会拒绝 | `tests/test_xhs_identity.py` 7 项 |
| 飞书审批必须等正文和全部图片送达 | `LarkNotifier` 任一张图失败即 `ok=False`；`deliver_pending` 保持 `retry` 并显示错误；`channel=lark` 的批准要求通知 `state=sent`；提前回复返回 `delivery_incomplete`；已接受决定不会因重试被重发/重复执行；控制台路径不受影响 | `tests/test_notify.py` 新增 3 项 |
| 可复现依赖与普通 venv | `requirements.txt`（Pillow 12.3.0、lark-oapi 1.7.2）、`scripts/setup.ps1`、`setup-bot.cmd`；显式 `python -m venv`，不使用 `--system-site-packages`；`.gitignore` 覆盖 `.venv*`、`config/bot.toml`、`config/secrets.toml`、`var/`、缓存、`.env` | `tests/test_setup_metadata.py`；`setup.ps1 -Target .venv-setup-check -SkipInstall` 实际创建了 `include-system-site-packages=false` 的普通 venv（本沙箱 pip bootstrap 受限，见 3.3） |
| 后台进程不能只凭 PID 停止 | `launch.py` 写 `<name>.json` 身份文件；`start/stop/health` 共用 `process_identity.ps1`，核对 PID/名称/项目根目录/可执行文件/命令行（`-m bot`、配置路径、子命令）；CIM 不可用或身份缺失时 fail closed，不杀进程 | `tests/test_process_ownership.ps1`（实际 OK）、`tests/test_launch_identity.py` |
| 模型预算必须先原子预留再联网 | 迁移 2 增加 `llm_usage.settled`；`BudgetGuard.reserve()` 在同一写事务内检查余额并插入 `settled=0` 预留；`settle()` 用实际 usage 结算，`retain_unknown()` 保留保守预留；DeepSeek 每次网络尝试前预留 | `test_simultaneous_reservations_with_tiny_budget_allow_only_one`、`test_deepseek_reserves_before_network_and_settles_actual_usage`、`test_unknown_network_error_retains_conservative_reservation` |

### 3.1.3 INTEGRATION_FOLLOWUP 七项落地（2026-11-06）

| 项目 | 实际落地 | 离线验证 |
| --- | --- | --- |
| 1. DeepSeek 请求边界 | `llm.thinking_disabled=true` 默认显式发 `{"thinking":{"type":"disabled"}}`；`finish_reason=length` 在解析内容前拒绝；API 重定向返回 fatal 且不跟随时不携带 Bearer；`_safe_error_text` 先按 key/Bearer/sk- 脱敏再截断 300 字 | `tests/test_deepseek_transport.py` 4 项 |
| 2. DeepSeek Search/Fetch | 新增 `kind="deepseek_search"`、固定官方 HTTPS 端点、同 key、query + `allowed_domains`、结果/响应/时间上限；空 content 跳过、缺失日期走 unknown；每次 search/fetch 前原子预留固定 0.00075 USD，空结果结算 0，模糊错误保留预留；结果 URL 仅做无 DNS 的字面量/凭据/域名检查，入库时该来源也跳过本机 DNS（RSS 的 DNS 防护未改） | `tests/test_deepseek_search.py` 8 项；真实付费调用未执行，待主控 |
| 3. 飞书回调异步 | “修改”只入队 `revise`，worker 调用模型；回调按 `message_id` 幂等；批准/拒绝/未完整送达/过期/修改排队等写入持久 `outcome` 通知，只发绑定 owner 私聊；新草稿审批通知仍保留 | approval/notify 套件；回调不调用模型、重复消息不重复建 job |
| 4. XHS 上游证据 | 只认 `data.data.userBasicInfo.redId`，配置项改为 `expected_red_id`/`XHS_EXPECTED_RED_ID`；不把公开 redId 写成不可变 user_id，不接受猜测形状；无 redId 阻断 POST；只有 `data.status` 的发布回执保持 unknown | `tests/test_xhs_identity.py` 7 项 |
| 5. 持久飞书网关 | `gateway-state` 只读判断；`start.ps1` 在 notify=lark 且 app+owner 已配置时启动第三个隐藏 `gateway` 进程；`stop/health` 使用同一 `Test-BotProcessOwnership`；`launch.py` 接受 gateway；`bot gateway`/`bot lark` 均可启动 | `tests/test_gateway_lifecycle.py` 3 项；未真实启动外部连接 |
| 6. pip 安装卡住 | setup 使用项目内 `var\tmp\setup-*` 作为 TEMP/TMP、`var\pip-cache` 作为 PIP_CACHE_DIR；安装加 `--no-cache-dir --timeout 30 --retries 3 --disable-pip-version-check`；删除强制 `pip install --upgrade pip`；不修改全局环境 | `tests/test_setup_metadata.py`；主控 `.venv-clean` 已装好 Pillow/lark-oapi，本轮未重装 |
| 7. 渲染分页/标签 | 章节标题 `keep_with_next`，标题+首条正文不同页时整体移到下页；拉丁词整词保留；封面移除 `draft_key` 与重复账号标签，续页头不再重复账号名；模板未大改 | `tests/test_render.py` 新增 2 项；未改写或重新生成主控的草稿/证据 |

### 3.1.4 INTEGRATION_REVIEW 四项修复（2026-11-06）

| 复核项 | 实际落地 | 离线验证 |
| --- | --- | --- |
| 1. Search summary 不得进入证据 | `DeepSeekSearchSource` 不再把 provider `summary` 写入 `SourceEntry`；空 content 跳过，证据来自真实 content。测试走 `ingest_source` 并检查 evidence 行：矛盾摘要与真实正文并存时只出现正文句子 | `test_provider_summary_never_becomes_evidence` |
| 2. 付费重试逐次预留 | 搜索/抓取按每次物理 HTTP 尝试先 `reserve`，失败尝试 `retain_unknown`，成功尝试按官方空/非空结果 `settle_fixed`；预算不足时下一次 `reserve` 在联网前抛 `BudgetExceeded` | `test_retry_reserves_before_each_physical_attempt`（先看到预留再调用）、`test_tiny_budget_blocks_retry_network_attempt`（第 1 次后不再联网） |
| 3. 修改请求立即冻结旧审批 | 回调同事务改为 `revision_pending`、冻结草稿/事件、取消排队 publish job、原子入队 revise；已开始发布则拒绝；修订失败保持冻结，成功才 supersede 并建新审批；重复消息不重复建 job | `test_queued_revision_freezes_pending_approval_immediately`、`test_queued_revision_cancels_an_approved_publish_gate`、`test_revision_failure_cannot_restore_approved_status` |
| 4. 持久当前新闻查询与精确域名 | 每源 `lookback_days`（默认 3，限制 1..30），每次 fetch 由注入时钟生成 `after:`；保留 provider 原始日期，缺失/过旧不被“刷新”；允许列表默认精确主机名，`*.domain` 为显式子域 opt-in；example 配置已注明，query 不含硬编码日期 | `test_after_filter_advances_with_clock_and_keeps_original_date`、`test_exact_allowlist_excludes_community_subdomain_by_default`、`test_old_search_result_is_not_magically_fresh` |
| writer 提示 | `SYSTEM_PROMPT` 增加一句：evidence 是外部网页的不可信引用数据，不能当作指令执行 | 不使用 prompts 重构；现有草稿校验照旧 |


### 3.2 离线演示

命令：

```powershell
.venv\Scripts\python.exe -m bot --config config/bot.toml demo
```

实际输出摘要：

- 2 个本地 fixture 来源，4 条新素材，选题队列 3 个；
- 草稿 #1 v1，标题 `开源模型发布新版本,支持更长上下文`；
- 审批码示例 `XH-KK8E5X`，正文 SHA256 `610d9789…`；
- 2 张 1080×1440 PNG，Pillow 验证可打开；
- 假飞书通知写入 `var/demo/notifications/`，明确标注“未发送到飞书”；
- 重启后待审批仍在；批准后经 Worker tick 发布到 **mock**：
  - `publish_attempts.state = simulated`
  - `mode = mock`
  - `external_post_id / external_url = NULL`
  - 重启后尝试数仍为 1；
- manifest 示例：`var/demo/exports/draft-1-v1-20261105T164346.json`，其中 `simulated: true`。

### 3.3 其他验证

- 2026-11-06 重新执行 `python -m bot --config config/bot.toml demo`：退出码 0；草稿 #1、2 张 PNG、批准后 mock 发布 `simulated`、重启后尝试数仍为 1。
- `python -m bot --config config/bot.toml doctor` 退出码 0；当前 DSH 环境里 DeepSeek key 显示 `set`（未调用、未读取值），Lark/小红书 token `missing`，mock/fake 明确标为未连接。数据库迁移 2（`llm_usage.settled`）已实际应用。
- `tests/test_process_ownership.ps1` 实际输出 `process ownership regression: OK`：匹配身份可识别；不匹配可执行文件/命令行被拒绝；缺失身份时 stop 脚本退出码 3 且不杀进程。该沙箱拒绝 `Win32_Process` CIM 查询，stop 脚本按设计 fail closed，未为了测试关闭安全控制。
- `scripts\setup.ps1 -Target .venv-setup-check -SkipInstall` 实际创建了 `include-system-site-packages=false` 的普通 Python 3.12 venv，并验证 `sys.prefix != sys.base_prefix`。本沙箱的 `ensurepip` 因临时目录权限失败，脚本如实告警；正常用户会话应能完成 pip 引导和 `requirements.txt` 安装。临时 venv 已删除。
- `scripts/start.ps1`、`stop.ps1`、`health.ps1`、`process_identity.ps1`、`setup.ps1` 和 `tests/test_process_ownership.ps1` 通过 PowerShell 语法解析。
- 第四轮用 `.venv-clean\Scripts\python.exe` 运行全套 104 个测试，`Ran 104 tests in 136.790s, OK`，退出码 0；未重装依赖、未重新执行真实付费调用。
- `python -m bot --config config/bot.toml gateway-state` 实际输出 `required=false, configured=false`（offline demo 的 notify=fake）；未启动任何外部连接。修改后的 `start.ps1`/`stop.ps1`/`health.ps1` 通过 PowerShell 解析。
- 渲染实际复核：用主控已保存的 `var/live-verification/draft.json`（只读）在 `var/tests/render-check` 临时重渲染 2 张卡片，确认「限制与不确定性」标题与首条正文同页、`Fetch`/`RAG`/`lang:` 不再被拆词、封面只出现一次账号标签、正文图片无 `draft_key`。临时输出已删除，`var/live-verification` 证据未被覆盖；主控仍可自行视觉验收。
- 本轮没有重新运行真实 DeepSeek Search/Fetch、飞书长连接或小红书桥；这些外部验收留给主控。
- `scripts/backup.ps1` 已对真实 SQLite 执行备份 + `PRAGMA integrity_check=ok`（首批验证保留）。
- `scripts/restore.ps1` 已在隔离数据库上验证：恢复前自动停服务、保留当前库副本、恢复后完整性检查通过（首批验证保留）。
- `config/bot.example.toml` 的 `configure` 骨架已在 `var/test-generated.toml` 重新加载验证（live、DeepSeek、非 demo），未写入任何密钥（首批验证保留）。

## 4. 外部待配置项与阻塞

| 项目 | 当前状态 | 需要你做什么 |
| --- | --- | --- |
| DeepSeek API | 代码完成；本会话未做真实付费调用 | 本会话按约束没有运行付费模型请求；代码默认 `deepseek-flash`、官方 base URL，并默认显式关闭 thinking。主控已在外部环境生成过真实草稿，但本会话没有复验。请自行核对 `DEEPSEEK_API_KEY`、额度和单价。 |
| DeepSeek Search/Fetch 来源 | 代码完成；付费调用未执行 | 需主控用现有 key 做一次限量、固定预算的真实 search/fetch，确认官方响应与 `.00075 USD` 结算。离线测试已覆盖空结果 0 结算、模糊错误保留预留、预算失败不联网、URL 白名单/私网/凭据拒绝；`allowed_domains` 由配置者填写，不预置为默认可信源。 |
| 直接 RSS | 代码完成，外部未验证 | 本机 DNS 把 `openai.com`、`huggingface.co`、`blog.google` 等解析到 `198.18.0.x` 基准网段，SSRF 防护会正确拦截；curl TLS 报 `SEC_E_NO_CREDENTIALS`。请在正常网络环境先 `doctor`，再单独验证 feed URL、条款和发布时间字段。 |
| 飞书 App / 长连接网关 | 未配置、未真实连接；`.venv-clean` 已由主控验证 `lark-oapi` 可导入 | 创建企业自建应用，开启机器人/长连接；配置 `LARK_APP_ID`、`LARK_APP_SECRET`、`LARK_OWNER_OPEN_ID`。配置齐全后 `start-bot.cmd` 默认会启动第三个 `gateway` 隐藏进程，`stop/health` 用同一身份核对管理；未配置时不会拉起。首次只做本人私聊文本审批彩排，不要直接开真实发布。本会话没有启动真实长连接，也没有发送消息。 |
| 小红书桥 + 账号身份 | 未配置、未登录；真实发布保持阻塞 | 需自行启动并登录 `xpzouying/xiaohongshu-mcp` 类本机桥（`127.0.0.1:18060`），配置 `XHS_MCP_AUTH_TOKEN` 或 `XHS_AUTH_TOKEN`；再按 `/api/v1/user/me` 的 `data.data.userBasicInfo.redId` 配置 `publisher.xiaohongshu.expected_red_id` 或 `XHS_EXPECTED_RED_ID`（不要写 `xhs-main` 本地别名，也不要填猜测的 `user_id`）。发布前会同时检查登录状态和该 `redId`，缺失/不匹配/形状未文档化都在 POST 前拒绝。验证码、登录过期、风控由你处理；它不是官方 API。文档中的发布回执没有 note id/URL，因此仍保持 `publish_unknown`，需人工对账。 |
| 真实发布开关 | 关闭 | 不要为了“先跑起来”打开。需要 live + `publisher.provider=xiaohongshu` + `publisher.live_enabled=true` + `BOT_ALLOW_REAL_PUBLISH=1` + 非 demo 管线 + 对某一篇最终作品的明确批准，全部同时满足才可能发布。 |
| 后台隐藏启动脚本 | 身份核对已实现；未完成真实 bot/gateway 的隐藏启动端到端验证 | `launch.py` 写 PID + 身份 JSON；`start/stop/health` 在动作前核对可执行文件、命令行、项目根目录和配置路径；`gateway` 与 worker/console 共用同一套检查。本次沙箱允许启动 dummy 子进程，但拒绝 `Win32_Process` CIM 查询，因此 `stop.ps1` 在无法核实时按设计 fail closed（退出码 3，不杀进程）。普通 Windows 会话可用 `start-bot.cmd`；若受限，先在前台分别运行 console、worker、gateway。本会话没有真实启动网关。 |
| lark-oapi / Pillow 依赖 | 主控已在 `.venv-clean` 安装并验证 Pillow、lark-oapi 可导入；本会话未重装 | 后续新环境运行 `setup-bot.cmd`（或 `scripts\setup.ps1`）创建**普通** Python 3.12 venv；安装流程使用项目内 TEMP/TMP 和 `var\pip-cache`、`--no-cache-dir`、有界 `--timeout/--retries`，并删除了强制 pip 自升级。不要依赖 `--system-site-packages` 的 DSH `.venv`；正常用户会话仍应验证一次全新安装。 |
| 评论/私信读取 | 未实现 | 需求允许以后做只读/草稿；本次没有接入平台评论 API，也没有任何自动评论、私信或删除行为。 |
| 24h/72h 指标 | 仅本地记录“缺失” | 没有平台指标读取适配器；真实作品不会伪造指标，已显式写 `missing`。 |

## 5. 启动方式

### 5.0 创建本机依赖环境（普通 venv）

```powershell
cd D:\Dev\Projects\bot
.\setup-bot.cmd
# 或：powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1
# 离线只验证 venv：.\setup-bot.cmd -SkipInstall
```

脚本要求 Python 3.12，显式执行 `python -m venv .venv`（不带 `--system-site-packages`），再按 `requirements.txt` 安装 Pillow 与 lark-oapi。安装时 TEMP/TMP 指向项目内 `var\tmp\setup-*`，PIP_CACHE_DIR 指向 `var\pip-cache`，并使用 `--no-cache-dir`、`--timeout 30 --retries 3`；不再强制执行 pip 自升级。`.gitignore` 已排除 `.venv*`、`config/bot.toml`、`config/secrets.toml`、`var/`、缓存和 `.env`。主控的 `.venv-clean` 已安装并验证 Pillow/lark-oapi；本轮测试优先使用它，未重装依赖、未改全局环境。

### 5.1 一键离线演示（推荐先跑）

```powershell
cd D:\Dev\Projects\bot
$env:PYTHONPATH="."
.venv\Scripts\python.exe -m bot --config config/bot.toml demo
```

该命令不连接任何外部账号，只写 `var/demo`。

### 5.2 本地审批控制台 + Worker

前台（最稳妥，适用于任何 Windows 会话）：

```powershell
# 终端 1
.venv\Scripts\python.exe -m bot --config config/bot.toml console

# 终端 2
.venv\Scripts\python.exe -m bot --config config/bot.toml worker
```

控制台：`http://127.0.0.1:8765`。若未设置 `BOT_CONSOLE_TOKEN`，首次启动会在终端打印一次性本地登录口令；建议在 `config/secrets.toml` 或环境变量中固定设置。控制台只监听回环地址。

后台脚本（普通 Windows 桌面会话）：

```powershell
.\start-bot.cmd
.\health-check.cmd
.\stop-bot.cmd
```

当 `notify.provider=lark` 且 `LARK_APP_ID`/`LARK_APP_SECRET`/`LARK_OWNER_OPEN_ID` 都已配置时，`start` 还会启动第三个隐藏 `gateway` 进程（飞书长连接）。`stop`/`health` 同时管理 worker、console、gateway；不需要另开一个终端长期运行 `python -m bot lark`。若只想启动网关：`powershell -File scripts\start.ps1 -GatewayOnly`。本会话没有实际启动外部连接。

备份与恢复：

```powershell
.\backup-bot.cmd
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\restore.ps1 `
  -Backup var\backups\bot-YYYYMMDD-HHMMSS.sqlite3 -Config config/bot.toml -Force
```

恢复会先停服务、校验备份、保留当前库副本；不要用它覆盖正在发布中的数据库。

### 5.3 真实模式前的顺序

1. 正常网络下运行 `doctor`，确认 RSS、DeepSeek、Lark SDK/凭据、小红书桥登录状态。
2. 用 live 配置但保持 `publisher.live_enabled=false`，先让真实 RSS + DeepSeek 生成 1 篇**未发布**草稿。
3. 在本地控制台或飞书本人私聊完成一次审批彩排；确认审批通知里的正文/图片与预览一致。
4. 配置小红书桥并确认账号 `enabled=true / status=verified`；另行配置 `publisher.xiaohongshu.expected_red_id`（或 `XHS_EXPECTED_RED_ID`）为 `/api/v1/user/me` 的 `data.data.userBasicInfo.redId`，不要用本地别名，也不要把公开 redId 当作不可变 user_id。
5. 先让 `doctor` 在真实发布关闭时做只读桥接/身份检查；确认飞书通知“正文 + 全部图片已送达”后再从手机批准。不要把 mock 结果当真实结果。
6. 只有当你明确确认某一篇最终作品、且四道真实发布开关同时满足时，才允许真实发布。
7. 首次真实发布后若没有回执，保持 `publish_unknown`，人工核对，不重试。

## 6. 已知限制与后续

- 真实外部链路未验证：RSS、DeepSeek、飞书、小红书桥都受本机网络/凭据限制；不要把适配器代码存在等同于“已连接”。
- 飞书按钮审批未实现/未验证；当前使用文本命令“批准/拒绝/修改 审批码”，这是 RESEARCH.md 中更稳妥的方案。
- 只支持单账号首发；X/知乎/抖音仅保留扩展点，未实现、不得声称支持。
- 评论读取、自动回复、指标抓取属于后续扩展；本次未执行任何平台写操作。
- AIHOT 在线数据默认没有作为可转载素材库；真实来源请优先使用官方 RSS 并核对条款。
- `start-bot.cmd` 的隐藏后台启动在本会话沙箱中无法完成端到端验证，原因见上表；请首次在普通 Windows 会话检查 `var\logs\worker.err.log`、`var\run\*.pid` 和 `health-check.cmd`。
- 发布配额/内容复检保证的是**同一个 SQLite 数据库内的串行化**，不是跨机器、跨数据库或平台侧的分布式 exactly-once；网络超时仍可能产生 `publish_unknown`，必须人工对账。
- 预算预留使用保守的 token 估算，**不是硬性货币上限**；DeepSeek 实际计费、缓存折扣、汇率/价格调整和网络重试都可能让账单与本地账本有偏差。未知用量会保留预留而不是记 0。
- DeepSeek Search/Fetch 的固定 0.00075 USD 来自官方文档，真实账单仍以服务商为准；本会话只做了离线响应测试，主控需另做一次限量付费验收。`allowed_domains` 不是平台级可信源认证，只是本项目的来源域名允许列表；默认精确主机名，显式 `*.domain` 才会放行子域。
- 小红书 `/api/v1/user/me` 只按已验证的 `data.data.userBasicInfo.redId` 匹配；这是公开账号号，不是不可变内部 ID。需要不可变身份时，应等桥接适配器提供可验证字段，当前会安全阻塞。`POST /api/v1/publish` 的文档回执没有 note id/URL，发布后仍可能进入 `publish_unknown`，必须人工对账。
- 飞书通知部分失败后的重试会重新发送通知正文；已接受的审批决定不会被重复执行，控制台已批准后也不会再重发通知。修改回调已改为异步 job，但真实飞书长连接、3 秒时限和网关隐藏启动仍未在本会话做端到端验收。
- 主控 `.venv-clean` 已安装 Pillow/lark-oapi；本会话未重装也未做全新环境安装验证。`setup.ps1` 的项目内 TEMP/cache 和 `--no-cache-dir` 仍需主控在干净环境复核。
- 渲染修正只改分页/换行/标签，不改写正文；主控保存的 validated draft 需重新渲染后才能做视觉验收。本会话没有覆盖 `var/live-verification` 的任何证据。

## 7. 关键文件

| 路径 | 作用 |
| --- | --- |
| `bot/worker.py` | 采集、研究、起草、通知、发布 job 循环 |
| `bot/console.py` | 本地审批控制台（登录/CSRF/只读 GET） |
| `bot/lark_gateway.py` | 飞书长连接与事件解析 |
| `bot/services/approvals.py` | 审批指纹、owner/p2p、命令处理、过期 |
| `bot/services/publish.py` | 发布前检查、事务抢占、unknown/恢复/reconcile |
| `bot/services/drafts.py` | 选题→草稿→渲染→待审批 |
| `bot/render.py` | 本地字体 PNG 卡片 |
| `bot/adapters/sources.py` / `llm.py` / `notify.py` / `publishers.py` | RSS、DeepSeek 写稿/Search-Fetch、飞书、mock/小红书适配器；thinking/重定向/错误脱敏、Search 预算与来源白名单、XHS redId 身份/登录检查与未知回执处理 |
| `bot/budget.py` / `bot/schema.py` | 预算原子预留/结算/未知预留/固定费用结算；迁移 2 `llm_usage.settled` |
| `bot/services/notifications.py` / `bot/services/approvals.py` / `bot/worker.py` | 持久审批/结果通知；回调异步 `revise` 入队与 worker 执行；owner 私聊结果回执 |
| `scripts/start.ps1` / `stop.ps1` / `health.ps1` / `launch.py` | worker/console/gateway 三进程隐藏启动、同一 PID + 身份核对；`gateway-state` 只读计划 |
| `requirements.txt` / `setup-bot.cmd` / `scripts/setup.ps1` | 固定依赖；普通 Python 3.12 venv；项目内 TEMP/PIP cache、`--no-cache-dir`、有界网络等待，不再强制 pip 自升级 |
| `scripts/launch.py` / `process_identity.ps1` / `start.ps1` / `stop.ps1` / `health.ps1` | 后台进程启动、PID + 身份文件、可执行文件/命令行/项目路径核对，CIM 不可用时 fail closed |
| `config/bot.toml` | 离线演示配置（本轮未修改） |
| `config/bot.example.toml` | live 示例：已加入 `expected_red_id`、`thinking_disabled` 和可选的 `deepseek_search` 来源骨架（默认 disabled） |
| `docs/IMPLEMENTATION_BRIEF.md` / `docs/RESEARCH.md` / `docs/REVIEW_NOTES.md` | 原始需求、调研、复核记录（未改动） |
| `docs/IMPLEMENTATION_STATUS.md` | 本文件 |

## 8. 结论

P0 已完成并通过端到端离线验证；第三轮七项接入修正和第四轮四项具体修复已完成针对性代码，离线全套 104 个测试和进程所有权回归脚本通过。P1/P2 的真实 RSS、DeepSeek、飞书、小红书桥仍未在本会话获得可用网络/凭据或未完成真实启动/安装验证，不能报告为“已连接”。主控另做联网、付费和视觉验收。在配置好外部账号、按 `data.data.userBasicInfo.redId` 核对身份、并完成一次“正文 + 全部图片已送达”的本人私聊审批彩排之前，不应开启真实发布。真实发布仍可能产生 `publish_unknown`，本地事务不构成分布式 exactly-once，预算估算也不构成硬性货币上限。


