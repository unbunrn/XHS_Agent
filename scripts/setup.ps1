#requires -Version 5.1
<#
Create an ordinary Python 3.12 virtual environment and install the pinned
runtime dependencies.  This deliberately does NOT use --system-site-packages:
the project must not depend on a preexisting DSH runtime.

Usage:
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1 -Recreate
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1 -SkipInstall

`-SkipInstall` still creates/validates the venv (useful on an offline machine).
No secrets are read, printed, or written.
#>

[CmdletBinding()]
param(
    [string]$Python = "",
    [string]$Target = ".venv",
    [switch]$Recreate,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# pip's HTTP cache has hung in this environment inside adjacent_tmp_file ->
# tempfile._mkstemp_inner.  Keep temp/cache inside the project and disable the
# download cache; this is process-local and does not touch global settings.
$tempRoot = Join-Path $Root "var\tmp"
$processTemp = Join-Path $tempRoot ("setup-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
$pipCache = Join-Path $Root "var\pip-cache"
New-Item -ItemType Directory -Force -Path $processTemp, $pipCache | Out-Null
$env:TEMP = $processTemp
$env:TMP = $processTemp
$env:PIP_CACHE_DIR = $pipCache
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
$env:PIP_NO_INPUT = "1"

function Resolve-PythonCommand {
    param([string]$Requested)
    if ($Requested) {
        if (-not (Test-Path -LiteralPath $Requested)) {
            throw "Python executable not found: $Requested"
        }
        return (Resolve-Path -LiteralPath $Requested).Path
    }
    $candidates = @()
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    $cmd = Get-Command py -ErrorAction SilentlyContinue
    if ($cmd) {
        # Prefer the launcher's 3.12 selection when available.
        return @($cmd.Source, "-3.12")
    }
    if (-not $candidates) {
        throw "No Python found. Install Python 3.12 and ensure 'python' is on PATH."
    }
    return $candidates[0]
}

$pythonCmd = Resolve-PythonCommand $Python
# Normalize to an array so callers can pass either an exe or exe + launcher args.
if ($pythonCmd -is [string]) {
    $pythonArgs = @()
    $pythonExe = $pythonCmd
} else {
    $pythonExe = $pythonCmd[0]
    $pythonArgs = @($pythonCmd[1..($pythonCmd.Length - 1)])
}

# Verify the interpreter is exactly Python 3.12 before creating anything.
$version = & $pythonExe @pythonArgs -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ($LASTEXITCODE -ne 0 -or $version.Trim() -ne "3.12") {
    throw "Python 3.12 is required; found '$($version.Trim())' from $pythonExe."
}

$targetFull = [System.IO.Path]::GetFullPath((Join-Path $Root $Target))
$rootFull = [System.IO.Path]::GetFullPath($Root)
if (-not $targetFull.StartsWith($rootFull + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to create a venv outside the project root: $targetFull"
}
$leaf = Split-Path -Leaf $targetFull
if ($leaf -notmatch '^\.?venv') {
    throw "Refusing to use a target that is not named .venv*: $targetFull"
}

if (Test-Path -LiteralPath $targetFull) {
    if ($Recreate) {
        Write-Host "Removing existing venv: $targetFull"
        Remove-Item -LiteralPath $targetFull -Recurse -Force
    } else {
        $cfgPath = Join-Path $targetFull "pyvenv.cfg"
        if (-not (Test-Path -LiteralPath $cfgPath)) {
            throw "$targetFull exists but is not a Python venv. Re-run with -Recreate or choose another -Target."
        }
        $cfg = Get-Content -LiteralPath $cfgPath -Raw
        if ($cfg -match '(?im)^\s*include-system-site-packages\s*=\s*true\s*$') {
            throw "$targetFull uses --system-site-packages. Re-run with -Recreate to build an ordinary venv."
        }
        $venvPython = Join-Path $targetFull "Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $venvPython)) {
            throw "venv exists but Scripts\python.exe is missing. Re-run with -Recreate."
        }
        Write-Host "Reusing ordinary venv: $targetFull"
    }
}

if (-not (Test-Path -LiteralPath $targetFull)) {
    Write-Host "Creating ordinary Python 3.12 venv at $targetFull (no --system-site-packages)"
    & $pythonExe @pythonArgs -m venv $targetFull
    if ($LASTEXITCODE -ne 0) {
        $partialPython = Join-Path $targetFull "Scripts\python.exe"
        if (Test-Path -LiteralPath $partialPython) {
            Write-Warning "venv was created, but pip bootstrap failed (exit $LASTEXITCODE). This is common in restricted sandboxes that deny temp-directory writes."
            if (-not $SkipInstall) {
                throw "The venv exists but has no pip. Re-run this setup in a normal user session, or bootstrap pip into $partialPython and install requirements.txt."
            }
        } else {
            throw "Failed to create venv at $targetFull (exit $LASTEXITCODE)"
        }
    }
}

$venvPython = Join-Path $targetFull "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Python venv was not created correctly: $venvPython is missing"
}

# Prove the interpreter is isolated from the base installation.
& $venvPython -c "import sys; assert sys.version_info[:2] == (3, 12); assert sys.prefix != sys.base_prefix; print('venv OK:', sys.prefix)"
if ($LASTEXITCODE -ne 0) {
    throw "The new interpreter is not an isolated Python 3.12 venv."
}

if (-not $SkipInstall) {
    & $venvPython -m pip --version | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "The venv has no working pip. Re-run setup in a normal user session, or bootstrap pip into $venvPython."
    }
    Write-Host "Installing pinned runtime dependencies from requirements.txt"
    # Do not force a pip self-upgrade.  Use a project-local temp/cache and
    # bounded network waits so a cache/temp stall cannot hang forever.
    & $venvPython -m pip install --no-cache-dir --disable-pip-version-check `
        --timeout 30 --retries 3 -r (Join-Path $Root "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "dependency installation failed" }
    Write-Host "Pinned dependencies installed with cache disabled."
} else {
    Write-Host "Skipping dependency installation (-SkipInstall)."
}

# Best-effort cleanup of this process's temp directory only; the pip cache path
# is scoped under var/ and may be reused/deleted by the operator.
Remove-Item -LiteralPath $processTemp -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Setup complete. Use:"
Write-Host "  $venvPython -m bot --config config/bot.toml doctor"
Write-Host "  $venvPython -m unittest discover -s tests -v"
Write-Host "No secrets were read or written."
