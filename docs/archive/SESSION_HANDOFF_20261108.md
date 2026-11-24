# 运营试运行接续记录

本机项目：D:\Dev\Projects\bot。此次不改动交接中心仓库。

## 已验证
- 用户手动下载 v2.7.2 镜像；主控经授权执行会话核对 RepoDigest 与 config/xhs-compose.yml 固定摘要相同，已启动 bot-xhs-bridge。
- 容器 healthy，仅绑定 127.0.0.1:18060；会话挂载项目 var/xhs-container/data，配图 var/drafts 到 /app/images 为只读。
- 用户扫码后，GET login/status 确认登录，user/me 返回实际账号。用户确认该账号并授权持续试运行。账号匹配值已绑定 config/bot.live.toml，不在此重复账号标识。
- DSH 的容器图片映射已复核；主控独立执行 tests.test_container_image_paths、tests.test_approval_publish、tests.test_xhs_identity，36项通过，108.456秒。Docker 内 sha256sum 与宿主验证图片一致。证据 var/xhs-container/mount-check.json。
- 用户授权范围：每小时采集、每天最多一篇图文发到本人飞书审批、每日估算费用上限 USD 0.10。本轮真实发布关闭。
- 已启动 worker、console、gateway（config/bot.live.toml）。进程身份验证均运行；实际飞书 SDK connected 标志已观察到；本机控制台 /healthz HTTP 200。
- 首轮搜索失败原因是 query 超过 DeepSeek API 的128字符限制。已把查询缩短为 AI release 加 OpenAI、Anthropic、Google 三个站点，保留原来源白名单；停止并重启本项目 worker，清除仅该错误的退避，立即重试。
- 重试真实搜索 HTTP 200，入库1条原始内容和1个事件，尚无草稿/审批/发布尝试。运行快照 var/live/trial-status.json。

## 当前运行与边界
- config/bot.live.toml publisher.live_enabled=false，is_live_publish_armed=false。用户批准稿件也不能在本轮自动发布。
- worker日志中的 publisher is not configured / not armed 属于此次主动关闭发布的预期状态。
- 费用是程序按配置估算并限额，并非服务商账单硬上限。没有足够合格来源时允许不出稿。
- 尚未配置 Windows 登录自启动或后台崩溃重启；电脑需保持开机联网、不休眠。Docker 容器已有 unless-stopped，但这不等同于运营程序开机恢复。
- 没有完成真实发布验收、真实运营数据回收、评论/私信、多平台。

## 后续
1. 检查下一轮选题/稿件，不为凑每日数量而编造新闻或降低证据标准。
2. 完成首篇实际新稿的手机审批，并保持发布关闭直到具体作品与真实发布步骤得到授权。
3. 后续补齐开机恢复、进程异常监控；当前不要声称已实现全天无人值守生产运营。
4. 所有密钥仅用本机 config/secrets.toml，不能输出；gateway 原始日志可能含连接 URL，检查只提取连接成功/失败标志，不能回显全文。
5. 当前会话直接调用 Docker CLI 的授权执行可以成功，Python 子进程调用 Docker 仍可能被拒绝。不要因此重启健康的 Docker 或调整系统安全设置。
文档更新时间：2026-11-08T14:21:53.7216772+08:00

## 飞书远程选题开发中（服务暂时停止）
用户新增要求：在飞书发送热点/链接，远程指挥智能体搜索并撰稿。已下达 docs/FEISHU_REMOTE_TOPICS_TASK.md 给 DeepSeek Harness（执行会话80568）。为避免新旧代码混用，worker、gateway、console已按身份核验停止；Docker小红书登录桥保持运行。必须完成实现复核和离线测试，再恢复 config/bot.live.toml 的试运行服务；真实发布仍必须关闭。当前不得声称远程选题已可用。DSH禁止访问真实凭据/运行数据、真实发消息/发帖。需要在接续时检查DSH实际结束状态和文件，不能重复并发修改它正在写的模块。

## 远程选题复核增量
DSH 初版会话80568已exit0；DSH报告新增20项与组合72项离线测试通过，尚不能据此报告生产启用。主控独立发现 shared-brand-only 会误匹配、恢复配额及失败收尾风险，已交 docs/FEISHU_REMOTE_TOPICS_REVIEW.md 给新DSH会话39667继续修正。当前运营三进程仍停止，桥保持运行。
用户选定首次测试话题：Anthropic 发布研究拆解 Claude 智能体记忆机制。主控联网定位官方原文 https://www.anthropic.com/research/components-of-agent-memory ，发布日期2026-11-06，后续真实测试应从该原文获取证据，不伪造日期。
升级前数据库备份：var/live/backups/before-remote-topics-20261108-154812.sqlite3，SQLite integrity_check=ok，schema版本[1,2]。新功能迁移v3尚未用于生产数据库。
主控快速复核确认当前修正代码拒绝无关Anthropic Claude价格文章，提供精确官方链接且正文/日期合格时接受。完整回归与新服务部署尚待完成；新关键词匹配属于保守词法规则，不能声称通用跨语言语义理解已验证。

