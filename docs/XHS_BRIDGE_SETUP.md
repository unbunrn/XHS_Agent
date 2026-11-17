# 小红书本机桥生命周期

适用项目：`D:\Dev\Projects\bot`。本文只说明如何启动、停止和体检本机的小红书社区桥（`xpzouying/xiaohongshu-mcp`）；脚本不代替你完成扫码登录，也不会真实发帖或发送消息。

> **当前 Windows 状态**：本机 Windows helper 已被系统安全策略阻断（`leakless.exe`），主控已停止该 Windows 桥。当前没有完成真实账号登录验证，也没有真实发布验证。Docker 替代方案由主控单独、另行授权处理。不要关闭、绕过或降低系统安全保护，也不要把安全软件拦截当作可以跳过的步骤。

## 这套脚本做什么

| 文件 | 作用 |
| --- | --- |
| `scripts/start-xhs.ps1` | 校验固定 SHA256 和 CIM 后无窗口启动桥；拒绝重复实例和已被占用的端口 |
| `scripts/stop-xhs.ps1` | 只停止本项目启动、且 PID/身份/端口参数/创建时间全部核对通过的桥 |
| `scripts/health-xhs.ps1` | 只读体检：进程、回环监听、`GET /health`；不检查登录，也不打开浏览器 |
| `scripts/xhs_bridge_launch.py` | 私有启动 helper；用项目配置加载 token、设置子进程环境、写 PID/身份 JSON、重定向日志 |
| `scripts/xhs_bridge_common.ps1` | 三个 PowerShell 脚本共用的路径、CIM、身份核对函数 |

当前工具版本为 `v2.7.2`，默认位置：

- 桥程序：`var\tools\xiaohongshu-mcp\v2.7.2\xiaohongshu-mcp-windows-amd64.exe`
- 登录程序：`var\tools\xiaohongshu-mcp\v2.7.2\xiaohongshu-login-windows-amd64.exe`
- 工作/状态目录：`var\xhs-bridge`
- 日志：`var\xhs-bridge\bridge.out.log`、`var\xhs-bridge\bridge.err.log`
- 运行状态：`var\xhs-bridge\bridge.pid`、`var\xhs-bridge\bridge.identity.json`
- 子进程状态：`var\xhs-bridge\cookies.json`、`var\xhs-bridge\cache`、`var\xhs-bridge\tmp`

`cookies.json`、日志和 PID/身份文件都不应提交或分享。脚本不会删除 cookies。

## 安全边界

- 桥只监听本机回环地址。启动参数固定为 `-port 127.0.0.1:<端口>`，不是裸 `:<端口>`；该字符串会直接进入上游 `http.Server.Addr`。不要改成 `0.0.0.0`，也不要做公网端口映射。
- 启动前校验 `v2.7.2` 可执行文件的 SHA256 必须等于 `1f62e1fe137d0293595197b3a76e280e4114293b425e9c4082a334d1f7fb5316`。哈希不匹配或无法计算时拒绝执行；没有跳过校验的开关。
- 启动前必须能使用 CIM 查询进程；即使没有旧 PID 文件也要先确认 CIM 可用。CIM 不可用时拒绝启动。
- token 由 `scripts/xhs_bridge_launch.py` 通过项目自己的 `bot.config.load_config(config/bot.toml, root=项目根目录)` 读取。`load_config` 支持环境变量 `XHS_MCP_AUTH_TOKEN` / `XHS_AUTH_TOKEN` 和 `config\secrets.toml` 的 `xiaohongshu_token`，具体优先级以该函数为准；PowerShell 不再用正则解析 TOML。
- token 只进入桥子进程的环境变量 `AUTH_TOKEN`，不会出现在命令行参数、日志或身份 JSON 中。缺少 token 时一律拒绝启动；没有跳过 token 检查的参数。
- 桥子进程的 `COOKIES_PATH`、`LOCALAPPDATA`、`TEMP`、`TMP` 都固定在本项目下，详见下文。
- `start-xhs.ps1` 不扫码、不登录、不批准、不发布、不发消息。
- `health-xhs.ps1` 只对本机回环地址检查进程、监听和 `GET /health`；不读取 token，不调用登录/账号/发布接口，不打开或启动浏览器。
- 这些脚本不会打开真实发布开关。真实发布仍需要项目原有的 live 模式、`publisher.provider=xiaohongshu`、`publisher.live_enabled=true`、`BOT_ALLOW_REAL_PUBLISH=1`、非 demo 管线，以及针对某一版最终正文和全部图片的本人批准。

