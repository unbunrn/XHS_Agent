# S1 收尾 + S2/S3 离线交付报告

日期：2026-11-11（Asia/Shanghai）
状态：代码与离线测试完成；**已于 2026-11-11 17:46 部署重启 console/worker/gateway，18:00 加载标题自动收短，18:18 启用批准后立即发布并把 live 选题上限调到 10，18:23 重启 worker**。飞书长连接已建立；首次立即发布 `XH-MUEAA5` 因桥接 HTTP 500 进入 `publish_unknown`，未重发，必须在平台人工确认。真实外部行为仍需验证。

## 2026-11-12 增量：AIHot 发现层

- 新增 `bot/services/aihot.py`：读取 `https://aihot.news/feed.xml`（`/rss.xml` 的 301 目标），解析标题、日期、摘要和原文链接；只对配置的 feed 主机放行（本机解析到非公网地址），从最终 feed 地址直接读取，不放开普通 SSRF/重定向。
- `topics.process_topic_request` 在规划前做 AIHot 匹配：显式条目 URL 优先；自动匹配仅用于 `news`；命中后把 `direct_url` 改为原文，原文未取到才使用带 `据 AIHot 汇总（原文未取到）：` 前缀的摘要。摘要由 `_apply_aihot_summary_verdict()` 强制为 `secondary/usable`，永不 `source_verified`；审批卡标“AIHot 聚合摘要/原文未取到（非官方）”。
- 直接提供的原文正文证据由 2 条扩展到最多 6 条，避免 writer 因摘录过少误判“证据不足”；原文身份仍由统一来源身份判定，不因 AIHot 背书升级。
- 配置：`bot/config.py` 默认关闭 `aihot_enabled`/`aihot_auto_match`（离线测试不访问网络），`config/bot.live.toml` 开启，`config/bot.example.toml` 提供注释示例。
- 验证：`tests/test_aihot.py` 7 项；`test_aihot + S2 + S3 + remote_topics` 70 项；`test_aihot + S1 + S1-review + S2 + S3 + approval_publish` 80 项通过；全量离线套件 `Ran 295 tests in 708.289s, OK`（日志 `var/tests/full_suite_aihot.log`）。真实 feed 拉取成功；链路测试 `XT-EFKZF9` 生成草稿 13/审批 `XH-DXQC9Z`，未自动发布；该测试审批已 `rejected`。
- 已知限制：AIHot 摘要不是原文；词面匹配失败会退回普通搜索；没有 `热点` 列表命令；AIHot 只解决发现，不改变审批/发布闸门。

## 2026-11-12 增量：P0 内容 Agent（实现完成，待全量）

- 新增 `bot/services/critic.py`：结构化 `pass/revise/reject` 审稿，问题带 evidence IDs；最多 `agent.max_critic_revisions` 轮重写，拒绝/超轮不生成 draft。审稿不可用时只记审计并继续人工审批，畸形势必 revise/fail。
- 新增 `bot/services/adaptive_research.py` + `research_orchestrator.run_followups()`：首轮后检查官方源/独立来源/关键数字/反方覆盖，最多补 2 个查询；补检共享同一物理抓取配额，仍走正文、时效、来源身份和预算闸门，停止原因写入 `diagnostics.adaptive`。
- 新增写稿失败自动补救 `_rescue_topic_evidence`：writer 因“证据不足/缺少某类事实”拒绝时，从错误中提取缺口，做一次有界补检（`topics.auto_rescue=true`，最多再抓 `rescue_max_fetch=2`），只把 verified/usable 证据写回同一事件，再重试一次 writer；第二次失败即停止。审计 `topic.writer_rescue_started/completed/failed`，不创建新选题、不重发旧审批。
- 新增 `bot/services/agent_eval.py` 与 `scripts/agent_eval.py`：replay JSONL 指标层，输出 first-pass、official recall、unsupported claim rate、human edit rate、queries/cost/latency；样例在 `docs/examples/agent_eval_records.jsonl`，明确不是模型质量结论。
- 配置：live `agent.critic_enabled=false`（owner 21:15 关闭；代码/测试保留）、`max_critic_revisions=1`、`topics.adaptive_research=true`、`topics.auto_rescue=true`；示例配置默认关闭审稿/自适应/补救。
- 测试：`test_critic_agent` 6 项、`test_adaptive_research` 6 项、`test_failure_rescue` 5 项、`test_agent_eval` 3 项；rescue 追加后 77 项定向通过（97.366s）；全量 `Ran 322 tests in 778.082s, OK`（日志 `var/tests/full_suite_rescue.log`）。worker 已于 21:40 重启加载 rescue（PID 19632）；live 审稿按 owner 要求关闭，自适应补检/自动补救保留；未做真实 20-topic 评测，未自动发布。