## 首次真实远程选题验收失败，修复中
用户在飞书发送官方原文链接，实际入站记录topic_queued；任务XT-YZ2HAN(id=1,event_id=2)检索成功，正文20000字符。排队回执和失败回执均sent。写稿失败title must be text，未生成草稿或发布。主控核查证据只有Markdown标题和开头哲学引言，后续记忆机制正文没有进入writer。已交 docs/REMOTE_TOPIC_EVIDENCE_FIX.md 给DSH会话63902做定向证据抽取和结构化模型拒绝提示修复，仅离线。
主控已通过上一版独立56测试（197.153秒），日志var/tests/parent-remote-topics-acceptance.log。生产已迁移v3，数据库完整性ok。DSH复核会话39667在101项针对性检查通过且主控56项通过后被主控中断，避免它自发再跑无必要全量测试；不要报告该会话exit0。
用户回复已发送后复查进程，worker/gateway/console均已stale；worker PID文件由stop脚本核实后移除。日志未见崩溃尾部，原因未确认。桥登录服务未作停止。修复后应恢复全部三进程，不能只重启worker。不要把早先启动成功当作当前持续在线。
修复后应复用同一原始请求并保留失败审计，不要求用户重复发消息；注意旧final通知dedupe键已sent，新的成功结果须明确版本化重试回执或新的dedupe键，不能覆盖已发送记录。当前真实发布仍关闭。

## Continuation verified 2026-11-09T18:26:00+08:00
- Fixed bot/services/drafts.py revision payload: serialize DraftContent with as_dict before model JSON encoding.
- Parent independently ran tests.test_approval_publish: 19 tests passed in 41.608 seconds.
- Resumed existing authorized revision job 9 (no duplicate inbound request). Worker generated draft 2; Feishu notification 10 reported sent successfully.
- New approval XH-XS4RYT pending; old approval XH-92BBQQ superseded. Draft 2 review_pending. Removed incorrect publication-date sentence; source label now official hostname. Footer retains explicit UTC source timestamp.
- Real publishing remains disabled. Console and gateway existing identities verified; worker restarted.
- Topic request original result may still reference initial draft; revision approval is authoritative. General latest-revision status linkage remains to be reviewed.
- User has not yet independently confirmed receipt of this revised version. No real post published in this continuation.

## Editorial autonomy continuation — verified 2026-11-09T19:48:00+08:00
- User requires topic-specific creative decisions, no fixed caption/template/page count; durable profile reflects complementary modalities, not universal image-short/caption-long.
- DSH initial run 10982 intentionally interrupted for user correction; continuation 22249 exited 0 and reported 164 offline tests passed. Parent independently verified 31 editorial+approval tests passed in 66.728s (var/tests/parent-editorial-acceptance.log).
- Read-only live XHS research: two real note details retrieved; first two images of note 6af170cb000000003303a0aa inspected. Diagram/highlight images complement question-led caption; concepts overlap legitimately. No likes/comments/public posts.
- Backup before activation: var/live/backups/before-editorial-live-20261109.sqlite3. Restarted worker/gateway/console with live publish disabled.
- Real job 33 used owner's feedback plus bespoke-content clarification. Generated draft 4, approval XH-G5RGG3, notification 18 sent plus outcome 19 sent. Chosen strategy caption_led with one cover, natural caption instead of four headings. Parent inspected body/storyboard/PNG; no overflow, but visual is simple text and excessive whitespace; mature aesthetic quality NOT established.
- No live publishing. User has not confirmed revised receipt yet.
- Limits: local text/geometric layout vocabulary remains constrained. Revision artifact inference still has keyword heuristics followed by raw instruction model generation and semantic review; not full semantic planning proof. Additional creative flexibility and visual quality remain improvement work. Topic-specific choices demonstrated once live, not universal success.

