<#
    build_exe.ps1
    -------------
    Packages GitHub_PR_Agent.py into a standalone Windows EXE with PyInstaller.
    PortableGit is shipped ALONGSIDE the EXE (onedir) so the final folder is
    fully self-contained and needs nothing installed on the user's machine.

    Steps performed:
      1. Ensure PortableGit is present (runs fetch_portable_git.ps1 if needed).
      2. pip install pyinstaller (into the current Python) if missing.
      3. Build a one-folder app into .\dist\GitHub_PR_Agent.
      4. Copy .\vendor\PortableGit next to the EXE.

    Usage:
        powershell -NoProfile -ExecutionPolicy Bypass -File tools\build_exe.ps1
#>
[CmdletBinding()]
param(
    [switch]$OneFile   # optional: build a single EXE (PortableGit still shipped beside it)
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 1. PortableGit ------------------------------------------------------------
$gitExe = Join-Path $root 'vendor\PortableGit\cmd\git.exe'
if (-not (Test-Path $gitExe)) {
    Write-Host "PortableGit missing - fetching it first..." -ForegroundColor Yellow
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'fetch_portable_git.ps1')
}

# 2. PyInstaller ------------------------------------------------------------
Write-Host "Ensuring PyInstaller is installed..." -ForegroundColor Cyan
python -m pip show pyinstaller *> $null
if ($LASTEXITCODE -ne 0) { python -m pip install --upgrade pyinstaller }

# 3. Build ------------------------------------------------------------------
$hidden = @('json','re','time','shutil','threading','traceback','webbrowser',
            'subprocess','urllib','urllib.request','urllib.error','pathlib',
            'datetime','tkinter','tkinter.ttk','tkinter.filedialog',
            'tkinter.messagebox','tkinter.scrolledtext')

$args = @('--noconfirm','--clean','--windowed','--name','GitHub_PR_Agent')
foreach ($h in $hidden) { $args += @('--hidden-import', $h) }
if ($OneFile) { $args += '--onefile' }
$args += 'GitHub_PR_Agent.py'

Write-Host "Running PyInstaller..." -ForegroundColor Cyan
python -m PyInstaller @args
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

# 4. Ship PortableGit beside the EXE ---------------------------------------
if ($OneFile) {
    $outDir = Join-Path $root 'dist'
} else {
    $outDir = Join-Path $root 'dist\GitHub_PR_Agent'
}
$destVendor = Join-Path $outDir 'vendor\PortableGit'
Write-Host "Copying PortableGit next to the EXE..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path (Split-Path $destVendor) | Out-Null
if (Test-Path $destVendor) { Remove-Item -Recurse -Force $destVendor }
Copy-Item -Recurse -Force (Join-Path $root 'vendor\PortableGit') $destVendor

Write-Host "Build complete. Self-contained app is in: $outDir" -ForegroundColor Green
Write-Host "IMPORTANT: run the EXE from a NON-OneDrive folder to avoid DLL load errors." -ForegroundColor Yellow
