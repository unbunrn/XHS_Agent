#requires -Version 5.1
<#
Start the local worker, approval console, and optional Feishu gateway in hidden windows.

Safety:
  * This script never approves a draft and never calls a publisher.
  * A real-publishing daemon is refused unless -AllowRealPublishDaemon is given.
  * The worker still goes through the full approval gate for every post.
#>

# =============================================================================
# 中文导读：启动脚本只“拉起进程”，不批准、不发布
# =============================================================================
# 它会启动后台进程：
#   console -> 本机审批控制台（127.0.0.1）
#   worker  -> 抓取/研究/写稿/等待批准/发布
#   gateway -> 可选飞书长连接（notify=lark 且 app/owner 已配置时）
#
# 安全设计：
#   * 本脚本永远不替人批准，也不直接调用发布器；
#   * 如果检测到 BOT_ALLOW_REAL_PUBLISH=1，但没带
#     -AllowRealPublishDaemon，会直接拒绝启动；
#   * 即使带了该开关，worker 仍然要走完整的发布闸门。
# 参数：
#   -Config        配置文件路径（默认 config/bot.toml）
#   -ConsoleOnly   只启动控制台
#   -WorkerOnly    只启动 worker
#   -AllowRealPublishDaemon  明确允许在“真实发布环境变量”下启动 worker
# =============================================================================

[CmdletBinding()]
param(
    [string]$Config = "config/bot.toml",
    [switch]$ConsoleOnly,
    [switch]$WorkerOnly,
    [switch]$GatewayOnly,
    [switch]$AllowRealPublishDaemon
)

$ErrorActionPreference = "Stop"
# 无论从哪个目录双击，都先切到项目根目录，
# 这样 config/bot.toml、.venv、var 等相对路径才找得到。
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
. (Join-Path $PSScriptRoot "process_identity.ps1")

# 三个“只启动一个”的开关不能同时用，否则语义自相矛盾。
$exclusive = @()
if ($ConsoleOnly) { $exclusive += "ConsoleOnly" }
if ($WorkerOnly) { $exclusive += "WorkerOnly" }
if ($GatewayOnly) { $exclusive += "GatewayOnly" }
if ($exclusive.Count -gt 1) {
    throw "Use only one of -ConsoleOnly, -WorkerOnly, -GatewayOnly."
}

# 允许传相对路径或绝对路径；最终统一成绝对路径再交给 Python。
$configPath = if ([System.IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $Root $Config }
if (-not (Test-Path -LiteralPath $configPath)) {
    throw "Config not found: $configPath"
}

# 优先用项目自带的虚拟环境；没装虚拟环境时退回系统 python。
$python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $python = "python"
}

# var\run 存 PID（谁在跑），var\logs 存日志（出了什么事）。
$runDir = Join-Path $Root "var\run"
$logDir = Join-Path $Root "var\logs"
New-Item -ItemType Directory -Force -Path $runDir, $logDir | Out-Null

# Decide whether the Feishu long-connection gateway is part of this deployment.
# The Python helper only reports presence of app credentials/owner; it never
# prints secret values or opens a connection.
$gatewayStateFile = Join-Path $runDir "gateway.state.json"
& $python -m bot --config $configPath gateway-state --output $gatewayStateFile
if ($LASTEXITCODE -ne 0) {
    throw "Could not evaluate gateway configuration (exit $LASTEXITCODE)."
}
$gatewayState = Get-Content -LiteralPath $gatewayStateFile -Raw | ConvertFrom-Json
$gatewayRequired = [bool]$gatewayState.required
$gatewayConfigured = [bool]$gatewayState.configured

function Remove-BotProcessFiles([string]$PidFile) {
    if (Test-Path -LiteralPath $PidFile) { Remove-Item -LiteralPath $PidFile -Force }
    $identity = Get-BotIdentityPath $PidFile
    if (Test-Path -LiteralPath $identity) { Remove-Item -LiteralPath $identity -Force }
}

