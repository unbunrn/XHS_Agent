# S1 独立验收与返修清单

文档更新时间：2026-11-10T21:06:18+08:00（北京时间，仅表示文档更新时间）。

## 结论

**S1 暂不通过部署验收，需要返修。** DSH 已完成主体结构、候选排序与诊断表，但单任务抓取上限、物理调用统计、诊断脱敏和来源状态仍存在问题。直接链接与缓存也有未完成要求。

本次只读检查业务代码、运行离线测试并编写验收文档；没有修改业务代码，没有读取生产密钥，没有调用收费 API、发送消息、触发发布或重启服务。本文不代表当前生产进程已加载 S1；实际运行版本未在本轮确认。

## 验证证据

- DSH 自报：200 项全套测试通过，见 `S1-research-pipeline.md`；这是交付方结果，不等同独立验收。
- 独立相关回归：命令与最终结果见本文件末尾验证补记。
- 独立反例脚本：项目内 `var/tests/parent_s1_probe.py`。
- 独立反例结果：项目内 `var/tests/parent-s1-probe.log`。
- 所有反例使用临时测试库、假模型与假搜索/抓取响应。URL 中的 TEST_SECRET、FAKE_KEY、FAKE_SIGNATURE 都是人为构造的假值。
- 路径环境：本机 Windows，项目根目录 `D:\Dev\Projects\bot`。`var/tests` 是本地验证产物，不是需要复制到交接仓库的源码。

## 必修 1：抓取次数按轮重置，突破单任务限制（P1）

定位：`bot/services/topics.py:712`、`:1672`；`bot/adapters/sources.py:296`；`bot/services/topic_research.py:185`。

每轮 `_topic_research` 都创建一个新 adapter，`fetch_attempts` 从 0 开始。第一轮和扩域轮各有独立额度，而不是共享同一任务的物理 Fetch 上限。另有 `min(12, ...)` 与注释所称的六次硬上限不一致。

已复现：第一轮 5 个空正文候选，模型建议一次补充域名，第二轮又 5 个空正文候选；同一个 topic_request 实际 search=2、fetch=10，两行 attempt 分别记录 5。全局每日费用仍有限制，但不能代替单任务上限。

修复：由任务编排层创建共享调用额度，传到所有轮次、直接抓取和 transport 重试。默认单任务上限 6；本阶段按六次硬上限实施，可配置收紧。第二轮只用余额；失败不能重置额度。不要把成功正文数或逻辑 fetch 调用数当物理尝试数。

验收：两轮总计不超过 6；直接 URL + 两轮 + 重试合计也不超过 6；余额耗尽明确记录 fetch_limit。保留第一轮和第二轮分别统计及任务汇总。

## 必修 2：物理抓取统计漏算重试（P1）

定位：`bot/services/topic_research.py:391`、`:405`；`bot/adapters/sources.py:565`。

服务层在每次 `fetch_content` 前只加 1，但 adapter 内可能发送多次物理请求。遇到 FetchLimit 又无条件减 1，可能漏掉该逻辑调用中已经发送的重试。adapter 的计数还发生在预算预留前，预留失败时实际并未发送。

已复现：一个候选连续 timeout，实际 `_post_once` 被调用 3 次，数据库 physical_fetches=1，回执也只报告一次。

修复：传输层作为物理调用事实的唯一来源。区分 logical_candidates、physical_attempts、retries；记录实际发送前后状态，预算拒绝不算已发送。即使重试中途触发 FetchLimit、预算耗尽或最终异常，也要保存已经发生的物理次数。确保预算中断时 research_attempts 能进入 deferred 等终态，而不是留下 running。

验收：超时重试 3 次记录 3；第一次成功记录 1；预算在第一次发送前拒绝记录 0；第一次失败后预算拒绝下一次记录 1；中途 FetchLimit 不丢失已发送次数。429 与 timeout 的候选结果、物理尝试次数分别展示。

## 必修 3：诊断 URL 没有清除敏感查询参数（P1）

定位：`bot/services/research_diagnostics.py:91`、`:156`。

`canonical_diagnostic_url` 直接调用用于去重的 `canonicalize_url`。后者只删除 tracking 参数，不是日志脱敏工具。URL 随后直接写入 research_candidates.url/canonical_url，没有经过 sanitize_text。

已复现：假 URL 的 `token=TEST_SECRET`、`api_key=FAKE_KEY`、`signature=FAKE_SIGNATURE` 均保留在返回值中。此处证明的是诊断落库路径有泄漏风险，不表示已发现真实凭据泄漏。

修复：把“请求/去重使用的 canonical URL”和“可写入诊断的显示 URL”分开。诊断 URL 清除凭据、fragment、敏感查询值；优先采用保守查询参数策略，覆盖大小写、百分号编码、重复键及签名 URL。需要精确关联时保存不可逆标识，不把访问令牌原文作为关联键。不要为脱敏改坏真正请求的 URL。