## 交付范围

本轮按 `docs/DSH_RESEARCH_S1_S3_TASK.md` 连续完成：

- S1 收尾：诊断 URL 默认脱敏、旧直达链接回填防护、全结果 TTL 缓存、共享抓取上限。
- S2：一次结构化研究计划、最多 3 条 query、一次批量来源身份判定、程序化链接/主张校验、身份缓存。
- S3：`news`/`analysis` 模式、模式持久化与发布闸门、owner 私聊运维命令、失败收据与可执行建议。
- 迁移 8 的合成 v7 数据库演练；未接触生产数据库。

## 已完成内容

### S1 收尾

- `bot/services/research_diagnostics.py` 的查询参数改为**默认拒绝**：除极少安全键外，所有 query 值都替换为 `[redacted]`；覆盖 `X-Amz-Signature`、`X-Amz-Credential`、嵌套 `X-Amz-Security-Token` 等签名/嵌套参数，同时保留不可逆 URL 指纹用于诊断关联。
- `bot/services/topic_research.py` 在后续搜索结果把已知直达 URL 判为过期时清空延迟直达条目，最终回填不能重新加入；最终入库前仍经过正文、日期和身份校验。
- 全结果缓存（`research_cache`）缓存的是完整条目和计数，不只正文；key 包含主题、query、模式、发现日期策略、可信/验证域名、时效、直达 URL、结果数/正文阈值/抓取上限。显式刷新绕过；恢复后仍按当前可信范围和时效重新校验。
- 所有计划 query、直达链接、来源身份核验涉及的抓取/重试共享 `topics.research_max_fetch_attempts`（默认 6）；没有按轮次重置。

### S2 计划与来源身份

- `bot/services/research_plan.py`：一次 `purpose="research_plan"` 调用，输出实体、版本、主张、来源线索和 ≤3 条 query；版本分别保留，不做全局词面去重；解析/校验失败使用有界回退，不递归重试。
- `bot/services/source_identity.py`：一次批量 `purpose="source_identity"` 判定；输入是实际抓取的 URL、标题、正文片段、正文长度和可信页面真实链接关系。程序拒绝不存在的 evidence ID、伪造 URL、自述 official、托管平台冒充组织、无正文支持的主张；共享托管站按真实链接的 `owner/repo`（含子页）或 exact resource 限定作用域，同 host 其他组织、编码路径和前缀碰撞不能继承授权；旧版本官方页不会被当成新版本证据。
- 角色 `official/vendor_primary/secondary/unknown` 和 `source_verified` 写入 evidence 并传给 writer；身份核验与正文支持分开。
- 身份缓存 key 由实际材料指纹 + 策略（主题、模式、可信域名、阈值、链接作用域策略版本）组成；缓存命中仍按当前策略重算 `verified` 并重新检查链接作用域，不会升级信任；内容/配置/模式变化会 miss。
- 跨语言补充：计划实体+版本可触发有界 alias 匹配，避免中文选题遇到英文正文时过早按中文 facet 拒绝；仍要求真实正文，旧版本、仅标题/summary、厂商单独出现都不会通过。

### S3 模式与飞书运维

- `topic_requests.research_mode` / `drafts.research_mode` 持久化；默认 `news`，显式 `分析`/`切换分析` 进入 `analysis`。
- `news` 继续用 `limits.max_age_hours`（默认 72h）；`analysis` 使用 `topics.analysis_max_age_hours`（默认 5 年）并保留原始日期。未知日期保持未知，不写成“今天”。
- 草稿写入前使用关联请求的模式；发布闸门再次检查 draft/request 模式一致，并重新检查新闻时效；analysis 允许旧材料但不能绕过审批、账号、证据和配额闸门。
- owner 私聊命令：`分析 ...`、`分析 <XT>`、`刷新 [XT]`、`补充链接 [XT] <公开链接>`、`失败原因 [XT]`、`切换分析 [XT]`。非 owner、群聊、重复 message_id、终态请求不会创建新检索/计费调用。
- 刷新/补链接/切模式在同一事务中更新请求、作废旧审批与旧草稿、取消旧通知并写入一个 `topic` 控制任务；旧审批绑定失效。
- 失败原因输出真实进度/错误和最多两条可执行命令；正文已取得但没有可用来源时明确写“没有可写入正文的可靠来源证据”，不写成官方证据。预算不足保持 `deferred`，不伪装成“没有新闻”。