## Token 与端口

桥和 bot 适配器必须使用同一个 token。推荐在启动本项目的终端里设置：

```powershell
$env:XHS_MCP_AUTH_TOKEN = "<与 bot 相同的 token>"
# 或：$env:XHS_AUTH_TOKEN = "<与 bot 相同的 token>"
```

也可以放在 `config/secrets.toml`（不要提交）：

```toml
xiaohongshu_token = "<与 bot 相同的 token>"
```

不要实际读取、打印或分享真实 secret。脚本只把加载到的值交给桥子进程，不会回显。

默认端口是 `18060`，应与 `config/bot.toml` 保持一致：

```toml
[publisher.xiaohongshu]
base_url = "http://127.0.0.1:18060"
```

### 子进程环境

启动 helper 为桥子进程设置以下路径，并确认它们都位于项目根目录下：

| 变量 | 值 |
| --- | --- |
| `COOKIES_PATH` | `<项目根目录>\var\xhs-bridge\cookies.json` |
| `LOCALAPPDATA` | `<项目根目录>\var\xhs-bridge\cache` |
| `TEMP` | `<项目根目录>\var\xhs-bridge\tmp` |
| `TMP` | `<项目根目录>\var\xhs-bridge\tmp` |

helper 只修改子进程的环境副本，不修改父进程环境。`cache` 和 `tmp` 目录会自动创建。

## 首次登录（当前未完成真实验证）

上游登录程序需要手动扫码。通常把 `var\xhs-bridge` 作为工作目录，让 `cookies.json` 留在该目录：

```powershell
Set-Location D:\Dev\Projects\bot\var\xhs-bridge
..\tools\xiaohongshu-mcp\v2.7.2\xiaohongshu-login-windows-amd64.exe
```

以上游程序的界面和文档为准。不要在聊天、文档或截图里暴露 cookie、token 或二维码。登录完成后关闭登录窗口，再启动桥。

**当前 Windows helper 被系统安全策略阻断（`leakless.exe`）**，因此上述真实登录流程没有完成验证。不要为了让它运行而关闭、绕过或降低安全保护；等待主控对 Docker 替代方案的另行处理。

如果你此前已经手工启动过桥（例如目录里已经有一个 `bridge-process.json`），先确认并关闭那个旧进程，再使用本套脚本。旧进程已经停止，不做迁移；新脚本使用自己的 `bridge.pid` / `bridge.identity.json`，不会读取或改动旧的 `bridge-process.json`。旧格式身份文件缺失所需字段时会保持 fail-closed。

## 启动

在项目根目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start-xhs.ps1
```

启动前会依次检查可执行文件存在、SHA256 固定值、CIM 可用、旧 PID/身份和端口占用。首次启动可能需要初始化/下载内置浏览器，日志里会显示进度；进程先启动、HTTP 稍后才可用是正常的。启动后检查：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\health-xhs.ps1
```

常用参数：

```powershell
# 换端口（同时要改 config/bot.toml 的 base_url）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start-xhs.ps1 -Port 18061

# 覆盖工具版本/程序位置（仍须匹配 v2.7.2 的固定 SHA256，否则拒绝执行）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start-xhs.ps1 -Version v2.7.2
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start-xhs.ps1 -BridgeExe D:\path\to\xiaohongshu-mcp-windows-amd64.exe

# 临时显示浏览器窗口（默认无头）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start-xhs.ps1 -ShowBrowser
```

启动脚本是幂等的：已有一个身份核对通过的桥在运行时，它会报告 PID 并退出 0；身份文件缺失/无法核对时，它会拒绝启动，而不是猜。缺少 token 时 helper 直接拒绝启动。

