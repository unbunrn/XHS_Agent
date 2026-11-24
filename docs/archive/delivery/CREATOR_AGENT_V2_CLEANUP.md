# Creator Agent V2 — 清理清单

日期：2026-11-13；2026-11-17 补充：隔离副本、临时测试/浏览器目录和缓存已清理。

## 结论

本轮**没有删除**任何旧模块、旧表、历史迁移或生产数据。

原因：

1. V2 已复制到主项目并接入 live，但旧 `process_topic_request` / `create_draft` 仍是回退路径；没有生产灰度证明可以删除。
2. 旧审批、发布、SSRF、迁移、审计和 feed 回执确认是被复用的安全边界，不是冗余。
3. V2 依赖旧 `ingest_targeted_entries`、`_insert_draft`、`_render_and_store`、`_finalize_with_approval` 等桥，删除会破坏真实发布路径。
4. 删除任何候选前需做 import/引用/测试扫描；本轮没有满足“明确替代 + 已确认冗余”的模块。

## 候选（以后评估，不本轮删除）

| 候选 | 原因 | 删除前条件 |
|---|---|---|
| 旧固定 orchestration 的部分分支 | V2 runner 覆盖动态路径 | 真实 runtime + 等价测试 + 生产灰度 |
| 重复的 source policy helper | 旧 `source_verified` 仍被 live 使用 | 迁移/兼容层 + 全量回归 |
| `docs/AGENT_LOOP.md` 与 V2 文档重叠 | 文档重复 | 合并并保留历史链接 |
| 隔离副本 `var/dev/creator-v2/` | 主项目已复制并验证 | 已于 2026-11-17 删除；主项目全量绿 |

## 禁止

- 不删除生产数据、审批/发布/对账/安全表、历史 migration。
- 不把 `creator_artifact_ready` 当成 approval。
- 不在没有 owner 明确批准的情况下发布。

## 当前保留物

- 旧 topic/draft/publish/approval 代码和测试。
- migration 1–9 全部历史。
- V2 代码、V2 测试、隔离副本、备份 `var/backups/v2-pre/bot-125841` 和 `var/live/backups/pre-v2-20261113-125*.sqlite3`。