## 独立验收 R1–R4 返修

针对 `S1-S3-independent-review-20261111.md` 的四项问题，本轮只做返修和回归，不增加功能：

- **R1：新来源正文进入身份核验。** `ResearchOutcome.candidates` / `PlanRun.candidates` 单独保存“已取得正文、尚未核验”的材料；计划不再因已有可信正文而提前结束；`process_topic_request` 将可信条目与候选正文合并后调用一次 `verify_source_material`，只把 `verified`（官方/一手）或 `usable`（明确标注为 secondary 的第三方报道）的条目写入 evidence。证据角色写入移到 `refresh_targeted_evidence()` 重建之后，避免重建时丢失。新增从 `process_topic_request` 开始的“新域名 + 可信官网链接 → 审批稿”“无真实关系 → 无草稿/无证据”和“未列入白名单的媒体正文 → 标注 secondary 的审批稿”端到端测试。
- **R2：链接关系不再是域名文字出现。** 从可信正文中提取真实 `http(s)` URL，只接受 exact hostname；检查链接前后各 180 字符的上下文，拒绝 beware/impersonation/假冒/非官方 等警告或否定，纯文字域名、子串碰撞、相似主机名和第三方链接不能证明归属。E 编号按完整材料稳定分配；身份缓存命中仍重新执行链接/主张/信任校验。
- **R3：预算和模型异常不再变成核验通过。** `BudgetExceeded` 在计划、身份核验和编排层单独透传；编排状态按结构化状态置为 `deferred`，不再根据英文大小写字符串猜测。身份模型/预算失败返回未核验结果或 deferred，已配置可信域名只保留诊断角色，`verified=False`，不新增 verified evidence。入口处规划预算也返回 deferred，不再无提示继续计费。
- **R4：analysis 有独立发现日期策略。** `deepseek_search` 增加 `recent|any` 发现策略：`news` 继续带近期 `after:`，`analysis` 可无近期过滤地发现旧文；最终 evidence 仍按 analysis 五年/news 72 小时时效校验。策略写入诊断和全结果缓存 key；网页/模型不能自行改日期策略。假 provider 记录真实 `_post_once` payload，覆盖 analysis 旧文发现、news 保留过滤和 adapter 不重加日期。

## 独立复验：共享托管来源作用域返修

针对 `S1-S3-rereview-20261111.md` 的剩余一项（共享托管站机构/路径作用域），本轮只修这一项及其缓存/端到端测试，不扩展新功能：

- **不再以整站为授权单位。** `source_identity._link_grant()` 从可信正文提取真实 `http(s)` URL，先做 exact host、警告/否定上下文检查，再按目标站点计算授权作用域：
  - `github.com`/`bitbucket.org`/`gitee.com` 等明确 owner/repo 共享托管：只授权链接目标的前两段 `owner/repo`（含该 repo 子页），`other-owner/fable`、`real-vendor-evil/fable`、`real-vendor/fable-evil` 都不会继承 `real-vendor/fable` 的授权；
  - 其他站点：只授权规范化后的 exact resource（host+path+query），不做 host 级授权；
  - 路径先 percent-decode 并拒绝编码分隔符/点段，再做 `posixpath.normpath`，避免 `%2e%2e`、重复斜杠或前缀碰撞扩大作用域。
