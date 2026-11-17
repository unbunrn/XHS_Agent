#requires -Version 5.1
<#
Shared helpers for the standalone Xiaohongshu bridge lifecycle scripts.

This file is dot-sourced by start-xhs.ps1, stop-xhs.ps1 and health-xhs.ps1.
It only reads configuration and process metadata. It never starts, stops,
publishes or logs in by itself.

Design rules:
  * A PID file is a hint, never proof.  Before trusting a process we also
    verify the identity JSON written by scripts/xhs_bridge_launch.py:
    name, PID, project root, executable, the complete loopback port argument,
    and the actual process creation time.
  * If CIM or the identity record is unavailable we fail closed.
  * The bridge token is loaded by scripts/xhs_bridge_launch.py through
    bot.config.load_config and is only placed in the bridge child environment.
    PowerShell never reads, prints or places the token on a command line.
#>

function Get-XhsFullPath {
    param(
        [string]$Path,
        [string]$Base
    )
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $Base $Path))
}

function Get-XhsBridgeLayout {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [string]$BridgeRoot = "",
        [string]$Version = "v2.7.2",
        [string]$BridgeExe = ""
    )

    $Root = [System.IO.Path]::GetFullPath($Root)
    if (-not $BridgeRoot) {
        $BridgeRoot = Join-Path $Root "var\xhs-bridge"
    }
    $BridgeRoot = Get-XhsFullPath -Path $BridgeRoot -Base $Root

    if (-not $BridgeExe) {
        $BridgeExe = Join-Path $Root "var\tools\xiaohongshu-mcp\$Version\xiaohongshu-mcp-windows-amd64.exe"
    }
    $BridgeExe = Get-XhsFullPath -Path $BridgeExe -Base $Root

    return [pscustomobject]@{
        Root           = $Root
        BridgeRoot     = $BridgeRoot
        Executable     = $BridgeExe
        Version        = $Version
        PidFile        = (Join-Path $BridgeRoot "bridge.pid")
        IdentityFile   = (Join-Path $BridgeRoot "bridge.identity.json")
        LegacyIdentity = (Join-Path $BridgeRoot "bridge-process.json")
        OutLog         = (Join-Path $BridgeRoot "bridge.out.log")
        ErrLog         = (Join-Path $BridgeRoot "bridge.err.log")
        Cookies        = (Join-Path $BridgeRoot "cookies.json")
        Host           = "127.0.0.1"
    }
}

function Get-XhsPidFromFile {
    param([string]$PidFile)
    if (-not (Test-Path -LiteralPath $PidFile)) { return $null }
    $raw = Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $raw) { return $null }
    $parsed = 0
    if ([int]::TryParse(([string]$raw).Trim(), [ref]$parsed) -and $parsed -gt 0) {
        return $parsed
    }
    return $null
}

function Read-XhsIdentity {
    param([string]$IdentityFile)
    if (-not (Test-Path -LiteralPath $IdentityFile)) { return $null }
    try {
        $raw = Get-Content -LiteralPath $IdentityFile -Raw -Encoding UTF8 -ErrorAction Stop
        $obj = $raw | ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $obj) { return $null }
        return $obj
    } catch {
        return $null
    }
}

