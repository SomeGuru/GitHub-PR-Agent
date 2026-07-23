# GitHub PR Agent

A generic GitHub publishing gateway for Windows. Create or reuse a repository, push any
local project, optionally add a matching build workflow, and cut tagged releases — all
without GitHub Desktop. The app is a single Python file that uses only the standard
library plus tkinter.

> Repository: **SomeGuru/GitHub-PR-Agent**

## Highlights

- **Publish any project.** No fixed project name or entry point is assumed. Point it at a
  folder and push.
- **Optional build automation.** Enable it to generate `.github/workflows/build.yml` for
  your project type (Python, C#/.NET, Node, Static site, Go, Rust, Java Maven/Gradle) and
  chosen OS targets (Windows, Linux, macOS, Fedora, Debian).
- **One-click Build.** Push or recreate a version tag to trigger GitHub Actions and, in
  release mode, publish executables as release assets.
- **Encrypted PAT vault.** Store your token at rest with a master passphrase
  (PBKDF2-HMAC-SHA256 + encrypt-then-MAC, stdlib only). Reset phrase wipes the vault
  without revealing the token.
- **Windows 11 style UI.** Light/dark themes with accent-colored actions and a theme
  toggle. No third-party UI dependencies.
- **Self-update.** Source mode updates from the latest `GitHub_PR_Agent.py`; packaged EXE
  mode updates from the latest GitHub Release.

## Requirements

- Windows with Python 3.12+ (tkinter is included with the standard Windows installer).
- Git on `PATH`, or a `vendor/PortableGit` folder next to the app.
- A GitHub Personal Access Token (classic) with `repo` scope, plus `workflow` scope if you
  use build automation or push workflow-triggered releases.

## Run

```powershell
python GitHub_PR_Agent.py
```

## Build an executable

```powershell
pip install pyinstaller
pyinstaller --noconfirm GitHub_PR_Agent.spec
```

The bundled GitHub Actions workflow (`.github/workflows/build.yml`) builds Windows,
Fedora, and Debian binaries and, on a `v*` tag, publishes them to a GitHub Release.

## Releasing

1. Bump `APP_VERSION` and add a `CHANGELOG.md` entry.
2. Commit and push to `main`.
3. Click **Build** in the app (or push a `v{APP_VERSION}` tag) to trigger Actions.

Tags must match `v*` for the release job to run.

## Reliability notes

- Push/commit errors surface the underlying `git` output and hint at missing token scopes.
- Before tagging, the app checks token scopes, warns when a tag will not match `v*`, and
  reports whether a workflow exists on the branch so you know a build will actually run.

## Security

- The token is only kept in memory and, if you opt in, in an encrypted vault file. It is
  redacted from all activity-log output.
- The reset passphrase clears the vault; it never decrypts or displays the stored token.
