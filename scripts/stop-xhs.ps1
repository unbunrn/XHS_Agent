#requires -Version 5.1
<#
Stop only the Xiaohongshu bridge started by scripts\start-xhs.ps1.

A PID file is not enough.  Before stopping anything, this script verifies the
identity JSON written by scripts/xhs_bridge_launch.py: name, PID, project root,
executable, the complete loopback port argument, and the actual process creation
time.  If CIM is unavailable, creation-time evidence is missing, the PID was
reused, or the identity cannot be verified, the process is left running.
Tokens, cookies and logs are never deleted here.

Parameters:
  -BridgeRoot  state directory (default var\xhs-bridge)
  -DryRun      verify and report without stopping
  -TimeoutSeconds  how long to wait for the process to exit (default 10)
#>

# =============================================================================
# 中文导读：只停“本项目脚本启动的”小红书桥
# =============================================================================
# 为什么不能直接按名字杀 chrome / xiaohongshu-mcp.exe？
# 因为用户可能同时开着别的浏览器或桥接实例。
# 为什么不能只信 PID 文件？
# 因为 Windows 会复用 PID；号可能已经属于别的程序。
#
# 所以这里核对：
#   - bridge.identity.json 中的 name / PID / 项目根目录 / 可执行文件；
#   - 当前进程的真实可执行文件；
#   - 当前进程命令行里是否有完整的 -port 127.0.0.1:<端口> 参数；
#   - 当前进程的真实创建时间是否与身份文件记录一致，防止 PID 被复用。
# 任何一项无法确认，就保留进程和文件，返回非 0，而不是猜。
# =============================================================================

[CmdletBinding()]
param(
    [string]$BridgeRoot = "",
    [switch]$DryRun,
    [int]$TimeoutSeconds = 10
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
. (Join-Path $PSScriptRoot "xhs_bridge_common.ps1")

try {
    if ($TimeoutSeconds -lt 1 -or $TimeoutSeconds -gt 120) {
        throw "TimeoutSeconds must be between 1 and 120."
    }

    $layout = Get-XhsBridgeLayout -Root $Root -BridgeRoot $BridgeRoot
    $ownership = Test-XhsProcessOwnership -PidFile $layout.PidFile `
        -IdentityFile $layout.IdentityFile -Root $Root

    switch ($ownership.Status) {
        "running" {
            if ($DryRun) {
                Write-Host "The bridge would be stopped (PID $($ownership.Pid)); identity verified: $($ownership.Detail)."
                exit 0
            }
            $processId = [int]$ownership.Pid
            Stop-Process -Id $processId -Force
            try {
                Wait-Process -Id $processId -Timeout $TimeoutSeconds -ErrorAction SilentlyContinue
            } catch { }
            $still = Get-Process -Id $processId -ErrorAction SilentlyContinue
            if ($null -ne $still) {
                throw "The bridge (PID $processId) did not stop; its PID/identity files were kept for inspection."
            }
            Remove-XhsStateFiles $layout.PidFile $layout.IdentityFile
            Write-Host "Xiaohongshu bridge stopped (PID $processId); identity verified." -ForegroundColor Green
            Write-Host "Cookies and logs were left in $($layout.BridgeRoot)."
            exit 0
        }
        "no_pid" {
            # No PID means no process to kill.  Remove a possible orphaned
            # identity file only; cookies/logs are preserved.
            Remove-XhsStateFiles $layout.PidFile $layout.IdentityFile
            Write-Host "Xiaohongshu bridge is not running (no PID file)."
            exit 0
        }
        "stale" {
            Remove-XhsStateFiles $layout.PidFile $layout.IdentityFile
            Write-Host "Xiaohongshu bridge was not running; removed stale PID/identity files."
            exit 0
        }
        "unverified" {
            Write-Warning "The bridge was NOT stopped: $($ownership.Detail). PID/identity files were left for inspection."
            Write-Warning "Inspect $($layout.BridgeRoot) and stop the process manually only if you are sure it is this bridge."
            exit 3
        }
        "mismatch" {
            Remove-XhsStateFiles $layout.PidFile $layout.IdentityFile
            Write-Warning "The bridge was NOT stopped: PID $($ownership.Pid) does not match this project ($($ownership.Detail))."
            Write-Warning "The unrelated process was left alone; stale local PID/identity files were removed."
            exit 3
        }
        default {
            Write-Warning "The bridge was NOT stopped: unknown ownership status '$($ownership.Status)'."
            exit 3
        }
    }
} catch {
    Write-Host ""
    Write-Host "Could not stop the Xiaohongshu bridge: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
