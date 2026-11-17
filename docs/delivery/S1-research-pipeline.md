# S1 检索管线交付报告：诊断、排序与直接链接

- 日期：2026-11-10
- 范围：仅 S1「诊断、排序和直接链接」
- 状态：**离线代码实现与离线测试完成；已按独立验收返修；未部署、未重启服务、未发送飞书消息、未调用真实平台或收费 API。**
- 依据：[选题检索与正文证据改进](../RESEARCH_PIPELINE_REPAIR.md) 的 S1 部分；S2/S3 仅保留设计，未开发。
- 数据边界：未读取 `config/secrets.toml`、Cookie、`var/live` 数据库或生产日志；测试只用临时数据库、Fixture、Fake LLM、假搜索/抓取响应。

## 1. 本次目标

修复“前两条搜索结果正文为空，第三条本来可用的结果却不再被尝试”的问题，并把检索过程从“只返回一个结果列表”升级为可审计的诊断流程：

1. 记录每次检索的原始候选、来源/URL 检查、实际抓取、可用正文、主题匹配和时效判断。
2. 对每个候选记录阶段、决策码和失败原因，通知/阻塞文案使用真实计数。
3. 为用户提交的安全链接提供直接抓取和排序入口，不依赖搜索索引命中。
4. 失败候选继续尝试后续候选，但物理抓取次数有全局上限。
5. 搜索摘要永远不能冒充正文；无法证明的内容不进入可引用证据。

## 2. 实现内容

### 2.1 诊断数据与持久化

- `bot/adapters/types.py`
  - `AdapterError` 增加可选 `code`，用于区分超时、HTTP 429/5xx、网络错误等。
  - `SourceFetch` 增加向后兼容的 `diagnostics: dict` 字段。
- `bot/adapters/sources.py`
  - 新增 `PhysicalFetchQuota`：同一选题的所有轮次、直接链接和重试共享一个物理抓取额度，硬上限 6 次，可由配置收紧；一轮耗尽后下一轮只能使用剩余额度。
  - 物理抓取次数由传输层在“预算已预留、请求即将发出”时计数；预算拒绝的请求不计入，重试计入，超限抛出 `FetchLimit` 且不抹掉已发送次数。
  - 新增 `safe_url()`：使用现有 SSRF guard 校验 http(s)、无凭据 URL，但不要求域名已加入搜索 allowlist。
  - 新增 `search_candidates()`：只返回候选与诊断，不按旧逻辑提前抓取；每个候选带 canonical URL、域名、URL 安全检查、域名验证、显式正文、摘要存在标记。
  - 新增 `fetch_content()`：经过统一 SSRF 校验后抓取单页正文；不按搜索 allowlist 限制，因此可信域名和非可信域名的安全直接链接都可由候选层抓取；是否可作官方证据由独立来源身份判断。
  - `_post()` 在每次物理请求前计数并在达到上限时抛出 `FetchLimit`；错误映射带 `http_redirect`、`http_error`、`timeout`、`network_error` 等 code。
- `bot/services/research_diagnostics.py`
  - 诊断 URL 使用独立展示清洗：移除凭据、fragment 和敏感查询参数值（token/api_key/signature/session 等，大小写、百分号编码和重复键均处理），嵌套 URL 递归清洗，并保存不可逆 `url_fingerprint` 用于精确关联；实际请求 URL 不改变。
- `bot/schema.py` 追加迁移 6 与迁移 7（仅新增表/列/索引，不删除历史数据）：
  - `research_attempts`：每次选题检索一行，保存清洗后的 query、provider、状态、计数 JSON、诊断 JSON、错误和时间。
  - `research_candidates`：每个候选一行，保存标题、清洗后的 URL/canonical、`url_fingerprint`、域名验证、`citable_as_official`、阶段、决策码、抓取状态、正文长度/SHA-256、`date_status`、延迟、脱敏细节。
  - 不把长原文、搜索结果原文或密钥写入 `topic_requests`；正文内容仍只进入既有证据链路。

### 2.2 排序与直接链接

