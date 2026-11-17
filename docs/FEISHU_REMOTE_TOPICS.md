# 飞书远程选题（v1 / 历史说明）

> 2026-11-16 起：`选题` / `写一篇` 默认路由到 Creator Agent V2（`creator_agent.enabled=true`），不再创建本页描述的旧 `topic_requests`。旧命令表中的 `刷新`、`补充链接`、`失败原因`、`分析` 等仍用于历史请求；新选题请使用 V2 审批卡。`V2 选题` 是等价别名。每日自动发现由 V2 `creator_discovery` 负责，只创建 pending approval，不自动发布。详见 [Creator Agent V2 使用说明](CREATOR_AGENT_V2_USAGE.md)。

本功能让已绑定的 owner 在手机飞书私聊里直接委托选题：机器人先持久化请求并立即回执，再由 worker 在后台使用**已配置的 DeepSeek Search / Fetch** 做定向检索、取证、写稿、渲染 PNG、生成审批单，最后通过现有通知 outbox 发送审批码或明确的未生成原因。

新代码位于 `bot/services/topics.py`，数据库迁移为 `schema.py` 的 v8（v1–v7 保持历史），worker 新增 `topic` 任务类型。没有新增发布路径：本功能从不调用发布器。

## 历史命令（v1，已退役）

> 以下表格只描述旧 `topic_requests` 的历史行为，不再接收新选题；V2 新入口见顶部说明和 [Creator Agent V2 使用说明](CREATOR_AGENT_V2_USAGE.md)。

所有命令只接受**已配置 owner 的飞书私聊（p2p）**。即使 `notify.lark.require_p2p=false`，选题、帮助、状态和格式错误的审批文本仍强制要求 p2p。

| 输入 | 行为 |
| --- | --- |
| `选题 <主题/公开链接/写作要求>` | 创建持久选题请求，立即回执 `XT-XXXXXX`（已排队） |
| `写一篇 <主题/写作要求>` | 同上 |
| 普通文字（没有“选题/写一篇/分析”前缀） | **不创建选题**，回复提示必须加前缀 |
| `选题 <http(s)://...>`（可附写作要求） | 链接只作为检索线索；AIHot 条目会先解析标题/原文 |
| `选题 <AIHot 条目 URL>` | 从 AIHot feed 找到条目，优先改用原文；原文未取到才用带标签摘要 |
| `帮助` / `help` | 返回命令示例、配额、链接与发布限制；不发起检索或模型调用 |
| `状态` / `status` | 返回最近 5 个自己的选题请求状态，以及真实发布是否关闭 |
| `分析 <主题/旧材料/公开链接>` | 创建 `analysis` 模式选题；允许使用较旧材料，原文日期保留，不伪装成今天 |
| `分析 <XT-...>` | 把已有请求切换为 `analysis` 模式；旧审批/草稿作废 |
| `刷新 [XT-...]` | 重新排队检索；省略编号时取自己最近一条请求；终态请求拒绝执行 |
| `补充链接 [XT-...] <公开链接>` | 只追加/更新公开链接并重新排队，不执行用户提供的代码或本地文件 |
| `失败原因 [XT-...]` | 返回当前状态、真实进度/失败原因和最多两条可执行命令，不发起检索 |
| `切换分析 [XT-...]` | 与 `分析 <XT-...>` 相同；需要重新检索/写稿/审批 |
| `批准/拒绝/修改 <XH-...>` | 仍走原有审批/修订路径；live 当前 `limits.publish_immediately=true`，批准后草稿立即到期（不再等晚间槽位），但审批/正文/图片/发布闸门不变 |

### 研究模式（news / analysis）

