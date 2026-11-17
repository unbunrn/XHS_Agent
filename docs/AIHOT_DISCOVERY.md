# AIHot 新闻发现与二次分析

日期：2026-11-12（Asia/Shanghai）

## 目的

解决“选题很新、官方还没发稿/搜不到官网”时不生成草稿的问题。AIHot 只作为**发现层**：先找新鲜条目，再优先打开原文；原文取不到才使用明确标注的聚合摘要，最后交给 DeepSeek 做二次分析。审批与发布闸门不变，仍必须由 owner 批准具体版本。

## 来源分层

| 层级 | 判定 | 草稿可用 | 发布/审批措辞 |
| --- | --- | --- | --- |
| `official` / `vendor_primary` | 原文正文、官方公告/文档/博客等实际材料支持 | 是，`source_verified=true` | 官方/一手 |
| `secondary` | 有正文支持的可信媒体/转述 | 是，`source_verified=false` | 非官方媒体/转述；writer 用“据/报道” |
| AIHot 聚合摘要 | 原文未取到，只剩 AIHot 摘要 | 是，但强制 `verified=false` | `AIHot 聚合摘要/原文未取到（非官方）` |
| `unknown` / 自述官方 / 伪造 | 无正文支持或仅有自述 | 否 | 拒绝 |

AIHot 摘要的正文以 `据 AIHot 汇总（原文未取到）：` 开头；`_apply_aihot_summary_verdict()` 无论模型怎么判，都只能把它固定为 `secondary`/`usable`，不能升级为官方。

## 数据流

1. owner 发 `选题 <主题>`；live 开启 `topics.aihot_auto_match` 时，worker 拉取 `https://aihot.news/feed.xml`，用标题/摘要做保守词面匹配。
2. 也可以用 `选题 https://aihot.news/items/<id>` 指定条目；worker 解析条目，把标题作为真正选题，而不是把 URL 当主题。
3. 有原文链接：把 `direct_url` 改为原文，抓取原文正文；AIHot 条目链接只做发现记录。
4. 无原文链接：不搜索空内容，使用带前缀的 AIHot 摘要作为候选正文。
5. 正文进入统一的来源身份判定；官方/媒体/聚合摘要分别打标。
6. 正常写稿、渲染、审批；不会自动批准或发布。

## 配置

```toml
[topics]
aihot_enabled = true
aihot_auto_match = true
aihot_feed_url = "https://aihot.news/feed.xml"
aihot_max_items = 50
```

`bot/config.py` 默认关闭（离线测试不访问网络）；`config/bot.live.toml` 显式开启。`config/bot.example.toml` 只给注释示例。

## SSRF / 网络边界

- `aihot.news` 在本机解析到 `198.18.0.36`（非公网地址）。AIHot 适配器只对**配置的 feed 主机**使用 `allow_private=True`，并从 `/feed.xml` 直接读取，避免 `/rss.xml` 的 301。
- 原文仍走原有 SSRF 守卫；不会因为 AIHot 而放开任意私网访问。
- `HttpClient` 的普通重定向策略未放宽；AIHot 使用已确认的最终 feed URL。

## 可执行测试（不发布）

```bash
PYTHONPATH=. PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m unittest \
  tests.test_aihot tests.test_research_s2 tests.test_s3_modes tests.test_remote_topics -q -b
```

2026-11-12 实测：70 项通过（AIHot + S2 + S3 + 远程选题回归）；随后与 S1/S1-review/approval_publish 合计 80 项通过；全量离线套件 `Ran 295 tests in 708.289s, OK`（日志 `var/tests/full_suite_aihot.log`）。真实 feed 拉取成功；链路测试 `XT-EFKZF9` 生成草稿 13、审批 `XH-DXQC9Z`，证据来自 `openrouter.ai` 原文，未自动发布；该测试审批随后已 `rejected`。

## 已知限制

- 词面匹配是保守的，找不到时退回普通搜索；不会把不相关条目硬塞进选题。
- AIHot 摘要只是聚合摘要，不是原文；审批卡必须显示“原文未取到（非官方）”。
- AIHot 条目的原文可能在第三方平台（X、媒体站），其身份仍由原站正文决定，不因 AIHot 背书而升级。
- 目前没有 `热点` 列表命令；`选题` 自动匹配和 `选题 <AIHot item URL>` 已可用。