- **缓存与首判同一套作用域。** `SourceVerdict.scope` 记录 `github.com/owner/repo` 或 `exact:host/path`；缓存命中时用当前可信材料重新执行 `_link_grant()`，旧 host 级/错误 owner 缓存不能复活。`link_scope_policy=2`、共享托管列表等进入 material/policy fingerprint，策略升级使旧缓存自然失效。
- **关系材料只收真正有关联的页面。** 可信页面若自身未命中主题，只有在正文包含真实 URL 指向本批已抓取的非可信候选时才作为 identity 关系材料发送；无关的同域名文章（例如 Anthropic pricing 与 Claude memory 主题）不会进入 identity，更不能变成 evidence。发现阶段只对“可信域名且结果文本带 URL”的候选跳过早期词面拦截，仍在同一 6 次物理抓取配额内。
- **测试。** 新增 `tests/test_source_scope.py` 9 项：repo/子页通过、同 host 其他 owner 拒绝、前缀/编码路径拒绝、非共享站 exact resource、缓存复验、策略版本使旧 fingerprint 失效，以及从 `process_topic_request` 开始的正确 repo 到草稿、错误 owner 无草稿/无 verified evidence 两条端到端。另恢复同厂商无关文章、无关旧结果等既有回归。

## 第三方来源可用性返修（2026-11-11）

生产反馈 `XT-QNJRPM` 取得 6 篇真实媒体正文后仍被阻断：模型把 ifanr、搜狐、驱动之家、DigitalToday 等判为 `secondary`，而入口只接受 `verified`（官方/一手）。这不是“没有正文”，而是信任策略过度依赖预配置可信域名。

本轮按用户确认的策略调整，不扩大为“无来源核验”：

- **仍从实际正文判定，不靠域名白名单自证。** 官方/一手仍需模型声明 + 真实正文支持；secondary 也必须有实际正文支持的主张和置信度 ≥ `topics.identity_min_confidence`（默认 0.6），且不得被模型标为无支持。
- **secondary 可以被写入草稿，但明确标注为第三方。** `SourceVerdict.usable` 与 `verified` 分离；`source_verified` 对 secondary 保持 `False`，`source_role=secondary`，不会改写成官方。草稿 writer 提示词强制“据/媒体称/报道”，禁止“官方宣布/确认”；审批卡片来源列表追加 `非官方媒体/转述` 标签。
- **官方/一手仍更严格。** 无正文支持的 official 会被降级为 unknown，不能创建草稿；共享托管链接仍按 owner/repo 或 exact resource 限定作用域。
- **缓存同一策略。** 身份缓存命中时重新计算 `usable/verified`；secondary 缓存可复用，unknown/无支持仍然 miss。
- **测试。** 新增 `tests/test_secondary_sources.py` 端到端：未列入可信域名的媒体正文生成 1 篇草稿，所有证据为 `secondary`、`source_verified=0`，审批来源含“非官方媒体/转述”且不含“官方/一手”。`tests/test_research_s2.py` 增加 secondary 可用与缓存后不再调用模型两项测试；S1 review 中旧的“身份待核验”文案断言更新为“没有可写入正文的可靠来源证据”。安全、审批绑定、发布闸门和来源作用域测试未放宽。

已知边界：secondary 仍只是第三方报道，不构成法律或机构归属证明；正式发布前仍应由 owner 在审批卡片上核对来源标签、正文和图片。真实 DeepSeek 判定质量与真实媒体样本未验证；本策略已随 2026-11-11 17:46 重启加载，但尚未经过真实飞书审批样本验证。

## 明确未完成 / 限制

### 外部待配置/待验证

- DeepSeek：未读取 `DEEPSEEK_API_KEY`/secrets，未发起真实 plan/identity/检索调用；离线 FakeLLM 不代表真实模型质量。
- 飞书：部署时 gateway 使用已配置的 app/owner 建立了长连接，未人工读取密钥值，未发送真实 p2p 消息；真实回调/消息重试和 owner 私聊测试仍未验证。
- 小红书：未读取 token/bridge，未发布；真实发布闸门和平台回执仍由既有 P0 路径负责。
- 可信来源：未修改生产 `deepseek_search` 来源或 `allowed_domains`；没有为通过测试扩大可信域名。

### 代码/验收缺口

