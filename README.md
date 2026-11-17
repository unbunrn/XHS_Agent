# AI 工具观察 · 本地运营机器人

一个面向“小红书 AI 工具实用内容”的本地运营闭环：抓取来源、去重/聚类、生成草稿与 PNG 卡片、人工审批、受控发布、失败恢复。默认离线运行，不需要外部账号。

> 安全边界：默认使用 mock 发布，只写本地 `simulated` 导出，**不会**声称已真实发帖。真实发布需要四道开关同时满足，并且必须由账号本人在飞书私聊或本地控制台明确批准某一版最终正文和图片。

## 快速开始

要求：Windows + Python 3.12。建议先创建普通 venv 并安装固定依赖（不使用 `--system-site-packages`）：

```powershell
cd D:\Dev\Projects\bot
.\setup-bot.cmd
# 离线只想验证 venv：.\setup-bot.cmd -SkipInstall
# 若当前 .venv 是 DSH 的 --system-site-packages 运行时，需要重建：
# .\setup-bot.cmd -Recreate
```

`requirements.txt` 固定了 Pillow 与 lark-oapi。`.gitignore` 已排除 `.venv*`、`config/bot.toml`、`config/secrets.toml`、`var/`、缓存和 `.env`。

```powershell
$env:PYTHONPATH="."
.venv\Scripts\python.exe -m bot --config config/bot.toml demo
```

演示会依次完成：

1. 读取两个本地 fixture 来源；
2. 去重、聚类、评分并选出 3 个选题；
3. 用 fake LLM 生成 1 篇草稿；
4. 渲染 2 张 1080×1440 PNG；
5. 生成本地审批通知；
6. 重启后确认待审批仍在；
7. 批准后通过 mock 发布，结果为 `simulated`（无外部帖子 ID/URL）；
8. 再次重启，确认仍然只有 1 次发布尝试。

产物在 `var/demo/`，不会发送到飞书或小红书。

## 本地审批控制台

```powershell
# 终端 1：本地控制台（仅 127.0.0.1）
.venv\Scripts\python.exe -m bot --config config/bot.toml console

# 终端 2：工作进程
.venv\Scripts\python.exe -m bot --config config/bot.toml worker
```

打开 `http://127.0.0.1:8765`。未设置 `BOT_CONSOLE_TOKEN` 时，首次启动会在终端打印一次性登录口令。控制台登录使用 HttpOnly cookie + CSRF，GET 只读，所有决定走 POST。

也可以用 `start-bot.cmd` / `health-check.cmd` / `stop-bot.cmd` 管理隐藏后台进程。当 `notify.provider=lark` 且应用凭据与 owner 已配置时，默认启动会额外拉起第三个隐藏的飞书长连接网关；`start/stop/health` 对它使用与 worker/console 相同的 PID + 身份核对。首次请在普通 Windows 桌面会话中验证 `var\logs` 和 `var\run`。

## 常用命令

```powershell
$py = ".venv\Scripts\python.exe"
& $py -m bot --config config/bot.toml doctor
& $py -m bot --config config/bot.toml status
& $py -m bot --config config/bot.toml tick
& $py -m bot --config config/bot.toml backup --output var/backups/bot.sqlite3
& $py -m bot --config config/bot.toml approve XH-XXXXXX
& $py -m bot --config config/bot.toml reconcile XH-XXXXXX confirmed_published --post-id ... --url ...
```

`publish-due` 仍经过完整发布闸门；它不会绕过审批。

## 测试

```powershell
$env:PYTHONPATH="."
$env:PYTHONIOENCODING="utf-8"
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

当前 174 个测试全部通过，覆盖审批绑定、异步修改队列、重复回调、并发抢占、事务内配额复检、重启恢复、`publish_unknown`、SSRF、RSS 条件请求、DeepSeek Search/Fetch 预算与来源限制、预算预留/结算、XHS `redId` 身份、飞书通知完整性、live 拒绝 fixture/fake 内容、PNG 布局/分页/预览一致、控制台鉴权、依赖元数据、网关生命周期和进程身份。另有 `tests/test_process_ownership.ps1` 验证后台进程所有权。

新增覆盖：定时采集跳过已结束（或失败）委托选题遗留的 `deepseek-search://topic/...` 动态来源；单个来源的适配器构造失败只记录该来源错误，不再中断整轮采集；模型仅对当前选题一次性建议官方域名（`[topics] model_source_review`，默认关闭），建议不会写入长期可信源，审批消息会明确标注“来源范围说明”。

## 配置与真实账号