## Real publish activation 2026-11-09T19:51:55+08:00
User explicitly authorized moving to real Xiaohongshu publishing. Login and expected public redId matched. No queued publish jobs existed. Enabled publisher.live_enabled; launched console/worker/gateway with process-only BOT_ALLOW_REAL_PUBLISH=1 and -AllowRealPublishDaemon. Superseded trial approval 4; created live approval XH-KKF9AS (id 5) for unchanged draft 4 body/images, with renewed schedule/expiry and owner notification. Awaiting explicit owner approval; do NOT infer approval from mode authorization. Backup var/live/backups/before-real-publish-20261109.sqlite3. First real publication still unverified. Environment flag is not globally persisted; future service restarts must intentionally carry the live switch. No autostart configured.

## First publish check 2026-11-09T20:01:27+08:00
Owner approved live code XH-KKF9AS. Job 34 initially blocked before any attempt because sync_sources disabled the registered dynamic topic source on worker restart. Fixed sync_sources to preserve registered topic-source state (exact name+URL and kind), including explicit disables; orphan/unconfigured sources still disabled. Parent ran 20 source-sync+approval tests, all passed (44.110s). Repaired only source 2 and resumed same approved job after verifying no prior attempts and matching artifact hashes.
Attempt 1 made one real publish call, ended unknown on HTTP timeout. Bridge logs explicitly report HTTP500, failure entering body: context canceled. Read-only user/me returned200 with no matching note found. No external ID or URL. DO NOT blind retry; retain unknown pending reconciliation. No successful real publish verified.

## Publish timeout repair 2026-11-09T20:04:01+08:00
Verified pinned upstream publish.go: body humanize.Type failure returns before clickPublishButton. Local bridge matched failure entering body context canceled at 12:00:06Z, HTTP500. App publisher timeout60 was shorter than bridge300s. Changed live publisher timeout to360s, instantiated adapter and asserted timeout360/retries0; restarted worker live. Reconciled attempt1 as confirmed_not_published using this stage-specific evidence (not an empty profile). Preserved failed attempt and original approval history. Created unchanged-content draft5/version5 and fresh real approval XH-ACEPGR; awaiting owner approval. Do not automatically retry. Timeout fix is active; successful actual publication still unverified, other editor issues not ruled out.

## Real publish confirmed 2026-11-09T20:27:18+08:00
- Owner approved XH-ACEPGR; worker made attempt 2. Bridge returned HTTP 2xx but no post id/URL, so the attempt was conservatively kept as `publish_unknown`; no automatic retry and no published claim were made.
- Owner then confirmed the note exists and supplied the real share URL/note id. Manual reconciliation used `confirmed_published`; attempt 2 now has `external_post_id = 6af1b81f0000000036023c27` and canonical `external_url = https://www.xiaohongshu.com/discovery/item/6af1b81f0000000036023c27`.
- Approval XH-ACEPGR, draft 5 and event 2 are now `published`. This approval has exactly 1 publish attempt; no resend occurred.
- Console `/healthz`: `ok=true`, `pending_approvals=0`, `publish_unknown=0`, `real_publish_armed=true`. Live services remain running; no autostart configured.
- Current full offline suite independently rerun: 166 tests, OK, 223.588s. The latest quota-recovery change (only an explicitly reconciled non-publication releases the daily slot) passed; ordinary failures/unknown attempts remain counted.
- Remaining operational gaps: no Windows autostart/crash restart, no comment/DM integration, no platform metrics reader, visual quality is still limited (caption_led single cover, simple text/geometric layout), and the bridge publish response still cannot prove note identity without manual reconciliation.

## Independent manual quota 2026-11-10T14:34:31+08:00
User reports real Xiaohongshu publication tested successfully (user report, not newly verified here). User requests automatic schedule unchanged and Feishu-origin manual publication up to5/day. Implemented separate manual/automatic publish attempt counting by durable topic_requests.event_id relation, surviving draft revisions. Live automatic limit1/day; manual5/day. Manual generation also5/day, revisions excluded from manual topic creation count. Automatic generation ignores manual drafts. Pending approvals3 and budget0.10USD unchanged. Explicitly reconciled non-publication releases slots; unknown remains counted; approvals and one-attempt-per-approval remain enforced. Parent verified22 tests passed39.296s; log var/tests/parent-split-quota.log. Config loaded and counters checked; console/worker/gateway restarted live. No publication initiated by this change.

## Budget increase 2026-11-10T14:36:24+08:00
User requested higher or no daily budget. Set live daily estimated API budget USD5 (was0.10), daily model/search call cap200 (was40), retaining finite runaway protection. Confirmed no running jobs/publishing before restart; loaded BudgetGuard to verify effective values; only old budget/call-limit pause may be cleared if below new caps. Other pauses untouched. Restarted console/worker/gateway live. Publish quotas unchanged: automatic1/manual5, per-post owner approval remains. This is estimated local usage control, not provider billing guarantee.
