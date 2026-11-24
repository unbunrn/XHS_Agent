# Creator Agent V2 — 使用与验收

## 位置

V2 代码在主项目，并接入 live worker、Feishu 入站和真实审批表。迁移 10 已应用到 `var/live/bot.sqlite3`（`PRAGMA integrity_check=ok`）。原隔离副本已清理，不再作为运行入口。

## 运行测试

在仓库根目录：

```powershell
$env:PYTHONPATH="."
D:/Dev/Projects/bot/.venv/Scripts/python.exe -m unittest tests.test_creator_agent_v2 tests.test_creator_agent_live tests.test_migration_v2 -v
```

当前主项目 V2 targeted/审批回归（R1/R2 返修 + 手机首轮修复后）：**67/67 OK**（132.861s）。主项目全量：**Ran 367 tests in 812.120s — OK**（`var/tests/full_suite_r1r2_final.log`，手机修复前记录；修复后尚未重跑全量）。此前 N1–N4 基线为 359 项；副本全量：`Ran 340 tests in 642.312s — OK`。

手机验收首轮故障已修复：小写 `v2` 前缀不再进入 topic；文本修订默认保留父图；失败/暂停/需补充会发一条 owner outcome，不再静默。

正文超限不再作为 V2 的硬失败：会保留来源块、压缩 caption 到平台上限，并在正文追加“正文超过平台上限，已自动压缩；请审批时核对关键信息。”，然后照常生成审批。你可以拒绝或发 `V2 修改 ...` 让模型改；来源/数字/审批/发布安全校验没有放宽。`tests/test_content_soft_limit.py` 2 项 + `test_creator_agent_live` = 30/30 OK（53.520s）。

小红书公开正文不再附带 `【来源】`/URL，也不会写“以上为公开资料整理”这类报告腔；来源只留在审批元数据里供你核对。V2 的写稿指令改为第一人称、口语化观点/转述，不列参考文献、不编造亲测。`test_content_soft_limit + test_creator_agent_live + test_creator_agent_v2 + test_migration_v2` = **52/52 OK**（78.940s）。live worker 8524 已加载；未自动发布。

## 离线 demo

```powershell
cd D:/Dev/Projects/bot
$env:PYTHONPATH="."
D:/Dev/Projects/bot/.venv/Scripts/python.exe scripts/creator_agent_v2_demo.py
```

输出示例：

```json
{"status": "completed", "artifact_version": 1, "steps": 5,
 "actions": ["search","draft_artifact","render_images","review_artifact","submit_for_approval"],
 "note": "demo only; approval/publish are not touched"}
```

## 飞书测试语句（已接入）

`选题` / `写一篇` 现在默认路由到 Creator Agent V2（只接受 owner 私聊）。`V2 选题` 仍保留为等价别名；旧固定链路只用于历史请求和回退，不再接收新选题。

1. `选题 Claude Code 如何选择模型`
2. `V2 修改 少用“最强”，保留：Fable 5.2 / Opus 5.5 / 价格对比`

收到后先进入 `creator_agent_runs`，由模型逐步选择工具；完成后生成真实 `pending` 审批，飞书卡片仍用 `批准 XH-...`。不会自动批准或发布。

如果选题/修改说明中提供了抓取页面没有的日期或优惠，写稿会保留该句，但在审批中标记“来自选题/修改说明，未在抓取来源中核实”；未提供的无来源数字/促销句会被移除并提示。平台长度、来源、审批和发布闸门不变。

`V2 修改 ...` 现在会绑定最近一个可修订的 V2 作品：新 run 继承 `base_artifact_version`，`generation` 加一，旧的 pending 审批变为 `revision_pending` 并取消旧发布 job。若写“保留第二张图”，修订会复用原图片的 id/hash，而不是重新生成。

## 配置

```toml
[creator_agent]
enabled = true
max_steps = 8
max_revision_steps = 6
max_review_cycles = 2
max_searches = 5
max_physical_fetches = 6
max_model_calls = 14
max_invalid_decisions = 2
max_no_progress_steps = 2
max_run_seconds = 600
# 可选：确定性检查通过后再做一次独立模型审稿；当前 live 未启用
semantic_review = false
```

## 每日自动发现（V2）

每日任务只负责“发现候选选题并创建 V2 run”，仍然需要 owner 审批，**不会自动发布**。

```toml
[creator_agent]
daily_discovery_enabled = true
daily_discovery_seconds = 86400
daily_discovery_max_topics = 2
daily_discovery_max_age_hours = 48
daily_discovery_feed_limit = 50
```

流程：`AIHot /feed.xml` → 过滤时效与重复标题 → 把原文链接写入 `CreativeBrief.source_url` → 创建 `creator_discovery` 任务 → 为每个候选创建 `creator_agent_run` → worker 预取一次原文作为种子材料 → 模型自主搜索/写稿/审稿 → 真实 pending approval。