- 默认是 `news`，要求材料在 `limits.max_age_hours`（live 当前 96 小时）内；超过就拒绝写稿/发布，不因命令或缓存而放宽。
- 只有用户明确发送 `分析 ...` / `切换分析 ...` 才进入 `analysis`。它允许较旧材料（最多 `topics.analysis_max_age_hours`），但会保留原始发布日期；日期未知仍是未知，绝不写成今天。
- 模式写入 `topic_requests.research_mode` 和 `drafts.research_mode`，检索、证据、草稿、审批、发布校验都检查它；草稿/请求模式不一致会被拒绝。
- 模式或补充链接一旦改变正文/证据，旧审批与旧草稿会作废，必须重新生成并重新审批。已处于 `published/simulated/publishing/cancelled` 的请求不能刷新、补链接或切模式。

### 运维命令边界

- 所有选题/运维命令都只接受已配置 owner 的 p2p 私聊；非 owner、群聊、重复 `message_id` 和终态请求不会创建新检索或计费调用。
- `刷新/补充链接/切换分析` 在同一事务里更新请求、作废旧审批/通知，并写入一个 `topic` 控制任务；使用原有单日/待审批配额、共享 6 次物理抓取上限和预算闸门。预算不足转 `deferred`，不伪装成“没有新闻”。
- `失败原因` 只读；若正文已取得但没有可用来源，会明确说“没有可写入正文的可靠来源证据”，并建议补充公开链接或切换分析；若只有可信 secondary，会继续生成草稿但在审批来源标为“非官方媒体/转述”，不会写成官方证据。

### AIHot 新闻发现（2026-11-12）

- live 已开启 `topics.aihot_enabled` / `topics.aihot_auto_match`；`选题 ...` 会先检查 `https://aihot.news/feed.xml`，命中则优先改用原文，取不到原文才使用 `据 AIHot 汇总（原文未取到）：` 摘要。
- AIHot 摘要由 `_apply_aihot_summary_verdict()` 固定为 `secondary` / `usable`，永远不会 `source_verified`；审批卡标“AIHot 聚合摘要/原文未取到（非官方）”。
- 可用 `选题 https://aihot.news/items/<id>` 指定条目；worker 用条目标题作为真正选题，不把 URL 当主题。
- AIHot feed 主机在本机解析到非公网地址，适配器只对配置的 feed 主机放行，仍从 `/feed.xml` 直接读取；普通 SSRF/重定向策略未放宽。
- 测试入口：`tests/test_aihot.py`；2026-11-12 链路测试 `XT-EFKZF9` 生成审批 `XH-DXQC9Z`（未自动发布）。

### 批准后立即发布与未知结果

- live 当前 `limits.publish_immediately=true`：审批通过后草稿排期取当前时间，worker 立即尝试发布；审批仍是必需步骤，来源/正文/图片/发布闸门不因此放宽。
- `XH-MUEAA5` 在 2026-11-11 18:19 的立即发布返回桥接 HTTP 500，状态为 `publish_unknown`，已发通知 57。unknown 不是失败，也不是成功；**不要直接重发或重复批准**。
- 先在已登录的小红书 App/账号核对：若笔记已存在，按已发布处理并人工对账；若确认不存在，再重新生成新草稿、新审批码后批准。桥接没有文档化的“列出本人笔记”只读接口，`reconcile-readonly` 在无 provider 时只会返回 unknown，不能代替人工核对。

示例：

```text
选题 OpenAI 发布 GPT-5，面向独立开发者，语气克制
写一篇 适合新手的中文 AI 工具对比，面向内容创作者
选题 https://example.com/news/openai-gpt5 重点讲 API 价格，面向开发者
```

v1 只处理文本消息；富文本/图片消息不会创建选题。

## 持久化与幂等

