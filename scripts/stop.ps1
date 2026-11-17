#requires -Version 5.1
<#
Stop the local worker, approval console and optional Feishu gateway started by scripts\start.ps1.

A PID file is not enough.  Before stopping anything, this script verifies the
identity JSON written by scripts/launch.py: PID/name/project root, live
executable, and the live command line ("-m bot", project root, config path and
subcommand).  If CIM is unavailable or the identity cannot be verified, the
process is left alone.  Unrelated reused PIDs are never killed.

Parameters used by the regression test:
  -RunDir <path>  directory containing worker.pid/console.pid/gateway.pid (default var\run)
  -Name worker|console|gateway  stop only one service
  -DryRun         verify and report without stopping anything
#>

# =============================================================================
# 中文导读：只停“自己启动的”进程
# =============================================================================
# 为什么不能直接按名字杀 python.exe？
# 因为用户可能同时开着别的 Python 程序，误杀会破坏无关工作。
# 为什么不能只信 PID 文件？
# 因为 Windows 会复用 PID；号可能已经属于别的程序。
# 所以这里同时核对：
#   - launch.py 写下的身份 JSON（PID、名称、项目根目录、配置路径）；
#   - 当前进程的实际可执行文件；
#   - 当前进程的命令行是否包含 "-m bot"、本项目路径和子命令。
# 任何一项无法确认，就拒绝停止（fail closed），而不是猜。
# =============================================================================

[CmdletBinding()]
param(
    [string]$RunDir = "",
    [ValidateSet("worker", "console", "gateway", "")]
    [string]$Name = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
. (Join-Path $PSScriptRoot "process_identity.ps1")

if (-not $RunDir) { $RunDir = Join-Path $Root "var\run" }
$RunDir = [System.IO.Path]::GetFullPath($RunDir)

function Remove-BotProcessFiles([string]$PidFile) {
    if (Test-Path -LiteralPath $PidFile) { Remove-Item -LiteralPath $PidFile -Force }
    $identity = Get-BotIdentityPath $PidFile
    if (Test-Path -LiteralPath $identity) { Remove-Item -LiteralPath $identity -Force }
}

function Stop-BotProcess([string]$ProcessName) {
    $pidFile = Join-Path $RunDir "$ProcessName.pid"
    if (-not (Test-Path -LiteralPath $pidFile)) {
        Write-Host "$ProcessName is not running (no PID file)."
        return
    }

    $ownership = Test-BotProcessOwnership -PidFile $pidFile -Name $ProcessName -Root $Root
    switch ($ownership.Status) {
        "no_pid" {
            # Invalid/empty PID file: remove only local bookkeeping, no process.
            Remove-BotProcessFiles $pidFile
            Write-Host "$ProcessName had an invalid PID file; removed it."
            return
        }
        "stale" {
            Remove-BotProcessFiles $pidFile
            Write-Host "$ProcessName was not running; removed stale PID/identity files."
            return
        }
        "unverified" {
            Write-Warning "$ProcessName was NOT stopped: $($ownership.Detail). PID/identity files were left for inspection."
            $script:Unverified = $true
            return
        }
        "mismatch" {
            # The PID now belongs to something else; never kill it.  Remove the
            # stale bookkeeping so a future start can proceed.
            Remove-BotProcessFiles $pidFile
            Write-Warning "$ProcessName PID $($ownership.Pid) does not match this project ($($ownership.Detail)); left it alone and removed stale PID/identity files."
            return
        }
        "running" {
            if ($DryRun) {
                Write-Host "$ProcessName would be stopped (PID $($ownership.Pid)); identity verified: $($ownership.Detail)."
                return
            }
            Stop-Process -Id $ownership.Pid -Force
            try { Wait-Process -Id $ownership.Pid -Timeout 5 -ErrorAction SilentlyContinue } catch { }
            $still = Get-Process -Id $ownership.Pid -ErrorAction SilentlyContinue
            if ($null -ne $still) {
                throw "$ProcessName (PID $($ownership.Pid)) did not stop; refusing to remove its PID file."
            }
            Remove-BotProcessFiles $pidFile
            Write-Host "$ProcessName stopped (PID $($ownership.Pid)); identity verified."
            return
        }
        default {
            Write-Warning "$ProcessName was NOT stopped: unknown ownership status '$($ownership.Status)'."
            $script:Unverified = $true
            return
        }
    }
}

$script:Unverified = $false
# 先停 worker（它可能在发布），再停 gateway（只接收消息），最后停 console。
# 用显式列表而不是 if，避免脚本解析/作用域差异导致调用被跳过。
$targets = @()
if ($Name) {
    $targets = @($Name)
} else {
    $targets = @("worker", "gateway", "console")
}
foreach ($target in $targets) {
    Stop-BotProcess $target
}

if ($script:Unverified) {
    Write-Host "Some processes could not be verified and were left running. Inspect var\run and stop them manually if appropriate."
    exit 3
}
exit 0