验收：通过 add_candidate 实际落测试库，检查 url、canonical_url、query、detail、error 均不出现合成秘密；不能只测试 sanitize_text 函数。覆盖 query 中嵌套 URL。继续保证原文不进入诊断表。

## 必修 4：模型建议域名被误记为已验证来源（P2）

定位：`bot/services/topics.py:1672` 将基础 domains 与 review_domains 合并；`:720` 把合并列表作为 trusted_domains；`bot/services/topic_research.py:282` 据此计算 domain_verified。

已复现：第二轮仅由 fake 模型建议的 seed.example.com，其 5 个候选全部被计入 domain_verified=5。代码无法区分“搜索允许访问的候选域名”和“原本配置可信的域名”。这与交付报告声称的“模型扩域结果标 domain_unverified、citable_as_official=false”不一致。

修复：搜索范围与来源身份分开传递，基础 trusted_domains 不因模型推荐改变。模型建议只能标 candidate/unverified；S1 不需要提前实现 S2 的完整来源核验，但不能把推荐伪装成已验证。若允许候选正文继续参与研究，必须把来源状态传到证据与写稿输入，不能只在诊断中写一个下游看不到的 false。

验收：基础域名 verified；仅模型建议的域名 unverified；未知域名不得因为进入 search allowlist 而升级；不自动修改长期配置。不要通过把所有未知域名放行、或全部拒绝所有来源来让测试通过。

## S1 未完成项：直接链接和缓存（P2）

### 安全但未核验来源的直接链接

`topic_research.py:573` 对安全、未配置的域名只记录 domain_unverified，不获取正文。DSH 报告已主动披露此限制，但原 S1 要求是将安全抓取与来源可信分层：可以获取候选材料，不意味着可以把它当官方证据。目前非白名单且未被索引的原文仍无法走通此路径。

完成安全候选抓取，保持 SSRF、凭据 URL、协议和重定向限制；未核验正文留在候选层，不作为已验证官方材料。若完整来源核验留待 S2，明确返回“已取得候选正文，来源身份待核验”，而不是说没有正文。补可信/未验证/私网三类直接链接测试，明确预算计数。

### 缓存与显式重新检索

现有 `_cache_lookup` 只复用已经进入 raw_items 的正文，且每次仍执行 search_candidates。代码中未看到显式 refresh 参数绕过缓存的路径；交付报告“显式重新检索可绕过缓存”缺少实现依据。不要把时间窗口设为 0 等同可靠的刷新开关。

补充带 TTL、主题及来源配置边界的复用策略和显式刷新参数，接到真实命令入口。相同研究可复用有效结果减少重复付费；强制刷新确实重新获取。缓存不能提升来源可信等级，也不能把旧正文当新公告。测试 TTL 内、TTL 外、强制刷新和来源配置改变四种情形。

## 其他诊断完整性检查

这些应在返修时一并补测试，避免回执继续误导：

- `_entry_freshness` 返回 `(True, date_unknown/date_future)` 时，目前后续没有持久记录 fresh_code，而是直接计入 freshness_passed。至少单独保存日期状态；本轮不要求重写 S3 时效策略。
- 提前取得足够正文或触及上限时，剩余候选应标 not_fetched 及原因，避免 decision_code 空白。
- 缓存正文与搜索元数据混合时，保存两者来源和时间，不覆盖后跳过必要判断。
- 已有直接正文复用搜索标题/日期后，不能跳过对更新后元数据的检查。

## 给 DSH 的执行指令

请阅读本文件和 docs/RESEARCH_PIPELINE_REPAIR.md，只返修 S1，不开始 S2/S3。先把以上反例转为正式离线回归测试，再修改代码；不要只修改报告、调整 fixture 或删断言。保持 transport、研究编排、诊断存储职责分离，不继续把实现全部堆进 topics.py。保留已有改动与历史迁移，禁止重写已发布迁移。

禁止读取 secrets、Cookie、var/live，禁止收费 API、真实平台、服务重启、飞书消息或发帖。所有验证使用临时库和假响应。完成后更新 S1 交付报告和 docs/README.md，逐项给出测试证据、已完成项和剩余限制，然后停止，交由独立复核。不要声称部署完成。

## 独立验证补记

执行 `.venv\Scripts\python.exe -m unittest tests.test_research_pipeline_s1 tests.test_remote_topics tests.test_source_review tests.test_deepseek_search -q`：**55 tests，126.548s，OK**。日志：`var/tests/parent-s1-review.log`。日志末尾存在 unclosed event loop 的 ResourceWarning，未导致测试失败。

现有相关回归通过，但上述独立反例仍成立，说明测试覆盖存在缺口。未重复执行全套 200 项测试；未演练生产迁移或部署，待返修后再验收。