function Normalize-XhsPath {
    param([string]$Path)
    if (-not $Path) { return "" }
    try {
        return [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/').ToLowerInvariant().Replace('/', '\')
    } catch {
        return $Path.TrimEnd('\', '/').ToLowerInvariant().Replace('/', '\')
    }
}

function Get-XhsProcessCreationTicks {
    param($Process)
    if ($null -eq $Process) { return $null }
    try {
        # System.Diagnostics.Process.StartTime is populated from the Windows
        # GetProcessTimes API, so it matches the launcher's recorded ticks.
        return $Process.StartTime.ToUniversalTime().ToFileTimeUtc()
    } catch {
        return $null
    }
}

function Test-XhsCimAccess {
    param([int]$ProbeProcessId = $PID)

    try {
        $probe = Get-CimInstance Win32_Process -Filter "ProcessId=$ProbeProcessId" -ErrorAction Stop
    } catch {
        return [pscustomobject]@{
            Available = $false
            Detail    = "CIM process inspection unavailable; refusing to start without ownership checks"
        }
    }
    if ($null -eq $probe) {
        return [pscustomobject]@{
            Available = $false
            Detail    = "CIM process inspection returned no data; refusing to start without ownership checks"
        }
    }
    return [pscustomobject]@{ Available = $true; Detail = "CIM process inspection available" }
}

function Compare-XhsIdentityToProcess {
    param(
        $Identity,
        [string]$ActualExecutable,
        [string]$CommandLine,
        $ActualCreationTimeTicks = $null,
        [string]$Root,
        [int]$ProcessId
    )

    if ($null -eq $Identity) {
        return [pscustomobject]@{ Status = "unverified"; Detail = "identity file missing or unreadable" }
    }
    if ([string]($Identity.name) -ne "xhs-bridge") {
        return [pscustomobject]@{ Status = "mismatch"; Detail = "identity name '$($Identity.name)' does not match 'xhs-bridge'" }
    }
    if ($null -eq $Identity.pid) {
        return [pscustomobject]@{ Status = "unverified"; Detail = "identity file has no PID" }
    }
    try {
        $identityPid = [int]($Identity.pid)
    } catch {
        return [pscustomobject]@{ Status = "unverified"; Detail = "identity PID is not a number" }
    }
    if ($identityPid -ne $ProcessId) {
        return [pscustomobject]@{ Status = "mismatch"; Detail = "identity PID $identityPid does not match PID file $ProcessId" }
    }

    $expectedRoot = Normalize-XhsPath $Root
    if ((Normalize-XhsPath ([string]($Identity.project_root))) -ne $expectedRoot) {
        return [pscustomobject]@{ Status = "mismatch"; Detail = "identity project root does not match this project" }
    }

    $expectedExe = Normalize-XhsPath ([string]($Identity.executable))
    if (-not $expectedExe) {
        return [pscustomobject]@{ Status = "unverified"; Detail = "identity file has no executable path" }
    }
    if (-not $ActualExecutable) {
        return [pscustomobject]@{ Status = "unverified"; Detail = "cannot read the live process executable" }
    }
    if ((Normalize-XhsPath $ActualExecutable) -ne $expectedExe) {
        return [pscustomobject]@{ Status = "mismatch"; Detail = "live executable does not match the recorded bridge executable" }
    }

    if ($null -eq $Identity.creation_time_utc_ticks) {
        return [pscustomobject]@{ Status = "unverified"; Detail = "identity file has no process creation time; refusing to trust the PID" }
    }
    try {
        $expectedCreation = [long]($Identity.creation_time_utc_ticks)
    } catch {
        return [pscustomobject]@{ Status = "unverified"; Detail = "identity process creation time is not a number" }
    }
    if ($expectedCreation -le 0) {
        return [pscustomobject]@{ Status = "unverified"; Detail = "identity process creation time is invalid" }
    }
    if ($null -eq $ActualCreationTimeTicks) {
        return [pscustomobject]@{ Status = "unverified"; Detail = "cannot read the live process creation time; refusing to trust the PID" }
    }
    try {
        $actualCreation = [long]$ActualCreationTimeTicks
    } catch {
        return [pscustomobject]@{ Status = "unverified"; Detail = "live process creation time is not a number" }
    }
    if ($actualCreation -le 0) {
        return [pscustomobject]@{ Status = "unverified"; Detail = "live process creation time is invalid; refusing to trust the PID" }
    }
    if ($actualCreation -ne $expectedCreation) {
        return [pscustomobject]@{ Status = "mismatch"; Detail = "live process creation time does not match the recorded bridge; the PID may have been reused" }
    }

    if ($null -eq $Identity.port) {
        return [pscustomobject]@{ Status = "unverified"; Detail = "identity file has no bridge port" }
    }
    try {
        $identityPort = [int]($Identity.port)
    } catch {
        return [pscustomobject]@{ Status = "unverified"; Detail = "identity bridge port is not a number" }
    }
    if ($identityPort -lt 1 -or $identityPort -gt 65535) {
        return [pscustomobject]@{ Status = "unverified"; Detail = "identity bridge port is invalid" }
    }
    $expectedPortArgument = [string]($Identity.port_argument)
    if (-not $expectedPortArgument) {
        return [pscustomobject]@{ Status = "unverified"; Detail = "identity file has no expected loopback port argument" }
    }
    $canonicalPortArgument = "127.0.0.1:$identityPort"
    if ($expectedPortArgument -ne $canonicalPortArgument) {
        return [pscustomobject]@{ Status = "mismatch"; Detail = "identity port argument '$expectedPortArgument' is not the expected loopback argument '$canonicalPortArgument'" }
    }

    if (-not $CommandLine) {
        return [pscustomobject]@{ Status = "unverified"; Detail = "cannot read the live process command line" }
    }
    $commandNormalized = $CommandLine.ToLowerInvariant().Replace('/', '\')
    if (-not $commandNormalized.Contains($expectedExe)) {
        return [pscustomobject]@{ Status = "mismatch"; Detail = "live command line does not contain the recorded bridge executable" }
    }
    $portPattern = '(?i)(?:^|\s)-port(?:\s+|=)["'']?' + [regex]::Escape($canonicalPortArgument) + '(?=$|["''\s])'
    if ($commandNormalized -notmatch $portPattern) {
        return [pscustomobject]@{ Status = "mismatch"; Detail = "live command line does not contain the complete expected '-port $canonicalPortArgument' argument" }
    }
    return [pscustomobject]@{ Status = "running"; Detail = "verified executable, loopback port argument, project root and process creation time" }
}

function Test-XhsProcessOwnership {
    param(
        [string]$PidFile,
        [string]$IdentityFile,
        [string]$Root
    )

    $result = [pscustomobject]@{
        Status       = "unverified"
        Pid          = $null
        Detail       = ""
        Identity     = $null
        Process      = $null
        IdentityFile = $IdentityFile
    }

    $processId = Get-XhsPidFromFile $PidFile
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

    $identity = Read-XhsIdentity $IdentityFile
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
    $actualCreation = Get-XhsProcessCreationTicks -Process $process
    $comparison = Compare-XhsIdentityToProcess -Identity $identity -ActualExecutable $actualExe `
        -CommandLine ([string]$info.CommandLine) -ActualCreationTimeTicks $actualCreation `
        -Root $Root -ProcessId $processId
    $result.Status = $comparison.Status
    $result.Detail = $comparison.Detail
    return $result
}

function Remove-XhsStateFiles {
    param(
        [string]$PidFile,
        [string]$IdentityFile
    )
    foreach ($path in @($PidFile, $IdentityFile)) {
        if ($path -and (Test-Path -LiteralPath $path)) {
            Remove-Item -LiteralPath $path -Force
        }
    }
}

function Test-XhsTcpPort {
    param(
        [string]$HostName = "127.0.0.1",
        [int]$Port,
        [int]$TimeoutMs = 700
    )

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync($HostName, $Port)
        if (-not $task.Wait($TimeoutMs)) { return $false }
        return [bool]$client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}