- `topic_requests.message_id` 唯一；同一条飞书消息 ID 的并发/重放回调不会产生第二条请求、任务或回执。
- 回执、请求行、`topic` job 在同一个 SQLite 事务中写入；callback 不检索、不调模型、不渲染、不发布。
- 请求状态（`queued/researching/drafting/deferred/completed/blocked/failed`）、检索 query、`event_id`、`evidence_ids_json`、`draft_id`、`approval_id`、审批码和失败原因都落库。
- `drafts.topic_request_id` 带部分唯一索引；崩溃/重试时先复用已关联草稿，再补齐渲染/审批，不会重复调用模型或生成第二篇草稿。
- 恢复已存储草稿时不检查“今日草稿额度”（这篇草稿本身已经占用额度），但仍检查全局暂停、预算暂停和待审批上限；不满足时转 `deferred`，不会绕过这些闸门。
- 如果崩溃发生在渲染成功之后、审批之前，恢复会校验并复用原有 PNG 字节，不重新渲染；只有渲染不完整时才在本地补渲染。
- 如果崩溃发生在草稿插入之后、`drafts.topic_request_id` 提交之前，恢复会先补上唯一关联，再走同一恢复路径。
- worker 继续使用既有 job 租约、启动恢复和重试机制；启动和每轮 tick 会协调“job 已失败但请求仍停在 researching/drafting”的选题，写入可见失败结果，避免永久悬挂。

## 检索与证据边界

- 只在 worker 中使用配置里已有的 `kind="deepseek_search"` 来源；可信域名取这些来源 `allowed_domains` 的并集，不会因为用户发来链接而扩大白名单。
- 提交链接在 callback 中只做静态 URL 校验（不解析 DNS、不发请求）：拒绝私网、loopback、metadata、带用户名密码、`file://` 等非 http(s) 目标。
- 提交域名不在可信范围时，回执会明确说明“改为在已配置可信来源中检索”；链接只进写作说明，不进证据。
- 搜索 query 由确定性关键词生成（最多 8 个关键词；URL 只取 host/path 词），不会把完整用户消息或原始 URL 传给搜索。
- DeepSeek Search 的最终 query 强制 `<= 128` 字符；`news` 保留生成的 ` after:YYYY-MM-DD`，显式 `analysis` 使用 `recent|any` 发现策略，可移除近期日期过滤但不改变最终证据时效。`bot/adapters/sources.py` 在预算预留和网络请求前完成确定性截短。
- 只接受与选题关键词匹配且在对应模式时效内的结果正文：`news` 使用 `limits.max_age_hours`（默认 72 小时），`analysis` 使用 `topics.analysis_max_age_hours`（默认 5 年）；provider 的生成 summary 永远不作为证据；发布时间缺失就保持未知，不伪造日期。
- 定向选题的证据抽取不是“标题 + 开头两句”：它会跳过 Markdown 标题、元数据标签和过短标签，从正文中选最多 6 段与选题词重叠、包含具体细节且分布在不同章节的原文片段。片段保持正文原样并限制长度，不生成摘要；标题、哲学引言或人类大脑类比不能单独构成充分证据。自动采集路径仍保持原有摘句行为。
- 如果正文只有标题/元数据/短标签，或没有任何与选题相关的实质段落，请求会以 `blocked` 结束并给出“检索结果没有形成可用证据”的明确原因，不会把标题硬当证据。
- 关键词匹配区分“品牌/模型名”和“具体主题 facet”：至少两个关键词命中只是下限，还必须命中至少一个具体主题词（版本号、中文短语或后续英文词）。仅有 Anthropic/Claude、OpenAI/GPT 这类共享厂商/模型名的同厂商文章不会被当成选题证据。
- 提交的链接若在已配置可信域名内，且搜索结果包含完全相同的规范 URL，则该原文优先进入证据（排在其它匹配结果之前），但仍必须有真实正文并满足时效；旧文章、空正文不会被接受。
- 提交链接不在可信域名时只作为线索，仍用关键词在配置域名内检索；不会直接抓取该链接，也不会扩大白名单。
- 同一 URL/内容如果已被自动采集过，commissioned event 会复用原 raw item 的引用与来源 URL 作为可追溯证据，而不是留下空证据或复制成“独立佐证”；重复项保留 `duplicate_of`，不会被计为新的独立来源。复用只写入请求自己的 event；已有草稿绑定的 evidence 不会被删除或改写。
- 重试一个尚未生成草稿的请求时，会按新的定向规则重建该请求 event 的 evidence（旧的两句开头摘录会被替换），因此失败请求不必让用户重发。只要该 event 已被任一草稿引用，重建会直接跳过，审批/发布所绑定的证据保持原样。
- DeepSeek 按提示词返回结构化 `{"error": "..."}` 时，适配器会把该响应识别为“模型拒绝生成”，在预算按真实 usage 结算后抛出一次模型输出错误；不会把它当成缺标题的草稿，也不会盲目重试再次产生费用。错误原因会做脱敏、去控制字符并截断。
- 没有匹配证据、结果过旧、未配置可信搜索域名时返回 `blocked`/`未生成可审批草稿` 的具体原因，不借用自动热度最高的无关事件硬写。若只命中品牌/模型名，会明确说明“未匹配到具体主题”并建议补充主题词或发送精确可信链接。

