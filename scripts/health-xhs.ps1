#requires -Version 5.1
<#
Read-only health check for the standalone Xiaohongshu bridge.

It verifies only:
  1. the PID/identity pair belongs to this project (process);
  2. the loopback port is listening (listener);
  3. GET /health on 127.0.0.1 returns HTTP 200 (transport).

It does not call login or account endpoints, does not read a token or account
identity, and never logs in, opens/spawns a browser, approves, publishes, writes
state, or sends content.  A successful check means the local process, listener
and /health transport are healthy; it does not prove the account is logged in,
the browser works, or that publication is ready.

Exit codes:
  0  process verified, loopback listener up, GET /health returned 200
  1  not running, still starting, listener unreachable, or /health not healthy
  3  PID/identity unverified, or the port is owned by an unmanaged process
#>

# =============================================================================
# 中文导读：只读体检
# =============================================================================
# 它不会扫码、不会登录、不会发帖、不会改数据库，也不会写任何文件。
# 先核对进程身份，再检查 127.0.0.1 端口和 GET /health。
# 端口被“来路不明”的进程占用时，报告退出码 3，而不是把它当成自己的桥。
#
# 退出码 0 只代表“进程身份已验证、端口在听、/health 返回 200”，
# 不代表账号已登录，也不代表某篇作品已获批准或已具备发布条件。
# =============================================================================

[CmdletBinding()]
param(
    [string]$BridgeRoot = "",
    [int]$Port = 18060,
    [int]$TimeoutSeconds = 5
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
. (Join-Path $PSScriptRoot "xhs_bridge_common.ps1")

try {
    if ($Port -lt 1 -or $Port -gt 65535) {
        throw "Port must be between 1 and 65535."
    }
    if ($TimeoutSeconds -lt 1 -or $TimeoutSeconds -gt 60) {
        throw "TimeoutSeconds must be between 1 and 60."
    }

    $layout = Get-XhsBridgeLayout -Root $Root -BridgeRoot $BridgeRoot
    $ownership = Test-XhsProcessOwnership -PidFile $layout.PidFile `
        -IdentityFile $layout.IdentityFile -Root $Root
    $portOpen = Test-XhsTcpPort -HostName $layout.Host -Port $Port -TimeoutMs 700

    if ($ownership.Status -eq "running") {
        $recordedPort = 0
        $hasPort = [int]::TryParse([string]$ownership.Identity.port, [ref]$recordedPort)
        if ($hasPort -and $recordedPort -ne $Port) {
            Write-Host "Port mismatch: the verified bridge records port $recordedPort, but -Port is $Port." -ForegroundColor Yellow
            exit 3
        }
        Write-Host "Process: running (PID $($ownership.Pid), identity verified)" -ForegroundColor Green
        if (-not $portOpen) {
            Write-Host "Listener: not listening on 127.0.0.1:$Port yet; the bridge is starting or its listener failed." -ForegroundColor Yellow
            Write-Host "Logs: $($layout.ErrLog)"
            exit 1
        }
        Write-Host "Listener: listening on 127.0.0.1:$Port"
    } elseif ($ownership.Status -eq "unverified" -or $ownership.Status -eq "mismatch") {
        Write-Host "Process: $($ownership.Status) - $($ownership.Detail)" -ForegroundColor Yellow
        if ($portOpen) {
            Write-Host "Port 127.0.0.1:$Port is in use, but it cannot be attributed to a verified bridge; refusing to call it healthy." -ForegroundColor Yellow
        }
        exit 3
    } else {
        Write-Host "Process: not running ($($ownership.Detail))"
        if ($portOpen) {
            Write-Host "Port 127.0.0.1:$Port is in use by an unverified process. It may be a bridge started outside these scripts; this check will not touch it." -ForegroundColor Yellow
            exit 3
        }
        Write-Host "Start it with: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start-xhs.ps1"
        exit 1
    }

    $healthStatus = 0
    $healthError = ""
    try {
        $response = Invoke-WebRequest -UseBasicParsing `
            -Uri ("http://127.0.0.1:{0}/health" -f $Port) `
            -TimeoutSec $TimeoutSeconds -ErrorAction Stop
        $healthStatus = [int]$response.StatusCode
    } catch {
        $exceptionResponse = $_.Exception.Response
        if ($null -ne $exceptionResponse) {
            try { $healthStatus = [int]$exceptionResponse.StatusCode } catch { }
        }
        $healthError = $_.Exception.Message
    }

    if ($healthStatus -ne 200) {
        if ($healthStatus -gt 0) {
            Write-Host "HTTP: GET /health returned HTTP $healthStatus ($healthError); transport is not healthy." -ForegroundColor Red
        } else {
            Write-Host "HTTP: GET /health unreachable on 127.0.0.1:$Port ($healthError); the bridge is starting or its transport failed." -ForegroundColor Red
        }
        Write-Host "Logs: $($layout.ErrLog)"
        exit 1
    }

    Write-Host "HTTP: GET /health returned HTTP 200" -ForegroundColor Green
    Write-Host ""
    Write-Host "Xiaohongshu bridge transport: OK" -ForegroundColor Green
    Write-Host "Process, loopback listener and /health are healthy. This does not prove the account is logged in, the browser works, or that publication is ready."
    Write-Host "This check did not log in, open a browser, approve, or publish."
    exit 0
} catch {
    Write-Host ""
    Write-Host "Xiaohongshu bridge health check failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