- 新增 `bot/services/topic_research.py`：
  - 先处理用户明确提交的链接。可信域名的链接直接抓取并可进入官方证据；安全但未验证域名的链接也会经过统一 SSRF 校验后抓取为**候选正文**，记录 `domain_unverified`，不进入证据、不生成草稿；若整轮只有这类正文，返回“已取得候选正文，来源身份待核验”，而不是误报没有新闻。
  - 可信直接链接抓取成功后先推迟入队，继续搜索以取得真实文章标题；搜索返回同一 canonical URL 时复用直接抓取的正文，避免重复抓取；搜索没有返回时把直接结果放在首位。
  - 搜索候选做廉价词法预筛和确定性排序：直接链接、发布/新闻路径、标题含实体/版本、关键词命中优先；首页、列表页、登录页、明显无关页面靠后。
  - 物理抓取由任务级共享额度控制，硬上限 6 次；达到可用正文数量或预算上限即停止，不会无限递归搜索。未处理的候选标记为 `not_fetched`，不留下空决策码。
  - 400/429/超时/空正文/解析失败/低质量正文/主题不匹配/过期/未知日期都有独立决策码；`date_unknown` / `date_future` 单独持久化。
  - 模型扩域只扩展搜索范围，不扩展可信身份：模型建议域名永远标 `domain_verified=0`、`citable_as_official=0`，即使搜索命中也不进入官方证据；来源身份只由基础可信域名决定。
  - 缓存只复用启用来源的正文和诊断，受 TTL 限制；`refresh=True` 绕过缓存，显式 `bot refresh-topic CODE` 会重置可重试请求并投递带 `refresh` 的任务，不删除历史。
- `bot/services/topics.py`：
  - 拆分主题匹配与时效判断，保留原 `_entry_matches` 包装以兼容调用方。
  - `process_topic_request` 改用新的诊断检索；第二轮扩域后保留第二轮的真实计数用于阻塞回执。
  - 中文阻塞原因区分“没有搜到结果”“搜到但主题不匹配”“来源检查失败”“抓取失败”等，不再一律写“没有返回可用正文”。
- `bot/services/research.py`：
  - 直接链接正文使用独立的正文句子提取，过滤标题/导航/元数据。
  - `ingest_targeted_entries(..., direct_url=...)` 与 `refresh_targeted_evidence(..., direct_url=...)` 把直接正文标记传入证据构建；搜索摘要仍不会被当作正文。

### 2.3 回执与缓存

- `bot/services/research_diagnostics.py` 汇总真实计数，生成中文回执，例如：
  “搜索发现 3 条；2 条通过来源检查；实际抓取 3 条；失败分布：2 条空正文；1 条进入候选。”
- 计数来自 `research_attempts.counts_json` 与候选记录，不由模型生成。
- 缓存只复用启用来源、同一 canonical URL 且仍在 `topics.research_cache_hours` 内的正文与诊断；显式 `refresh=True` 绕过缓存并强制重新抓取；缓存不会证明“没有新公告”，也不会把旧内容升级为可信证据。
- `sanitize_text()` 与 `canonical_diagnostic_url()` 在写入诊断/检查前脱敏 Bearer、token、cookie、API key、敏感查询参数并截断；候选另存不可逆 `url_fingerprint`。

## 3. 变更文件

| 文件 | 变更 |
| --- | --- |
| `bot/adapters/types.py` | `AdapterError.code`；`SourceFetch.diagnostics` |
| `bot/adapters/sources.py` | 共享物理抓取额度、候选诊断、直接候选抓取、传输层计数、错误码 |
| `bot/config.py` | `topics.research_max_fetch_attempts` / `research_cache_hours` / `research_min_usable` / `research_min_body_chars` |
| `bot/schema.py` | 迁移 6：`research_attempts`、`research_candidates`；迁移 7：URL 指纹、可引用标记、日期状态 |
| `bot/services/research_diagnostics.py` | 新增：URL/文本脱敏、指纹、诊断存储、计数、中文回执 |
| `bot/services/topic_research.py` | 新增：排序、可信/候选分离、直接链接、共享抓取额度、候选决策、刷新 |
| `bot/services/topics.py` | 接入新检索流程；共享额度；模型扩域与来源身份分离；可信/候选文案 |
| `bot/services/research.py` | 直接正文证据提取；摘要不作正文 |
| `bot/worker.py` | 选题任务传递显式 `refresh` |
| `bot/cli.py` | `refresh-topic CODE` 显式重新检索入口 |
| `tests/test_research_pipeline_s1.py` | 新增：6 个 S1 验收/回归测试 |
| `tests/test_research_pipeline_s1_review.py` | 新增：12 个独立验收返修测试 |
| `tests/test_source_review.py`、`tests/test_remote_topics.py` | 按“候选正文不等于官方证据”契约更新断言，不降低安全约束 |
| `docs/README.md`、本报告 | 文档索引与交付报告 |

