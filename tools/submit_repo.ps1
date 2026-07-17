<#
    submit_repo.ps1
    ---------------
    GitHub repository submission script — the repeatable process for publishing
    (or updating) this agent's own source as a GitHub repository. Use it to ship
    new versions of the agent that will be built later on.

    What it does:
      1. Uses the bundled PortableGit (falls back to system git).
      2. Initialises a git repo in the project root if needed.
      3. Writes a sensible .gitignore (excludes vendor/, build/, dist/, config).
      4. Creates the GitHub repo via the REST API if it does not exist.
      5. Commits everything and pushes to <owner>/<repo> on branch <Branch>.

    Usage:
        powershell -NoProfile -ExecutionPolicy Bypass -File tools\submit_repo.ps1 `
            -Token  ghp_xxx `
            -Repo   my-github-pr-agent `
            -Owner  myuser `
            -Message "Release v1.0.0" `
            [-Private] [-Branch main]

    The token is used only for API calls and a transient push URL; it is never
    written to disk or to .git/config.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$Token,
    [Parameter(Mandatory)] [string]$Repo,
    [string]$Owner,
    [string]$Message = "Update GitHub PR Agent",
    [string]$Branch  = "main",
    [switch]$Private
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# --- locate git (prefer bundled PortableGit) -------------------------------
$git = Join-Path $root 'vendor\PortableGit\cmd\git.exe'
if (-not (Test-Path $git)) { $git = (Get-Command git -ErrorAction SilentlyContinue).Source }
if (-not $git) { throw "No git found. Run tools\fetch_portable_git.ps1 first." }

$headers = @{
    Authorization          = "Bearer $Token"
    'User-Agent'           = 'GitHub-PR-Agent-Submit'
    Accept                 = 'application/vnd.github+json'
    'X-GitHub-Api-Version' = '2022-11-28'
}

# --- resolve owner ---------------------------------------------------------
if (-not $Owner) {
    $me = Invoke-RestMethod -Uri 'https://api.github.com/user' -Headers $headers
    $Owner = $me.login
    Write-Host "Authenticated as $Owner" -ForegroundColor Green
}

# --- .gitignore ------------------------------------------------------------
$gitignore = @'
# Bundled/portable + build artifacts (fetched by tools\fetch_portable_git.ps1)
vendor/
build/
dist/
*.spec
__pycache__/
*.pyc
# Local agent state / secrets
config.json
'@
Set-Content -Path (Join-Path $root '.gitignore') -Value $gitignore -Encoding UTF8

# --- ensure repo exists on GitHub -----------------------------------------
$exists = $true
try {
    Invoke-RestMethod -Uri "https://api.github.com/repos/$Owner/$Repo" -Headers $headers | Out-Null
} catch { $exists = $false }

if (-not $exists) {
    Write-Host "Creating repository $Owner/$Repo ..." -ForegroundColor Cyan
    $body = @{ name = $Repo; private = [bool]$Private; auto_init = $false } | ConvertTo-Json
    Invoke-RestMethod -Uri 'https://api.github.com/user/repos' -Headers $headers -Method Post -Body $body -ContentType 'application/json' | Out-Null
} else {
    Write-Host "Repository $Owner/$Repo already exists - updating." -ForegroundColor Green
}

# --- init + commit + push --------------------------------------------------
if (-not (Test-Path (Join-Path $root '.git'))) {
    & $git init | Out-Null
    & $git symbolic-ref HEAD "refs/heads/$Branch"
}
& $git add -A
& $git -c user.name="$Owner" -c user.email="$Owner@users.noreply.github.com" commit -m $Message
# transient authenticated URL (token not persisted to .git/config)
$pushUrl = "https://$Owner`:$Token@github.com/$Owner/$Repo.git"
& $git push $pushUrl "HEAD:$Branch" --force

Write-Host "Submitted to https://github.com/$Owner/$Repo (branch $Branch)" -ForegroundColor Green
