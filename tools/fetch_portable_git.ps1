<#
    fetch_portable_git.ps1
    ----------------------
    Downloads the latest 64-bit PortableGit self-extracting archive from the
    git-for-windows releases and extracts it to ..\vendor\PortableGit so the
    agent (and its packaged EXE) has a fully self-contained git — the user's
    machine needs NOTHING pre-installed.

    Usage (from anywhere):
        powershell -NoProfile -ExecutionPolicy Bypass -File tools\fetch_portable_git.ps1
#>
[CmdletBinding()]
param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$root      = Split-Path -Parent $PSScriptRoot
$vendor    = Join-Path $root 'vendor'
$target    = Join-Path $vendor 'PortableGit'
$gitExe    = Join-Path $target 'cmd\git.exe'

if ((Test-Path $gitExe) -and -not $Force) {
    Write-Host "PortableGit already present at $target" -ForegroundColor Green
    & $gitExe --version
    exit 0
}

New-Item -ItemType Directory -Force -Path $vendor | Out-Null

Write-Host "Querying latest git-for-windows release..." -ForegroundColor Cyan
$headers = @{ 'User-Agent' = 'GitHub-PR-Agent-Builder'; 'Accept' = 'application/vnd.github+json' }
$release = Invoke-RestMethod -Uri 'https://api.github.com/repos/git-for-windows/git/releases/latest' -Headers $headers
$asset   = $release.assets | Where-Object { $_.name -match '^PortableGit-.*-64-bit\.7z\.exe$' } | Select-Object -First 1
if (-not $asset) { throw "Could not find a PortableGit 64-bit asset in the latest release." }

$sfx = Join-Path $env:TEMP $asset.name
Write-Host "Downloading $($asset.name) ($([math]::Round($asset.size/1MB,1)) MB)..." -ForegroundColor Cyan
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $sfx -Headers $headers

if (Test-Path $target) { Remove-Item -Recurse -Force $target }
New-Item -ItemType Directory -Force -Path $target | Out-Null

Write-Host "Extracting to $target ..." -ForegroundColor Cyan
# PortableGit ships as a 7-Zip SFX; -y = assume yes, -o = output dir.
& $sfx -y -o"$target" | Out-Null

if (-not (Test-Path $gitExe)) { throw "Extraction finished but $gitExe was not found." }

Remove-Item $sfx -Force -ErrorAction SilentlyContinue
Write-Host "PortableGit ready:" -ForegroundColor Green
& $gitExe --version