- 离线演示：`config/bot.toml`（fixture 来源、fake LLM、mock 发布）。
- live 示例：`config/bot.example.toml`。复制后填账号，但不要直接打开真实发布。
- 密钥只放环境变量或本地 `config/secrets.toml`（不要提交）：
  - `DEEPSEEK_API_KEY`
  - `LARK_APP_ID` / `LARK_APP_SECRET` / `LARK_OWNER_OPEN_ID`
  - `XHS_MCP_AUTH_TOKEN` 或 `XHS_AUTH_TOKEN`
  - 可选 `BOT_CONSOLE_TOKEN`
- 可选 `[topics] model_source_review = true`：当配置的可信域名没有命中时，模型可为**当前这一条**委托选题建议最多 3 个官方域名；代码会校验并只用于本次补充搜索，不会写入长期可信源，也不会自动扩张以后选题的权限。审批消息会显示“来源范围说明”。默认关闭；live 配置已显式开启。
- 小红书真实发布还要求 `publisher.xiaohongshu.expected_red_id`（或环境变量 `XHS_EXPECTED_RED_ID`）等于桥接 `/api/v1/user/me` 的 `data.data.userBasicInfo.redId`。这是公开账号号，不是不可变内部 `user_id`；`xhs-main` 这类本地别名不算。发布前会先检查登录状态和当前账号身份，不匹配/无法解析/只有猜测字段时会在 POST 前拒绝；文档中只有 `status`、没有 note id/URL 的发布回执仍保持 `publish_unknown`。

真实发布必须同时满足：`mode=live`、`publisher.provider=xiaohongshu`、`publisher.live_enabled=true`、`BOT_ALLOW_REAL_PUBLISH=1`，且不是 fixture/fake 演示管线；然后还需要对具体最终版本的一次 owner 批准。缺少任何一项都不会真实发布。飞书手机批准只有在审批通知的最终正文和**全部图片**都确认送达后才生效；未完整送达会显示错误并重试。本地控制台是独立的受认证路径。

飞书审批命令：

```text
批准 XH-XXXXXX
拒绝 XH-XXXXXX
修改 XH-XXXXXX 你的修改意见
```

只接受本人（owner open_id）的私聊；群聊、非本人、超时或未回复都不会发布。修改正文/图片/账号/计划时间会使审批失效。飞书/CLI 的“修改”只写入持久 `revise` 队列并立即返回结果，由 worker 在后台调用模型生成新版本；新版本会重新发起审批，不会沿用旧批准。

## 备份与恢复

```powershell
.\backup-bot.cmd

powershell -NoProfile -ExecutionPolicy Bypass -File scripts\restore.ps1 `
  -Backup var\backups\bot-YYYYMMDD-HHMMSS.sqlite3 -Config config/bot.toml -Force
```

恢复会先停本地服务、校验备份、保留当前数据库副本，再恢复并运行 `doctor`。不要覆盖正在发布中的库。

## 项目结构

```
bot/                 核心代码（worker、console、审批/发布服务、适配器）
config/              离线配置与 live 示例
fixtures/            离线演示来源
tests/               174 个自动化测试 + 进程所有权 PowerShell 回归
scripts/             Windows 启动/停止/健康检查/备份/恢复/setup 脚本
docs/                需求、调研、复核记录与本实现状态
```

实现状态、外部待配置项和真实验证结果见 [docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md)。

## 当前未完成/未验证

- 真实 RSS、DeepSeek、飞书、小红书桥受本机网络和凭据限制，尚未端到端验证；`doctor` 会明确报告缺失项，不会报告为已连接。本轮没有运行付费模型请求。
- 小红书 `/api/v1/user/me` 的实际响应字段未能在本会话核对（GitHub/DNS 被阻止）；实现只接受明确 ID 字段，无法解析就拒绝发布。请在正常网络对照官方 `docs/API.md` 确认。
- 飞书按钮审批未实现，使用文本命令；手机批准要求通知正文和全部图片已完整送达。
- 评论读取/自动回复、平台指标抓取未接入；不伪造指标。
- 本沙箱拒绝 `Win32_Process` CIM 查询，`stop.ps1` 因此按设计 fail closed；普通 Windows 会话可完成真实停止验证。新 venv 的 `ensurepip` 也受沙箱临时目录权限限制，依赖安装需在正常会话执行。
- 发布事务只保证同一 SQLite 数据库内串行化，不是分布式 exactly-once；预算预留是估算，不是硬性货币上限。
- 没有真实发帖、评论、私信，也没有 Git 提交/推送。