- **部署已完成（2026-11-11 17:46 +08）**：重启 console/worker/gateway，worker 进入 live tick，gateway 已连接飞书长连接；部署前已备份 live DB 并验证 `integrity_check=ok`。没有向真实飞书发送消息/审批，没有调用真实 DeepSeek 付费接口，没有发布。
- 飞书命令的本地入库/去重/事务测试通过，但真实飞书回调、真实 p2p 账号、消息 API 重试未验证。
- 跨语言相关性仍是“计划 alias + 真实正文”的有界回退，不是完整语义相关性模型；没有额外模型调用。仍可能把某些同事件英文文章判为 blocked，阻断原因会明确，不伪造通过。
- 来源身份返修仍是“实际正文 + 可信页面真实链接 + exact host/共享托管 owner-repo 或 exact resource/上下文/主张”的程序边界，不构成法律或完整机构/路径归属证明；repo 内被接管、页面后改链接、图片/脚本/跨页导航中的官方关系等边界未验证，仍可能被判未核验或产生有限误判。缓存命中会重验，但真实 provider 的边界未验证。
- `analysis` 不使用近期发现过滤，旧文召回范围更大，但仍受最终五年时效、正文和身份校验约束；没有对真实搜索索引的召回率做验证。
- 官方文章召回仍取决于计划 query 和搜索结果排序：`XT-QNJRPM` 中 Anthropic 官网实际可达（`https://www.anthropic.com/claude-fable-and-mythos-5-2`，HEAD 200），但三条泛中文 query 每个只取前 5 条，未生成 `site:anthropic.com`/精确官方标题 query，官方文章没有进入候选集。这不是域名不可访问；本轮未改计划 query 规则。
- S1 全结果缓存的 TTL 命中/过期、显式刷新绕过、配置/模式失效均已由离线测试覆盖；真实 provider 的缓存收益和长尾边界未验证。
- `bot/services/topics.py` 仍有 8 处非阻塞 `SyntaxWarning`（非 raw 字符串的 `\s`/`\.`），行为已由测试覆盖；未在本轮重写这些历史正则。
- 真实账号配置（DeepSeek key、飞书 app/owner、XHS bridge/token、可信来源域名）未检查、未读取、未修改。

## 主要变更文件

- `bot/services/research_plan.py`（新增）
- `bot/services/source_identity.py`（新增）
- `bot/services/research_orchestrator.py`（新增）
- `bot/services/topics.py`（恢复并接入计划/身份/模式/运维命令；从 `.pyc` 反编译恢复后逐项修复）
- `bot/services/topic_research.py`（全结果缓存、直达回填防护、共享配额、候选正文单独传递）
- `bot/services/research_orchestrator.py`（计划 query 编排、候选聚合、结构化 deferred）
- `bot/adapters/sources.py`（`recent|any` 发现日期策略）
- `bot/services/research_diagnostics.py`（默认拒绝查询脱敏）
- `bot/services/research.py`（evidence 角色/验证传播）
- `bot/services/drafts.py`、`bot/services/publish.py`（模式持久化与发布闸门）
- `bot/adapters/llm.py`（plan/source_identity 提示词与 FakeLLM 分支）
- `bot/schema.py`（迁移 8）、`bot/config.py`、`config/bot.example.toml`
- `tests/test_research_s2.py`、`tests/test_research_r1_r4.py`、`tests/test_source_scope.py`、`tests/test_secondary_sources.py`、`tests/test_s3_modes.py`、`tests/test_migration_s8.py`、`tests/test_research_pipeline_s1_review.py`、`tests/test_source_review.py`、`tests/test_remote_topics.py`
- `docs/FEISHU_REMOTE_TOPICS.md`、`docs/README.md`

## 测试与结果

环境：`.venv/Scripts/python.exe`，`PYTHONPATH=. PYTHONIOENCODING=utf-8`；全部临时数据库、FakeLLM/FakeNotifier/本地 fixture，无真实网络。

已执行：

```text
python -m unittest tests.test_research_pipeline_s1 tests.test_research_pipeline_s1_review tests.test_research_s2 tests.test_research_r1_r4 tests.test_s3_modes tests.test_source_scope tests.test_secondary_sources tests.test_remote_topics tests.test_migration_s8 -q -b
# Ran 102 tests in 290.152s
# OK

python -m unittest tests.test_migration_s8 -q
# Ran 1 test ... OK
```

全量离线套件：

```text
python -u -m unittest discover -s tests -v
# Ran 266 tests in 599.348s
# OK
```

全量日志：`var/tests/full_suite_secondary_verbose.log`（完整 `-v` 输出）。

## 迁移 8 演练

`tests/test_migration_s8.py` 在临时目录构造 v7 SQLite：

- 按 `MIGRATIONS[:7]` 顺序执行并写入版本行；
- 插入旧 `settings` 与 `sources` 行；
- 调用真实 `Database.init()` 两次；
- 验证 `topic_requests` 的 `research_mode/research_plan_json`、`drafts.research_mode`、`evidence.source_role/source_verified`、`research_cache`、`source_identity_cache`；
- 旧行保留、版本 8 只出现一次、`PRAGMA integrity_check = ok`。