未修改 S2/S3 设计文档；未新增发布、审批或平台写操作。

## 4. 验证结果

运行解释器：`.venv/Scripts/python.exe`；未调用真实网络、真实平台、收费 API，未读取生产数据。

| 验证 | 命令 | 结果 |
| --- | --- | --- |
| S1 专项 | `python -m unittest tests.test_research_pipeline_s1 -v` | **6 tests，OK，约 15s** |
| 独立验收返修专项 | `python -m unittest tests.test_research_pipeline_s1_review -v` | **12 tests，OK，约 40s** |
| 检索/选题相关回归 | `tests.test_research_pipeline_s1 tests.test_research_pipeline_s1_review tests.test_remote_topics tests.test_source_review tests.test_deepseek_search` | **67 tests，OK，144.576s** |
| 全套离线测试 | `python -m unittest discover -s tests` | **212 tests，OK，338.314s，exit 0** |
| 全套日志 | `var/final-test-run-20261110-s1-review.log` | 完整输出已保留 |

S1 验收对应：

1. **前两条空正文、第三条有效**：复现用例通过；第三条候选被选中并进入证据，物理抓取计数为 3。
2. **直接安全链接**：可信域名的未索引链接可直接抓取并形成证据；安全但未验证的域名会抓取为候选正文，但不进入证据、不生成草稿，阻塞文案明确“来源身份待核验”。
3. **失败可区分且不终止**：429、超时、空正文分别记录为 `fetch_http_error`、`fetch_timeout`、`fetch_empty`，后续候选仍会被尝试；重试由传输层按物理请求计数。
4. **第二轮失败使用第二轮计数**：扩域后阻塞回执引用第二轮统计，搜索调用次数为 2；两轮共享 6 次物理抓取额度。
5. **搜索摘要不得当正文**：摘要仅作为摘要标记，摘要文本不会进入证据；无正文时阻塞。
6. **预算耗尽**：返回 `deferred`，不会误报“没有新闻”；后续可重新检索。
7. **诊断可审计**：attempt/candidate 记录包含清洗 query、candidate URL 与指纹、provider、抓取状态、正文长度/哈希、决策码、日期状态和脱敏细节；敏感查询参数不落库。
8. **来源身份分离**：基础可信域名才可标 `domain_verified=1`；模型建议域名和未验证直链始终 `citable_as_official=0`，不能因进入搜索范围而被升级。
9. **缓存与刷新**：命中启用来源的 TTL 缓存可复用；`refresh=True` 强制重新抓取；`refresh-topic CODE` 仅重置 blocked/failed 请求并投递显式刷新任务，不删除历史。
10. **诊断完整性**：可用正文之后的候选标 `not_fetched`；未知/未来日期单独持久化，不伪造发布时间。

## 5. 部署步骤（本次**未执行**）

本次没有部署、没有重启 worker/console、没有修改生产数据库。后续授权部署时建议：

1. 先备份生产库并确认完整性。
2. 在合成的旧版数据库上演练迁移 6→7，确认原 `publish_results`、approvals、drafts、notifications、S1 诊断表数据保留；本次未做生产库迁移验证。
3. 部署代码；worker/console 初始化数据库时会自动应用迁移 7。
4. 在无待审批/待发布任务时重启服务。
5. 运行 `python -m bot --config config/bot.live.toml doctor`、`bot status`，并观察下一轮检索的 `research_attempts`/`research_candidates`。
6. 真实搜索/抓取/provider 行为需要另行授权验证；本次未开启真实调用。迁移未在真实库演练，部署前必须单独验证。

