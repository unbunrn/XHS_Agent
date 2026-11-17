#requires -Version 5.1
<#
Shared ownership checks for the background worker/console.

A PID file is only a hint.  Before start/stop/health treats a PID as "ours",
this module verifies the identity JSON written by scripts/launch.py:
  * identity file exists and matches the PID/name/project root;
  * the live process executable matches the recorded executable;
  * the live command line contains "-m bot", the recorded project root,
    the recorded config path, and the expected subcommand (worker/console).

If CIM is unavailable we fail closed ("unverified") rather than killing or
starting based on the PID alone.  No security controls are disabled.
#>

function Get-BotIdentityPath([string]$PidFile) {
    return [System.IO.Path]::ChangeExtension($PidFile, ".json")
}

function Get-BotPidFromFile([string]$PidFile) {
    if (-not (Test-Path -LiteralPath $PidFile)) { return $null }
    $raw = Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $raw) { return $null }
    $parsed = 0
    if ([int]::TryParse([string]$raw, [ref]$parsed) -and $parsed -gt 0) { return $parsed }
    return $null
}

function Read-BotIdentity([string]$PidFile) {
    $path = Get-BotIdentityPath $PidFile
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try {
        $raw = Get-Content -LiteralPath $path -Raw -ErrorAction Stop
        $obj = $raw | ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $obj) { return $null }
        return $obj
    } catch {
        return $null
    }
}

function Normalize-BotPath([string]$Path) {
    if (-not $Path) { return "" }
    try {
        return [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/').ToLowerInvariant().Replace('/', '\')
    } catch {
        return $Path.TrimEnd('\', '/').ToLowerInvariant().Replace('/', '\')
    }
}

function Compare-BotIdentityToProcess {
    param(
        $Identity,
        [string]$ActualExecutable,
        [string]$CommandLine,
        [string]$Name,
        [string]$Root,
        [int]$ProcessId
    )

    if ($null -eq $Identity) {
        return [pscustomobject]@{ Status = "unverified"; Detail = "identity file missing or unreadable" }
    }
    if ([string]($Identity.name) -ne $Name) {
        return [pscustomobject]@{ Status = "mismatch"; Detail = "identity name '$($Identity.name)' does not match '$Name'" }
    }
    if ($null -eq $Identity.pid) {
        return [pscustomobject]@{ Status = "unverified"; Detail = "identity file has no PID" }
    }
    if ([int]($Identity.pid) -ne $ProcessId) {
        return [pscustomobject]@{ Status = "mismatch"; Detail = "identity PID $($Identity.pid) does not match PID file $ProcessId" }
    }
    $expectedRoot = Normalize-BotPath $Root
    if ((Normalize-BotPath ([string]($Identity.project_root))) -ne $expectedRoot) {
        return [pscustomobject]@{ Status = "mismatch"; Detail = "identity project root does not match this project" }
    }
    $expectedExe = Normalize-BotPath ([string]($Identity.executable))
    $expectedConfig = Normalize-BotPath ([string]($Identity.config))
    if (-not $expectedExe -or -not $expectedConfig) {
        return [pscustomobject]@{ Status = "unverified"; Detail = "identity file is incomplete" }
    }
    if (-not $ActualExecutable) {
        return [pscustomobject]@{ Status = "unverified"; Detail = "cannot read the live process executable" }
    }
    if ((Normalize-BotPath $ActualExecutable) -ne $expectedExe) {
        return [pscustomobject]@{ Status = "mismatch"; Detail = "live executable $ActualExecutable does not match recorded $($Identity.executable)" }
    }
    if (-not $CommandLine) {
        return [pscustomobject]@{ Status = "unverified"; Detail = "cannot read the live process command line" }
    }
    $commandNormalized = $CommandLine.ToLowerInvariant().Replace('/', '\')
    if ($commandNormalized -notmatch '(^|\s)-m\s+bot(\s|$)') {
        return [pscustomobject]@{ Status = "mismatch"; Detail = "live command line does not contain '-m bot'" }
    }
    if (-not $commandNormalized.Contains($expectedRoot)) {
        return [pscustomobject]@{ Status = "mismatch"; Detail = "live command line does not contain this project root" }
    }
    if (-not $commandNormalized.Contains($expectedConfig)) {
        return [pscustomobject]@{ Status = "mismatch"; Detail = "live command line does not contain the recorded config path" }
    }
    if ($commandNormalized -notmatch ('(^|\s)' + [regex]::Escape($Name.ToLowerInvariant()) + '(\s|$)')) {
        return [pscustomobject]@{ Status = "mismatch"; Detail = "live command line does not contain the '$Name' subcommand" }
    }
    return [pscustomobject]@{ Status = "running"; Detail = "verified executable, command line and project root" }
}

function Test-BotProcessOwnership {
    param(
        [string]$PidFile,
        [string]$Name,
        [string]$Root
    )

    $result = [pscustomobject]@{
        Status = "unverified"
        Pid = $null
        Detail = ""
        IdentityPath = (Get-BotIdentityPath $PidFile)
        Identity = $null
        Process = $null
    }

    $processId = Get-BotPidFromFile $PidFile
    if (-not $processId) {
        $result.Status = "no_pid"
        $result.Detail = "no valid PID file"
        return $result
    }
    $result.Pid = $processId
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        $result.Status = "stale"
        $result.Detail = "PID $processId is not running"
        return $result
    }
    $result.Process = $process

    $identity = Read-BotIdentity $PidFile
    $result.Identity = $identity
    if ($null -eq $identity) {
        $result.Status = "unverified"
        $result.Detail = "identity file missing or unreadable; refusing to trust the PID alone"
        return $result
    }

    try {
        $info = Get-CimInstance Win32_Process -Filter "ProcessId=$processId" -ErrorAction Stop
    } catch {
        $result.Status = "unverified"
        $result.Detail = "CIM process inspection unavailable; refusing to trust the PID alone"
        return $result
    }
    if ($null -eq $info) {
        $result.Status = "stale"
        $result.Detail = "PID $processId disappeared during verification"
        return $result
    }

    $actualExe = [string]$info.ExecutablePath
    if (-not $actualExe -and $process.Path) { $actualExe = [string]$process.Path }
    $comparison = Compare-BotIdentityToProcess -Identity $identity -ActualExecutable $actualExe `
        -CommandLine ([string]$info.CommandLine) -Name $Name -Root $Root -ProcessId $processId
    $result.Status = $comparison.Status
    $result.Detail = $comparison.Detail
    return $result
}
