#requires -Version 5.1
<#
Regression test for the background process ownership checks.

It starts dummy Python processes (not the bot), writes PID/identity files, and
runs stop.ps1 in -DryRun mode so nothing unrelated is killed.  It also unit-tests
the pure identity comparison, which works even when the DSH sandbox denies
Win32_Process CIM queries.  In that restricted environment the stop script must
fail closed (exit 3) instead of trusting the PID alone.
#>

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
. (Join-Path $Root "scripts\process_identity.ps1")

$python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { $python = (Get-Command python).Source }
$python = (Resolve-Path -LiteralPath $python).Path
$configPath = (Resolve-Path -LiteralPath (Join-Path $Root "config\bot.toml")).Path

$testDir = Join-Path $Root ("var\tests\process-ownership-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force -Path $testDir | Out-Null
$dummy = Join-Path $testDir "dummy.py"
Set-Content -LiteralPath $dummy -Value "import time`ntime.sleep(120)" -Encoding UTF8

function Invoke-StopDryRun {
    $output = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "scripts\stop.ps1") `
        -RunDir $testDir -Name worker -DryRun *>&1 | Out-String
    return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = $output }
}

$proc = $null
try {
    $arguments = @($dummy, "-m", "bot", "--config", $configPath, "worker")
    $proc = Start-Process -FilePath $python -ArgumentList $arguments -PassThru -WindowStyle Hidden
    Start-Sleep -Milliseconds 700
    if ($proc.HasExited) { throw "dummy process exited immediately" }

    $pidFile = Join-Path $testDir "worker.pid"
    Set-Content -LiteralPath $pidFile -Value ([string]$proc.Id) -Encoding ASCII
    $identity = [ordered]@{
        name = "worker"
        pid = $proc.Id
        executable = $python
        command = @($python, $dummy, "-m", "bot", "--config", $configPath, "worker")
        command_line = ('"' + $python + '" "' + $dummy + '" -m bot --config "' + $configPath + '" worker')
        project_root = $Root
        cwd = $Root
        config = $configPath
        started_at = (Get-Date).ToUniversalTime().ToString("o")
    }
    $identity | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $testDir "worker.json") -Encoding UTF8

    # 1) Pure comparison: matching metadata is accepted.
    $ok = Compare-BotIdentityToProcess -Identity ($identity | ConvertTo-Json -Depth 5 | ConvertFrom-Json) `
        -ActualExecutable $python -CommandLine $identity.command_line -Name "worker" -Root $Root -ProcessId $proc.Id
    if ($ok.Status -ne "running") { throw "matching identity was not accepted: $($ok.Detail)" }

    # 2) Unrelated executable is rejected.
    $badExe = Compare-BotIdentityToProcess -Identity ($identity | ConvertTo-Json -Depth 5 | ConvertFrom-Json) `
        -ActualExecutable "C:\Windows\System32\notepad.exe" -CommandLine $identity.command_line -Name "worker" -Root $Root -ProcessId $proc.Id
    if ($badExe.Status -ne "mismatch") { throw "unrelated executable was not rejected: $($badExe.Status)" }

    # 3) A command line that is not this project's bot is rejected.
    $badCmd = Compare-BotIdentityToProcess -Identity ($identity | ConvertTo-Json -Depth 5 | ConvertFrom-Json) `
        -ActualExecutable $python -CommandLine ('"' + $python + '" unrelated.py') -Name "worker" -Root $Root -ProcessId $proc.Id
    if ($badCmd.Status -ne "mismatch") { throw "unrelated command line was not rejected: $($badCmd.Status)" }

    # 4) Integration dry-run.  In a normal session it recognizes the process.
    #    Under the DSH sandbox CIM is denied, so the script must fail closed.
    $result = Invoke-StopDryRun
    if ($proc.HasExited) { throw "dry-run killed the process" }
    if ($result.Output -match "CIM process inspection unavailable") {
        if ($result.ExitCode -ne 3) { throw "CIM-unavailable case should exit 3, got $($result.ExitCode): $($result.Output)" }
    } elseif ($result.Output -match "would be stopped") {
        if ($result.ExitCode -ne 0) { throw "verified dry-run should exit 0, got $($result.ExitCode): $($result.Output)" }
    } else {
        throw "unexpected dry-run output: $($result.Output)"
    }

    # 5) Missing identity must fail closed and leave the process alone.
    Remove-Item -LiteralPath (Join-Path $testDir "worker.json") -Force
    $result = Invoke-StopDryRun
    if ($result.ExitCode -ne 3) { throw "missing identity should exit 3, got $($result.ExitCode): $($result.Output)" }
    if ($result.Output -notmatch "NOT stopped|refusing to trust") { throw "missing identity was not reported: $($result.Output)" }
    if ($proc.HasExited) { throw "missing identity killed the process" }

    Write-Host "process ownership regression: OK"
    exit 0
} finally {
    if ($proc -and -not $proc.HasExited) {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        try { Wait-Process -Id $proc.Id -Timeout 5 -ErrorAction SilentlyContinue } catch { }
    }
    Remove-Item -LiteralPath $testDir -Recurse -Force -ErrorAction SilentlyContinue
}