## 写作、审批与发布

- 复用 `create_draft` / `render_draft` / 审批绑定 / 通知 outbox；正文、来源 URL、图片哈希、计划时间和审批过期时间仍按原规则绑定。
- 用户的角度/受众要求作为 `instruction` / `editorial_instructions` 传给 writer，与 `evidence` 分开；它不能成为事实依据，也不能改变流程。
- 手动选题绕过自动热度排序和自动 `max_queued_topics` 队列上限，但**不绕过**每日草稿、待审批、证据/时效、去重、审批哈希、预算和调用次数限制。
- 手动 `topic` job 优先级高于尚未开始的自动任务；发布任务、审批/修订仍按原逻辑执行。
- 本功能从不调用 publish。真实发布是否可用只由现有四道发布闸门决定。

## 配额与延后

默认值（`bot/config.py` 的 `topics` 段）：

```toml
[topics]
max_input_chars = 4000
max_pending = 3
max_per_day = 5
search_max_results = 5
search_max_fetches = 2
search_lookback_days = 7
```

- 单条消息超过 `max_input_chars` 直接拒绝，不建请求。
- 待处理/延后请求达到 `max_pending` 时拒绝新请求。
- 每个本地自然日最多接受 `max_per_day` 个选题请求；该上限独立于 `limits.max_drafts_per_day`。live 当前为 10（2026-11-11 由 5 放宽）；`limits.max_manual_drafts_per_day` 仍为 5，草稿额度不是这次 5/5 的原因。
- 今日草稿额度已满、待审批达上限、预算/调用次数耗尽时，请求标记为 `deferred` 并排队一条可见说明；job 延后到下一个本地自然日再继续，不会每几秒重复花钱检索。
- 恢复“已存在但尚未完成审批”的草稿时，不再看今日草稿额度（否则自己的未完成草稿会阻塞当天恢复），但仍服从全局暂停、预算暂停和待审批上限。
- 延后不是完成：最终完成消息一定包含审批码；否则会明确写阻塞/无证据/失败原因。

## 失败、租约与取消限制

- job 租约到期后按既有 `max_attempts` 重试；次数用尽时 job 进入 `failed`。worker 启动和 tick 会协调对应 `topic_requests`：若已有草稿和审批则补记为 `completed`，否则写成 `failed` 并发出一条可见的“未生成可审批草稿”结果，避免请求永久停在 `researching/drafting`。
- 该协调只处理 `queued/researching/drafting/deferred`；终态请求不会被改写。最终通知使用 `topic:<code>:final` 去重，重复启动/协调不会重复发消息。
- v1 没有单独的“取消选题”命令；目前只有 job 失败/租约耗尽后的自动收口，以及用户继续使用既有“拒绝/修改”处理已生成的审批。若需要中止排队中的请求，仍需后续版本增加显式取消命令和状态机。

## 通知措辞

审批通知现在统一由发布闸门生成安全说明：

