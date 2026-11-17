#requires -Version 5.1
<#
Check configuration, dependencies, database, external credentials and the
local console health endpoint. Read-only: never approves or publishes.

Exit code 0 means no hard failure. Missing optional external accounts are
reported as MISSING/WARN by the doctor and do not count as a hard failure.
#>

# =============================================================================
# 中文导读：体检脚本 = 只读，不动任何状态
# =============================================================================
# 依次检查：
#   1. bot doctor：Python、SQLite、Pillow、字体、配置、数据库、密钥、
#      DeepSeek、来源、通知、飞书、发布器、控制台、owner；
#   2. worker / console / gateway 是否在跑（看 PID 文件 + 身份 + 进程是否存在）；
#   3. 控制台 /healthz 是否能返回 200。
# 退出码：
#   0 = 没有硬故障（外部账号缺失只算 MISSING/WARN，不算硬故障）
#   非 0 = 有硬故障，或服务根本没启动
# 它不会批准、不会发布、不会改数据库。
# =============================================================================

[CmdletBinding()]
param(
    [string]$Config = "config/bot.toml"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
. (Join-Path $PSScriptRoot "process_identity.ps1")

$configPath = if ([System.IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $Root $Config }
if (-not (Test-Path -LiteralPath $configPath)) {
    Write-Error "Config not found: $configPath"
    exit 2
}

$python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { $python = "python" }

# 先跑 Python 侧的 doctor；它的输出本身就是给人看的检查清单。
& $python -m bot --config $configPath doctor
$doctorExit = $LASTEXITCODE

# PID 文件存在 ≠ 进程活着，也不代表这个 PID 还是本项目的进程。
# 与 start/stop 使用同一套身份核对（可执行文件 + 命令行 + 项目根目录）。
$runDir = Join-Path $Root "var\run"
$worker = Test-BotProcessOwnership -PidFile (Join-Path $runDir "worker.pid") -Name "worker" -Root $Root
$console = Test-BotProcessOwnership -PidFile (Join-Path $runDir "console.pid") -Name "console" -Root $Root
$gateway = Test-BotProcessOwnership -PidFile (Join-Path $runDir "gateway.pid") -Name "gateway" -Root $Root
$workerRunning = $worker.Status -eq "running"
$consoleRunning = $console.Status -eq "running"
$gatewayRunning = $gateway.Status -eq "running"
$workerId = $worker.Pid
$consoleId = $console.Pid
$gatewayId = $gateway.Pid

$gatewayStateFile = Join-Path $runDir "gateway.health.json"
& $python -m bot --config $configPath gateway-state --output $gatewayStateFile
if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $gatewayStateFile)) {
    $gatewayState = Get-Content -LiteralPath $gatewayStateFile -Raw | ConvertFrom-Json
} else {
    Write-Host "gateway: configuration state could not be read" -ForegroundColor Yellow
    $gatewayState = [pscustomobject]@{ required = $false; configured = $false; reason = "state unavailable" }
}

Write-Host ""
if ($workerRunning) { Write-Host "worker: running (PID $workerId, identity verified)" }
elseif ($worker.Status -eq "unverified") { Write-Host "worker: unverified ($($worker.Detail))" -ForegroundColor Yellow }
elseif ($worker.Status -eq "mismatch") { Write-Host "worker: PID does not belong to this project ($($worker.Detail))" -ForegroundColor Yellow }
else { Write-Host "worker: not running" }
if ($consoleRunning) { Write-Host "console: running (PID $consoleId, identity verified)" }
elseif ($console.Status -eq "unverified") { Write-Host "console: unverified ($($console.Detail))" -ForegroundColor Yellow }
elseif ($console.Status -eq "mismatch") { Write-Host "console: PID does not belong to this project ($($console.Detail))" -ForegroundColor Yellow }
else { Write-Host "console: not running" }
if ($gatewayRunning) { Write-Host "gateway: running (PID $gatewayId, identity verified)" }
elseif ($gateway.Status -eq "unverified") { Write-Host "gateway: unverified ($($gateway.Detail))" -ForegroundColor Yellow }
elseif ($gateway.Status -eq "mismatch") { Write-Host "gateway: PID does not belong to this project ($($gateway.Detail))" -ForegroundColor Yellow }
elseif ($gatewayState.required -and $gatewayState.configured) { Write-Host "gateway: configured but not running" -ForegroundColor Yellow }
else { Write-Host "gateway: not running ($($gatewayState.reason))" }

$healthExit = 0
if ($consoleRunning) {
    try {
        # Avoid pipe capture here: the DSH sandbox can deny pipe-based child
        # process output. Have Python write the port to a small file instead.
        # 中文：某些受限环境不允许捕获子进程的管道输出，
        # 所以让 Python 把端口号写进临时文件，PowerShell 再读文件。
        $portFile = Join-Path $runDir "console.port"
        & $python -c "import sys; from bot.config import load_config; open(sys.argv[2], 'w', encoding='utf-8').write(str(load_config(sys.argv[1]).get('console.port')))" $configPath $portFile
        if ($LASTEXITCODE -ne 0) { throw "could not read console port from config" }
        $port = (Get-Content -LiteralPath $portFile -Raw).Trim()
        if (-not $port) { $port = "8765" }
        $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 4 "http://127.0.0.1:$port/healthz"
        if ($response.StatusCode -eq 200) {
            Write-Host "console /healthz: HTTP 200"
        } else {
            Write-Host "console /healthz: HTTP $($response.StatusCode)" -ForegroundColor Red
            $healthExit = 1
        }
    } catch {
        Write-Host "console /healthz: unreachable ($($_.Exception.Message))" -ForegroundColor Red
        $healthExit = 1
    }
}

if ($doctorExit -ne 0) { exit $doctorExit }
if ($worker.Status -eq "unverified" -or $console.Status -eq "unverified" -or $gateway.Status -eq "unverified") {
    Write-Host "A PID/identity pair could not be verified; refusing to call this service healthy." -ForegroundColor Yellow
    exit 1
}
if ($gatewayState.required -and $gatewayState.configured -and -not $gatewayRunning) {
    Write-Host "The configured Feishu gateway is not running. Start it with scripts\start.ps1." -ForegroundColor Yellow
    exit 1
}
# 三个服务都没跑：这不是“配置错误”，但也不能算健康，提示用户去启动。
if (-not $workerRunning -and -not $consoleRunning -and -not $gatewayRunning) {
    Write-Host "No local services are running. Start them with scripts\start.ps1." -ForegroundColor Yellow
    exit 1
}
exit $healthExit
