#requires -Version 5.1
<#
Restore a SQLite backup. This is destructive.

Safety:
  * Requires -Force.
  * Stops the local worker and console first.
  * Verifies the backup before touching the live database.
  * Keeps a timestamped copy of the current database.
  * Removes stale -wal/-shm files so the restored file is authoritative.
#>

# =============================================================================
# 中文导读：恢复数据库是破坏性操作，所以处处设卡
# =============================================================================
# 顺序很重要：
#   1. 没带 -Force -> 直接拒绝（防止手滑）；
#   2. 先验证备份文件本身是完好的；
#   3. 从配置里解析出真正的数据库路径；
#   4. 备份路径不能等于当前数据库路径（防止把原库覆盖成自己）；
#   5. 先停服务，再动文件；
#   6. 覆盖前把当前库另存一份 .before-restore-时间戳；
#   7. 删掉旧的 -wal/-shm，避免 SQLite 用旧日志“污染”恢复后的库；
#   8. 最后跑 doctor 让人确认。
# =============================================================================

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Backup,
    [string]$Config = "config/bot.toml",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# 第一道闸：没有 -Force 就什么都不做。
if (-not $Force) {
    throw "Restore is destructive. Re-run with -Force after stopping services and checking the backup path."
}

$configPath = if ([System.IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $Root $Config }
if (-not (Test-Path -LiteralPath $configPath)) { throw "Config not found: $configPath" }

# 相对路径统一按项目根目录解析，避免“以为在别处”。
$backupPath = if ([System.IO.Path]::IsPathRooted($Backup)) { $Backup } else { Join-Path $Root $Backup }
if (-not (Test-Path -LiteralPath $backupPath)) { throw "Backup not found: $backupPath" }

$python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { $python = "python" }

# 第二道闸：先验证备份能通过完整性检查，再去碰现网数据库。
# Use the exit code rather than capturing child output.
& $python -c "import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); r=db.execute('PRAGMA integrity_check').fetchone()[0]; db.close(); print(r); sys.exit(0 if r=='ok' else 1)" $backupPath
if ($LASTEXITCODE -ne 0) { throw "Backup integrity check failed." }

$runDir = Join-Path $Root "var\run"
New-Item -ItemType Directory -Force -Path $runDir | Out-Null
# 同样让 Python 把数据库路径写进文件，绕开受限环境的管道限制。
$dbFile = Join-Path $runDir "restore-dbpath.txt"
& $python -c "import sys; from bot.config import load_config; open(sys.argv[2], 'w', encoding='utf-8').write(str(load_config(sys.argv[1]).db_path))" $configPath $dbFile
if ($LASTEXITCODE -ne 0) { throw "Could not resolve database path from config." }
$dbPath = (Get-Content -LiteralPath $dbFile -Raw).Trim()
if (-not $dbPath) { throw "Could not resolve database path from config." }
# 第三道闸：备份文件不能就是当前数据库本身，
# 否则“恢复”会变成把自己复制到自己，毫无意义还可能损坏文件。
if ([System.IO.Path]::GetFullPath($backupPath) -eq [System.IO.Path]::GetFullPath($dbPath)) {
    throw "Backup path is the live database; choose a different backup file."
}

# 先停服务：数据库被进程占用时覆盖会失败，也可能写坏。
& (Join-Path $PSScriptRoot "stop.ps1")
if ($LASTEXITCODE -ne 0) { throw "stop.ps1 failed; refusing to restore." }

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dbPath) | Out-Null
# 覆盖之前，把当前数据库另存一份“后悔药”。
if (Test-Path -LiteralPath $dbPath) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $safety = "$dbPath.before-restore-$stamp"
    Copy-Item -LiteralPath $dbPath -Destination $safety -Force
    Write-Host "Current database saved as: $safety"
}

# 覆盖主库文件。
Copy-Item -LiteralPath $backupPath -Destination $dbPath -Force
# -wal/-shm 是旧数据库的“未合并日志”。如果留着它们，
# SQLite 下次打开可能用旧日志把刚恢复的内容改回去，所以必须删掉。
foreach ($suffix in @("-wal", "-shm")) {
    $side = "$dbPath$suffix"
    if (Test-Path -LiteralPath $side) { Remove-Item -LiteralPath $side -Force }
}

Write-Host "Restored: $dbPath"
# 恢复后立刻体检；doctor 有问题时用它的退出码返回。
& $python -m bot --config $configPath doctor
$doctorExit = $LASTEXITCODE
Write-Host "Start services again with scripts\start.ps1 after reviewing the doctor output."
exit $doctorExit