## 6. 限制与未验证项

- **真实 DeepSeek Search/Fetch 行为未验证**：本环境无可用外网/TLS，且本次授权禁止收费 API/真实平台调用；测试使用假响应验证协议、排序、计数和错误分流。
- **未验证域名只作候选**：安全但未加入基础可信源的提交链接会经过 SSRF/凭据/协议检查后抓取为候选正文，但不会进入证据或生成草稿；若整轮只有此类正文，返回“来源身份待核验”。这与“不得把未验证来源升级为官方证据”一致。
- **模型建议域名不升级信任**：模型建议仅扩大一次搜索范围；结果始终 `domain_verified=0`、`citable_as_official=0`，不会自动写入配置或长期可信源。
- **模型批量排序未启用/未验证**：当前使用确定性词法排序；S1 允许但未要求模型排序，本次没有引入逐候选模型调用。
- **缓存/刷新只在离线验证**：缓存以启用来源、canonical URL 和本地时间为边界；`refresh-topic CODE` 只重置 blocked/failed 请求并投递显式刷新任务。抓取页面若没有可解析发布时间，保留 `date_unknown`，不伪造日期。
- **迁移与真实 provider 未在生产验证**：迁移 6→7 未在真实库演练；真实搜索/抓取返回字段、重试和额度仍需单独授权验证。
- **平台/飞书未验证**：未发送飞书、未调用真实平台、未部署；本轮不涉及 S2/S3。
- 所有测试库与日志在 `var/tests`、`var/final-test-run-20261110-s1-review.log`，未提交 Git。

## 7. 下一步真实验收所需

需要另行授权并提供：

1. 可用的 DeepSeek API 环境（或允许测试用的脱敏录制响应），验证真实搜索候选、抓取 429/超时和返回字段。
2. 一套可信源配置与至少一个真实官方发布页，确认直接链接、标题/日期解析和来源核验。
3. 若恢复 S2/S3，再单独评审缓存、模型排序和长期运行策略；本次不提前开发。

## 8. 独立验收返修记录（2026-11-10）

依据 [S1 独立验收与返修清单](S1-independent-review-20261110.md) 返修，未删除或改写原验收报告。

| 验收问题 | 返修 |
| --- | --- |
| P1 每轮抓取额度重置，两轮最多抓 10 次 | 引入任务级 `PhysicalFetchQuota`，首轮、模型扩域第二轮、直接链接和重试共享硬上限 6 次；每轮保留自己的计数，attempt 另记任务总次数 |
| P1 物理抓取次数漏计重试，预算拒绝也被计入 | 传输层在预算预留成功后、请求发出前计数；区分逻辑候选、物理请求和重试；超限/异常时保留已发送计数并结束 attempt，不留下 running |
| P1 诊断 URL 泄露 token/api_key/signature | 诊断 URL 独立清洗：凭据、fragment、敏感查询值、嵌套 URL、大小写/编码/重复键；新增不可逆 `url_fingerprint`，实际请求 URL 不变 |
| P2 模型建议域名被当作已验证 | 搜索范围与来源身份分离；基础可信域名才可验证，模型建议域名始终候选/未验证，不进入证据，也不改配置 |
| 未验证安全直链未抓取 | 经 SSRF/凭据/协议检查后抓取为候选正文，标记 `domain_unverified`/`citable_as_official=0`，不进入证据；仅有候选正文时阻塞为“来源身份待核验” |
| 缓存/刷新无显式入口 | 缓存限定启用来源与 TTL；`refresh=True` 绕过缓存；新增 `bot refresh-topic CODE` 显式重置 blocked/failed 请求并投递刷新任务，不删除历史 |
| 日期状态与未处理候选留空 | 持久化 `date_status`；达到可用正文或额度后剩余候选标 `not_fetched` 和原因 |

对应测试：`tests/test_research_pipeline_s1_review.py` 12 个测试，覆盖共享额度、物理/重试计数、URL 脱敏、信任分离、直链候选、缓存/刷新、日期与 `not_fetched`。相关回归 67 tests，全套 212 tests，均离线通过；真实 provider 和迁移仍未验证（见 §6）。