- 未武装真实发布（包括 `mode=live` + `publisher.provider=xiaohongshu` 但 `live_enabled=false` 或环境开关缺失）：
  `试运行：真实发布关闭，批准仅记录审稿决定。`
- 离线/mock 还会额外写“本地模拟（不会真实发布）”。
- 只有四道发布闸门全部武装时才改为“真实发布已武装；批准后仍由发布闸门核对……”。

## 测试（全部离线）

新增 `tests/test_remote_topics.py`，使用临时数据库、冻结时钟、假的 DeepSeek Search/Fetch、FakeLLM、FakeNotifier 和本地 PNG 渲染；没有真实网络、真实密钥、真实飞书消息或真实发布。

新增 `tests/test_remote_topic_evidence_fix.py`，用合成的多章节文章、本地临时数据库和 mocked DeepSeek transport 覆盖：后续相关段落进入 writer、标题/元数据不成为证据、无实质正文时 `blocked`、重复来源不改写已绑定 evidence、旧请求重试重建 evidence，以及结构化 `{"error": ...}` 只触发一次模型拒绝且 usage 已结算。

覆盖：owner p2p 端到端、非 owner/群聊/无 owner/放宽 p2p、重放与并发、错误审批文本、长度/待处理/每日上限、query 128 字符（含日期）、长 query 截短、SSRF/凭据/file/私网链接、非可信域名回退、缺失/无关/过旧证据、草稿配额/待审批/预算延后与次日恢复、重启/重试不重复草稿、检索/模型错误结果、状态/帮助、通知措辞、既有批准/修订闸门；新增覆盖：共享厂商/模型名不构成主题相关性（含中英双语 memory vs pricing 回归）、无关同厂商文章 fail-closed、精确可信 URL 优先且校验正文/时效、自动采集重复内容的证据复用、`www.` 前缀精确处理、渲染中断/审批中断的 worker 恢复且不重复调用模型/不重复建草稿、恢复尊重暂停与待审批上限、job 租约耗尽收口。

已执行（本机离线，临时数据库/假 provider/FakeLLM/本地 PNG）：

```powershell
.venv\Scripts\python.exe -m unittest tests.test_remote_topics -q
# Ran 30 tests ... OK

.venv\Scripts\python.exe -m unittest tests.test_remote_topics tests.test_approval_publish tests.test_notify tests.test_deepseek_search tests.test_deepseek_transport tests.test_worker_e2e tests.test_gateway_lifecycle tests.test_concurrency tests.test_ingestion tests.test_render tests.test_security_network -q
# Ran 101 tests ... OK
```

`tests/test_deepseek_transport.py` 的本地 302 测试服务器现在会先读取 POST body 再响应；否则 Windows 可能在客户端仍在发送时重置连接（WinError 10053），导致与产品代码无关的假失败。断言与安全含义未改变。

本轮 S1 收尾/S2/S3 新增 `tests/test_research_s2.py`（计划、来源身份、身份缓存、跨语言 alias、单批判定、secondary 可用性）、`tests/test_s3_modes.py`（news/analysis、发布闸门、owner 命令、去重/非 owner/终态）、`tests/test_research_r1_r4.py`（R1 新来源正文接线、R2 链接关系、R3 预算/模型异常、R4 analysis 发现日期）、`tests/test_source_scope.py`（共享托管 owner/repo、exact resource、编码路径、缓存策略、正反端到端）和 `tests/test_secondary_sources.py`（第三方 secondary 来源生成带标签草稿的端到端），并加强 `tests/test_research_pipeline_s1_review.py` 与 `tests/test_source_review.py`。最近的离线结果：102 项针对性测试通过；`unittest discover -s tests -v` 全量 266 项通过（599.348 秒）；迁移 8 合成 v7 库演练 1 项通过。2026-11-12 又新增 AIHot 发现层：7 项单测、80 项针对性回归、全量 `Ran 295 tests in 708.289s, OK`。