## 停止

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop-xhs.ps1
```

只停止 `bridge.identity.json` 能证明属于本项目的桥。它会核对名称、PID、项目根目录、可执行文件、命令行中完整的 `-port 127.0.0.1:<端口>` 参数，以及当前进程的真实创建时间（Windows GetProcessTimes）。以下情况会保留进程：

- 身份 JSON 缺失、不可读，或 CIM 进程信息不可用：退出码 `3`；
- 身份文件没有创建时间，或当前进程创建时间无法读取：退出码 `3`，拒绝仅凭 PID 判断；
- PID 已被其他程序复用（创建时间与记录不一致）：不杀该进程，清理本地过期状态后退出码 `3`；
- 进程在超时内没有退出：保留 PID/身份文件，退出码 `1`。

脚本不会按进程名批量杀进程。只看不杀：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop-xhs.ps1 -DryRun
```

## 重启

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop-xhs.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start-xhs.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\health-xhs.ps1
```

桥重启不会自动改变 bot 的工作进程；如果 bot 的发布器也需要重启，仍按项目原有的 `scripts\start.ps1` / `scripts\stop.ps1` 流程操作，两者互不替代。

## 健康检查与退出码

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\health-xhs.ps1
```

如果启动时用了自定义 `-BridgeRoot` 或 `-Port`，体检时带上同样的参数，例如：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\health-xhs.ps1 `
  -Port 18061
```

体检只做三件事：核对进程身份、检查 `127.0.0.1:<端口>` 是否在监听、`GET /health` 是否返回 200。它**不**调用登录状态或账号接口，**不**读取 token/`redId`，**不**打开浏览器。

| 退出码 | 含义 |
| --- | --- |
| `0` | 进程身份已验证、端口在监听、`GET /health` 返回 200；仅代表本机传输健康 |
| `1` | 未运行、仍在启动、监听不可达，或 `/health` 未返回 200 |
| `3` | PID/身份无法核对（含缺少创建时间证据），或端口被无法归属的进程占用 |

`0` 不代表账号已登录，不代表浏览器可用，也不代表真实发布已武装或某篇作品已获批准。健康检查不会验证登录，也不会创建登录会话。

## 排查

- **SHA256 不匹配**：不要启动该文件。重新获取官方 `v2.7.2` 发布包并核对固定哈希；没有跳过校验的参数。
- **端口已占用**：先确认是否有旧桥或别的程序在 `127.0.0.1:18060`。本套脚本不会替你停止来路不明的进程。关闭旧桥后重新运行启动脚本。
- **CIM 不可用**：启动和停止都会 fail-closed。不要为了绕过检查而关闭安全保护；先修复本机 CIM/WMI 权限。
- **健康检查非 200**：这表示本机进程/监听/`/health` 传输有问题，或桥仍在启动。查看 `var\xhs-bridge\bridge.err.log`；健康检查不代表登录状态。
- **未登录**：本套脚本不提供自动登录。当前 Windows 登录 helper 又被系统安全策略阻断，因此没有真实登录验证；不要关闭或绕过安全保护。
- **身份无法核对**：查看 `var\xhs-bridge\bridge.pid` 和 `bridge.identity.json`，确认其中有 `port_argument` 和 `creation_time_utc_ticks`；不要只凭 PID 杀进程。必要时先 `-DryRun` 查看报告。
- **能启动但发布被拒**：检查 `expected_red_id`、`publisher.xiaohongshu.base_url`、真实发布开关和审批状态。发布回执缺少帖子 ID/URL 时会保持 `publish_unknown`，必须人工对账，不能自动重发。

## 当前状态

桥程序已就位，但当前 Windows helper 被系统安全策略阻断（`leakless.exe`），主控已停止该 Windows 桥；真实账号登录、`redId` 核验和真实作品发布都没有完成验证。Docker 替代方案由主控单独处理。本套脚本只补齐本机生命周期和传输体检，不声称已完成账号绑定、登录或发布验证，也不提供绕过系统安全保护的方法。