- 旧的 `collect`/`draft` 自动排期已停用；旧 `collect`/`draft` 仅作为历史 job 保留读取能力，不再产生新选题。
- AIHot 只是发现层；原文能取到时应改用原文，原文失败时才使用“据 AIHot 汇总”的摘要，并保持非官方标签。
- 每日任务失败不会自动重试出稿；会写一次 V2 失败/状态通知，owner 可重新发选题。

## 限制

- `LiveRuntime` 已接真实搜索、抓取、写稿、Pillow 渲染和审批桥；每日自动发现已由 V2 `creator_discovery` 接替，旧 `discover_topics` 不再使用。
- `submit_for_approval` 创建真实 `pending` 审批；批准后的发布仍走旧发布闸门和 feed 回执确认。
- 没有真实视觉模型；布局检查是确定性规则，`visual_review` 只能报 `not_available`。
- `semantic_review=false` 时只有确定性 claim/quote/number 检查；打开后才会额外调用一次独立模型审稿；该调用已在 provider 调用前计入 creator_agent 的模型预算。
- Migration 9 已在 live DB 执行；回滚只能恢复备份，不能删除历史迁移。
- 旧固定选题路径已物理删除；`research_plan`/`topic_research`/`adaptive_research` 等历史模块保留为只读/回退代码，不再驱动新选题。V2 没有删除历史迁移或生产数据。


## 视觉主题与文风配置（V2 视觉与文案升级）

### 1. HTML/CSS 本地视觉引擎与 6 套主题

- **主渲染引擎**：默认使用 `render.engine = "html"`，通过 Playwright 驱动本机已安装的 Microsoft Edge（`render.browser_channel = "msedge"`）将 12 种版式（`cover`, `comparison`, `process`, `keypoints`, `diagram`, `argument`, `annotated`, `stat`, `quote`, `timeline`, `checklist`, `qa`）和 5 种封面变体渲染为 `1080×1440` PNG。
- **6 套视觉主题**：`editorial`（杂志社论）、`swiss`（瑞士网格）、`terminal`（技术终端）、`newsprint`（铅印简报）、`notebook`（手账笔记）、`poster`（高反差海报），按近期冷却窗（`render.theme_cooldown = 2`）与版式偏好自动轮换并配对双色方案。
- **单篇主题覆盖**：若 `inspect_layout` 提示封面 `dHash` 与近期已提交作品过于接近（汉明距离 `< render.cover_min_distance`），Agent 可通过 `render_images(theme="...")` 指定其他主题重渲。
- **离线字体与无浏览回退**：运行 `powershell -ExecutionPolicy Bypass -File scripts/fetch_fonts.ps1` 可将精选字体下载至 `assets/fonts/`；若本机无法启动 Edge（抛出 `BrowserLaunchError`）且 `render.fallback_engine = "pillow"`，会自动回退到纯 Python Pillow 渲染（排版超限触发 `RenderError` 时不会静默回退或截断）。

### 2. 人设表达与去 AI 腔护栏

- **人设配置**：在 `config/voice.toml`（由 `writer.voice_path` 指定）维护第一人称口径、读者对象、语气规则、开头/结尾招式与正反例句；生成提示词按 `run_key` 确定性抽样并注入近期作品的 `avoid_recent`（近 8 篇开头、标题、封面文案与近 3 篇主题）。
- **AI 腔体检与单轮润色**：草稿经事实校验并剥离报告套话后，由 `bot/style_lint.py` 评估 12 条去 AI 腔及跨篇重复规则；得分高于阈值且启用 `writer.style_polish = true` 时执行至多 1 轮 `purpose = "polish"` 润色，并在润色后强制重跑 `validate_draft_payload(..., repair_caption_claims=False)`——若新增未证实数字或改变卡片数量，立即回滚到润色前草稿并记录至 `uncertainties`。
- **分用途温度**：在 `[llm.purposes.*]` 可按用途配置独立温度（例如 `draft = 0.85`、`revise = 0.8`、`polish = 0.9`、`review` / `draft_review` / `visual_review = 0.2`）。

### 3. 视觉画廊与人工对比抽检

使用本机 Edge 离线渲染 6 套主题的对比图卡并输出接触印相拼图与 §7.2 指标报告：

```powershell
$env:PLAYWRIGHT_NODEJS_PATH="D:\Dev\Tools\node-v22.23.2-win-x64\node.exe"
$env:PYTHONPATH=".;.venv\Lib\site-packages"
python -B scripts/render_gallery.py --strict
```

产物输出在 `var/gallery/<theme>/` 及总览拼图 `var/gallery/contact.png`。