未打开、备份、修改或迁移生产数据库。

## 成本与硬上限

- 每个选题 ≤3 条计划 query；物理抓取（含直达、身份相关和重试）≤6。
- 计划模型调用 ≤1；来源身份批量调用 ≤1；预算仍走既有 `BudgetGuard`，超限转 `deferred`。
- 自动选题 1 次/天、手动 5 次/天、每篇独立审批、未知结果不重发、发布结果只信平台回执或账号 feed 的唯一新增同标题笔记。live 当前把 `topics.max_per_day` 调到 10（仍保留审计记录），`limits.max_manual_drafts_per_day` 仍为 5；批准后立即发布只改变排期，不绕过审批/来源/发布闸门。
- 未新增真实平台发布路径；`mock` 仍只能写 `simulated/exported`。

## 部署与回滚（2026-11-11 已执行）

部署在用户明确授权后执行；没有批准任何草稿或发布：

1. 备份 `var/live/bot.sqlite3` 到 `var/live/backups/secondary-policy-20261111-1741.sqlite3`（647168 bytes，`integrity_check=ok`）；确认无 running job、pending approval、publishing attempt。
2. 停止旧 console/worker/gateway，更新代码后启动 schema 8；`scripts/launch.py` 的 `CREATE_NO_WINDOW` 在本机 embedded Python 上触发 `0xC0000142`，已改为 `STARTUPINFO` 隐藏窗口并通过 `scripts/start.ps1` 启动。
3. 当前 console `/healthz`：`ok=true, mode=live, demo=false, publisher=xiaohongshu, pending_approvals=0, publish_unknown=1, real_publish_armed=true`；worker 已 tick，gateway 已连接 `msg-frontier.feishu.cn`。
4. 18:00 重新拉起：`XT-GNAZE6` 的 22 字标题被 20 字平台上限拒绝。`bot/content.py` 增加 `_fit_title()`，超长标题自动收短后继续校验/写稿，不再整篇失败；`tests/test_title_fit.py` 4 项、`test_editorial_autonomy`/`test_render` 共 21 项通过。worker 已用新代码重启。
5. 18:18 立即发布/额度：备份 `var/live/backups/pre-immediate-20261111-181834.sqlite3`；live `limits.publish_immediately=true`、`topics.max_per_day=10`。把已批准的 `XH-MUEAA5` 排期从 20:00 改为当前时间并重算审批指纹/job。worker 发起 attempt #3，桥接返回 HTTP 500，审批/草稿进入 `publish_unknown`；通知 57 明确“不自动重试、勿直接重发”。`tests/test_immediate_publish.py` 2 项，连同审批/标题共 25 项通过；18:23 重启 worker 加载配置。
6. 回滚：停止服务、恢复上述备份和上一版代码；不要用“删除新列/新表”反向迁移。`XH-MUEAA5` 的 unknown 不能靠回滚或重发解决，必须先人工核对平台。

## 手机端 3 条验收命令（已部署，可发送）

服务已部署。以下命令可由 owner 私聊发送；前两条不产生检索/发布，`选题`/`分析` 会产生真实检索，批准后会按既有闸门真实发布：

1. `帮助` → 预期返回选题、分析、刷新、补充链接、失败原因、切换分析及审批命令说明；不产生检索/模型费用。
2. `失败原因 <XT 编号>` 或 `失败原因` → 预期返回真实状态/进度/错误和最多两条可执行建议；不重试、不发布。
3. `补充链接 <XT 编号> <公开可信链接>` → 预期更新请求并在同一事务内作废旧审批/旧草稿、排队一次重检索；不能用于已发布/模拟/发布中请求。

## 结论

S1 收尾、S2、S3 的离线代码、针对性测试和迁移演练已完成；第三方 secondary 来源可在明确标注的前提下进入草稿，但不会被写成官方来源。服务已于 2026-11-11 17:46 部署并重启，飞书长连接已建立；真实飞书投递/审批已发生，`XH-MUEAA5` 的真实发布结果为 unknown，不能宣称成功，也不能自动/手工重发。跨语言检索质量、官方文章召回和 unknown 的人工对账仍是待验证边界。没有把模拟发布写成真实成功，也没有修改生产凭据或数据。
