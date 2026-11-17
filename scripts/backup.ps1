#requires -Version 5.1
<#
Create a consistent SQLite backup and verify it with PRAGMA integrity_check.
Does not stop services and does not modify the live database.
#>

# =============================================================================
# 中文导读：在线备份 + 自检
# =============================================================================
# 为什么不能直接复制 .sqlite3 文件？
# 因为数据库运行时还有 -wal/-shm 文件，直接复制可能拿到“半截”状态。
# 这里调用 bot 自带的 backup 命令，用 SQLite 的备份接口生成一致快照；
# 然后立刻对备份文件跑 PRAGMA integrity_check，确认它是完好的。
# 整个过程不停服务、不改原库。
# =============================================================================

[CmdletBinding()]
param(
    [string]$Config = "config/bot.toml",
    [string]$Output = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$configPath = if ([System.IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $Root $Config }
if (-not (Test-Path -LiteralPath $configPath)) {
    Write-Error "Config not found: $configPath"
    exit 2
}

$python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { $python = "python" }

# 备份文件名带时间戳，默认写到 var\backups\bot-年月日-时分秒.sqlite3。
# 传了相对路径就按项目根目录解析。
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
if (-not $Output) {
    $Output = Join-Path $Root "var\backups\bot-$stamp.sqlite3"
} elseif (-not [System.IO.Path]::IsPathRooted($Output)) {
    $Output = Join-Path $Root $Output
}

# 目标目录不存在就创建，避免因为“少一层文件夹”导致备份失败。
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Output) | Out-Null

# 真正的备份由 Python 完成；失败就原样返回退出码。
& $python -m bot --config $configPath backup --output $Output
if ($LASTEXITCODE -ne 0) {
    Write-Error "Backup command failed."
    exit $LASTEXITCODE
}
if (-not (Test-Path -LiteralPath $Output)) {
    Write-Error "Backup file was not created: $Output"
    exit 1
}

# Run the integrity check directly (no pipeline capture) and use the exit
# code. This also works when PowerShell cannot capture child process output.
# 中文：用退出码判断，而不是去抓打印结果——受限环境可能不允许管道捕获。
& $python -c "import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); r=db.execute('PRAGMA integrity_check').fetchone()[0]; db.close(); print(r); sys.exit(0 if r=='ok' else 1)" $Output
if ($LASTEXITCODE -ne 0) {
    Write-Error "Backup integrity check failed."
    exit 1
}

$size = (Get-Item -LiteralPath $Output).Length
Write-Host "Backup OK: $Output ($size bytes, integrity=ok)"