function Start-BotProcess([string]$Name, [string[]]$BotArgs, [string]$PidFile) {
    # 已经在跑就不重复启动，避免两个 worker 抢同一批任务。
    # 不能只看 PID：必须核对身份文件、可执行文件、命令行和项目根目录。
    $ownership = Test-BotProcessOwnership -PidFile $PidFile -Name $Name -Root $Root
    switch ($ownership.Status) {
        "running" {
            Write-Host "$Name is already running (PID $($ownership.Pid)); identity verified."
            return
        }
        "unverified" {
            throw "$Name PID file cannot be verified ($($ownership.Detail)). Refusing to start a duplicate; inspect var\run and stop the process manually if needed."
        }
        "mismatch" {
            Write-Warning "$Name PID file points at a process that does not match this project: $($ownership.Detail). Removing stale files and starting a new process."
            Remove-BotProcessFiles $PidFile
        }
        default {
            # no_pid / stale: remove old files before launching.
            Remove-BotProcessFiles $PidFile
        }
    }

    # Launch through Python: Start-Process is blocked in some restricted
    # environments, while this helper uses CREATE_NO_WINDOW with stdout/stderr
    # redirected to files. It verifies the child survived its first second.
    # 中文：用 scripts/launch.py 而不是 Start-Process——
    # 某些受限环境会拦截 Start-Process；launch.py 会隐藏窗口、
    # 把输出写进日志，并在启动约 1 秒后确认子进程还活着。
    $launchArgs = @(
        "scripts\launch.py",
        "--config", $configPath,
        "--name", $Name,
        "--log-dir", $logDir,
        "--pid-file", $PidFile,
        "--"
    ) + $BotArgs
    & $python @launchArgs
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed to start. See var\logs\$Name.err.log and var\logs\bot.log"
    }
}

# 真实发布环境变量存在时必须“双重确认”：
# 光有环境变量还不够，还要人在命令行明确加上 -AllowRealPublishDaemon。
# 这是防止“复制粘贴了一个带环境变量的终端，顺手启动”的误操作。
$realPublishRequested = $env:BOT_ALLOW_REAL_PUBLISH -match '^(?i:1|true|yes)$'
if ($realPublishRequested -and -not $AllowRealPublishDaemon) {
    throw "BOT_ALLOW_REAL_PUBLISH is set. Refusing to start a real-publishing daemon without -AllowRealPublishDaemon. Use the offline config for the first run."
}

# 默认启动 console + worker，并在 notify=lark 且 app/owner 已配置时启动网关。
$startConsole = -not $WorkerOnly -and -not $GatewayOnly
$startWorker = -not $ConsoleOnly -and -not $GatewayOnly
$startGateway = $gatewayConfigured -and -not $ConsoleOnly -and -not $WorkerOnly
if ($GatewayOnly) {
    if (-not $gatewayConfigured) {
        throw "Gateway is not configured: $($gatewayState.reason)"
    }
    $startGateway = $true
}
if ($gatewayRequired -and -not $gatewayConfigured) {
    Write-Warning "notify=lark is selected but the gateway is not configured: $($gatewayState.reason). The gateway was not started."
}
if ($startConsole) {
    Start-BotProcess "console" @("console") (Join-Path $runDir "console.pid")
}
if ($startWorker) {
    Start-BotProcess "worker" @("worker") (Join-Path $runDir "worker.pid")
}
if ($startGateway) {
    Start-BotProcess "gateway" @("gateway") (Join-Path $runDir "gateway.pid")
}

Write-Host ""
# 最后告诉人：去哪看、怎么体检、怎么停，以及“没批准就不会发”。
Write-Host "Local preview: http://127.0.0.1:8765 (loopback only; check [console].port in the config)."
Write-Host "Gateway: $($gatewayState.reason)"
Write-Host "Health:  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\health.ps1 -Config `"$Config`""
Write-Host "Stop:    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop.ps1"
Write-Host "The worker will not publish without a specific owner approval."
