#requires -Version 5.1
<#
Optional helper to download OFL-licensed Chinese/mono fonts into assets/fonts/.
No secrets are touched; each downloaded file is SHA-256 verified.
#>

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$FontDir = Join-Path $Root "assets\fonts"
New-Item -ItemType Directory -Force -Path $FontDir | Out-Null

$Fonts = @(
    @{
        Name = "LXGWWenKai-Regular.ttf"
        Url  = "https://github.com/lxgw/LxgwWenKai/releases/download/v1.511/LXGWWenKai-Regular.ttf"
        Sha256 = ""
    },
    @{
        Name = "JetBrainsMono-Regular.ttf"
        Url  = "https://github.com/JetBrains/JetBrainsMono/raw/master/fonts/ttf/JetBrainsMono-Regular.ttf"
        Sha256 = ""
    }
)

foreach ($font in $Fonts) {
    $target = Join-Path $FontDir $font.Name
    if (Test-Path -LiteralPath $target) {
        $hash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
        Write-Host "Exists: $($font.Name) sha256=$hash"
        continue
    }
    Write-Host "Downloading $($font.Name)..."
    Invoke-WebRequest -Uri $font.Url -OutFile $target -UseBasicParsing
    $hash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($font.Sha256 -and ($hash -ne $font.Sha256.ToLowerInvariant())) {
        Remove-Item -LiteralPath $target -Force
        throw "SHA-256 mismatch for $($font.Name): got $hash"
    }
    Write-Host "Saved $($font.Name) sha256=$hash"
}
