#requires -Version 5.1
<#
Start the standalone Xiaohongshu bridge (community xiaohongshu-mcp) for this
project.

Safety:
  * loopback only: the child receives -port 127.0.0.1:<port>;
  * the exact v2.7.2 executable SHA256 is verified before launch, with no
    skip-check switch;
  * the token is loaded by scripts/xhs_bridge_launch.py through
    bot.config.load_config and passed only through the child environment as
    AUTH_TOKEN, never on the command line, never written to a log or identity
    file;
  * CIM process inspection must be available even when no PID file exists;
  * an already-running or unverified bridge is never duplicated or killed;
  * this script does not log in, approve, publish, or send any message.

Parameters:
  -BridgeRoot   state/working directory (default var\xhs-bridge)
  -Port         bridge port (default 18060; keep publisher.xiaohongshu.base_url
                in config/bot.toml in sync)
  -Version      tool directory version (default v2.7.2)
  -BridgeExe    explicit bridge executable (default under var\tools)
  -ShowBrowser  run non-headless (useful for a one-off manual check)
#>

# =============================================================================
# 中文导读：启动本机小红书桥
# =============================================================================
# 普通用户以后需要重启时，只需：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start-xhs.ps1
#
# 启动前会：
#   1. 找到 var\tools\xiaohongshu-mcp\<版本>\xiaohongshu-mcp-windows-amd64.exe；
#   2. 校验该文件的 SHA256（v2.7.2 固定值），不匹配就拒绝执行；
#   3. 确认 CIM 进程查询可用，再检查旧的 PID 文件和身份 JSON；
#   4. 如果端口已被占用，拒绝启动，绝不误杀；
#   5. 由 helper 通过 bot.config.load_config 读取 token，只放进子进程环境；
#   6. 用 scripts\xhs_bridge_launch.py 无窗口启动并记录 PID/身份。
#
# 本脚本不会扫码登录、不会批准、不会发布，也不会调用任何社交平台接口。
# =============================================================================

[CmdletBinding()]
param(
    [string]$BridgeRoot = "",
    [int]$Port = 18060,
    [string]$Version = "v2.7.2",
    [string]$BridgeExe = "",
    [switch]$ShowBrowser
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
. (Join-Path $PSScriptRoot "xhs_bridge_common.ps1")

$ExpectedSha256 = "1f62e1fe137d0293595197b3a76e280e4114293b425e9c4082a334d1f7fb5316"

try {
    if ($Port -lt 1 -or $Port -gt 65535) {
        throw "Port must be between 1 and 65535."
    }

    $layout = Get-XhsBridgeLayout -Root $Root -BridgeRoot $BridgeRoot `
        -Version $Version -BridgeExe $BridgeExe

    Write-Host "Xiaohongshu bridge: $($layout.Executable)"
    if (-not (Test-Path -LiteralPath $layout.Executable)) {
        throw "Bridge executable not found. Download the official Windows release into var\tools\xiaohongshu-mcp\$Version, or pass -BridgeExe. See docs\XHS_BRIDGE_SETUP.md."
    }

    $actualSha256 = (Get-FileHash -LiteralPath $layout.Executable -Algorithm SHA256 -ErrorAction Stop).Hash
    if ($actualSha256 -ne $ExpectedSha256) {
        throw "Bridge executable SHA256 mismatch (expected $ExpectedSha256, got $actualSha256). Refusing to execute an unverified bridge."
    }
    Write-Host "SHA256: verified v2.7.2 executable"

    $cim = Test-XhsCimAccess
    if (-not $cim.Available) {
        throw "$($cim.Detail). Refusing to start without process ownership checks."
    }

    $ownership = Test-XhsProcessOwnership -PidFile $layout.PidFile `
        -IdentityFile $layout.IdentityFile -Root $Root
    switch ($ownership.Status) {
        "running" {
            $recordedPort = 0
            $hasPort = [int]::TryParse([string]$ownership.Identity.port, [ref]$recordedPort)
            if ($hasPort -and $recordedPort -ne $Port) {
                throw "The bridge is already running on port $recordedPort (PID $($ownership.Pid)); refusing to start a second instance on port $Port. Stop it first or use the recorded port."
            }
            Write-Host "The bridge is already running (PID $($ownership.Pid)); identity verified." -ForegroundColor Green
            Write-Host "Health: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\health-xhs.ps1"
            exit 0
        }
        "unverified" {
            throw "The bridge PID/identity cannot be verified ($($ownership.Detail)). Refusing to start a duplicate. Inspect $($layout.BridgeRoot), or stop the process manually if appropriate."
        }
        "mismatch" {
            Write-Warning "The recorded PID does not belong to this bridge ($($ownership.Detail)); leaving that process alone and removing stale local state."
            Remove-XhsStateFiles $layout.PidFile $layout.IdentityFile
        }
        "no_pid" {
            Remove-XhsStateFiles $layout.PidFile $layout.IdentityFile
        }
        "stale" {
            Write-Warning "The recorded bridge process is no longer running; removing stale PID/identity files."
            Remove-XhsStateFiles $layout.PidFile $layout.IdentityFile
        }
        default {
            throw "Unknown bridge ownership status '$($ownership.Status)'; refusing to start."
        }
    }

    # The port check is intentionally before launch.  It protects a bridge
    # started outside these scripts (and any unrelated local service): we do
    # not stop or reuse something we cannot attribute.
    if (Test-XhsTcpPort -HostName $layout.Host -Port $Port -TimeoutMs 700) {
        throw "Port $($layout.Host):$Port is already in use. start-xhs.ps1 never stops or reuses an unverified process. If this is a previously hand-started bridge, close it manually once, then run this script again."
    }

    New-Item -ItemType Directory -Force -Path $layout.BridgeRoot | Out-Null

    $python = Join-Path $Root ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python)) { $python = "python" }

    $helperArgs = @(
        "scripts\xhs_bridge_launch.py",
        "--exe", $layout.Executable,
        "--project-root", $Root,
        "--bridge-root", $layout.BridgeRoot,
        "--pid-file", $layout.PidFile,
        "--identity-file", $layout.IdentityFile,
        "--log-dir", $layout.BridgeRoot,
        "--port", ([string]$Port),
        "--version", $Version
    )
    if ($ShowBrowser) { $helperArgs += "--show-browser" }

    # The helper loads the token through bot.config.load_config and sets it
    # only in the bridge child environment.  Neither this script nor the
    # helper modifies the parent token environment.
    & $python @helperArgs
    $launchExit = $LASTEXITCODE
    if ($launchExit -ne 0) {
        throw "The bridge helper failed (exit $launchExit). Check $($layout.ErrLog) and $($layout.OutLog)."
    }

    $newIdentity = Read-XhsIdentity $layout.IdentityFile
    $newPid = if ($newIdentity) { $newIdentity.pid } else { "unknown" }
    Write-Host ""
    Write-Host "Xiaohongshu bridge started (PID $newPid)." -ForegroundColor Green
    if ($ShowBrowser) {
        Write-Host "Mode: visible browser (-headless=false)."
    } else {
        Write-Host "Mode: headless."
    }
    Write-Host "Logs:   $($layout.ErrLog)"
    Write-Host "Health: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\health-xhs.ps1"
    Write-Host "Stop:   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop-xhs.ps1"
    if (-not $ShowBrowser) {
        Write-Host "The first launch may download/start the bundled browser; run health-xhs.ps1 after a short wait."
    }
    Write-Host "This script did not log in, approve, or publish anything."
    exit 0
} catch {
    Write-Host ""
    Write-Host "Could not start the Xiaohongshu bridge: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