## S2：计划与来源身份

- 每次选题最多一次结构化计划调用，生成实体/版本/主张和不超过 3 条检索 query；“Fable 5.2”和“Mythos 5.2”等版本分别保留，不做跨版本词面去重。计划失败时使用有界回退 query，不无限重试。
- 计划只提供检索方向，不能凭空提供证据 URL；最终证据仍来自实际抓取正文。query 可按中文原名、英文/别名和精确产品名调整，但关键词只用于排序，最终相关性必须由正文和日期/完整性校验支持。
- 来源身份使用一次批量判定，输入是实际抓取到的 URL、正文片段、正文长度和“可信页面是否真实链接到该域名”的关系。程序会拒绝不存在的 evidence ID、伪造 URL、仅凭自述“official”的域名、托管平台/同名不同组织，以及没有可信链接支撑的官方声明。
- 判定结果分为 `official` / `vendor_primary` / `secondary` / `unknown`，并写入 evidence 的 `source_role` / `source_verified`；旧版本官方页不能证明新版本发布，第三方转载只是线索，来源身份与正文支持是两件事。
- 可信 secondary 报道不要求事先列入 `allowed_domains`：只要模型基于实际正文给出有支持的主张、置信度达到 `topics.identity_min_confidence`（默认 0.6），即可作为 `usable` 来源写入草稿；`source_verified` 仍为 `false`，审批卡片标为“非官方媒体/转述”，writer 必须使用“据/媒体称/报道”措辞，不得写成官方宣布。无正文支持或低置信度仍拒绝。
- 共享托管站（GitHub/Bitbucket/Gitee）按可信页面真实链接的 `owner/repo`（含子页）授权，其他站点只按 exact resource 授权；编码路径、前缀碰撞和同 host 其他组织不能继承授权。关系页必须真实链接到本批候选，普通提及、警告/否定语句和无关同域名文章不会进入 identity。
- 身份判定结果可缓存，但 cache key 包含实际材料指纹和策略（主题、模式、可信域名、阈值、链接作用域策略版本）。缓存命中也会按当前策略重新执行链接作用域检查并计算 `verified`，不会把 unknown/secondary 升级为官方；配置、内容、模式、可信范围或作用域策略变化会 miss 并重新判定。
- 直接链接、计划 query、身份核验涉及的抓取/重试共享 `topics.research_max_fetch_attempts`（默认 6）上限；一次任务最多一次来源身份模型调用。

## 全结果缓存

- 完整研究结果（不是仅正文）可缓存 `topics.research_cache_hours`（默认 6 小时）；cache key 包含规范化主题、query、模式、可信/验证域名、时效、直接链接和抓取上限策略。
- 显式 `刷新` 绕过缓存。缓存恢复仍要重新通过当前可信域名和时效检查，不能提高来源身份或新闻时效；配置变化会使旧 key 失效。

## 已知限制

- v1 仅文本；不解析富文本卡片。
- 定向检索依赖至少一个已配置 `deepseek_search` 来源及其 `allowed_domains`；没有配置时请求会明确 `blocked`。
- 关键词抽取和 facet 判断是确定性词法规则，不等同于跨语言语义搜索。中文主题配英文原文时，如果没有精确可信 URL，可能因为中文 facet 无法命中英文正文而 fail-closed；这是有意的“宁可不写”，阻断原因会明确说明。此时发送原文的精确可信 URL 或补充英文关键词即可。
- 只有提交链接在已配置可信域名内且搜索结果返回完全相同的规范 URL 时，才会优先该原文；不会为了“找到它”而直接抓取用户链接或扩大域名白名单。
- v1 没有显式取消排队请求的命令；租约/重试耗尽会自动写入可见失败结果，已生成草稿仍通过既有批准/拒绝/修改命令处理。
- 本文件不激活生产：重启 gateway/worker、调整 live 配置和真实发布授权仍由 parent 执行。
