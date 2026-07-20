# Changelog

All notable changes to **GitHub PR Agent** are recorded here. The version in
`APP_VERSION` (top of `GitHub_PR_Agent.py`) is the source of truth and increments
with every release. Newest entries go on top.

The self-update feature ("Check for updates") compares this local `APP_VERSION`
against `APP_VERSION` in the copy on the default branch of
`https://github.com/someguru/GitHub-PR-Agent` and offers to replace the local
script when the remote is newer.

**Process:** every change-set bumps `APP_VERSION` (patch for fixes/small
additions, minor for features, major for breaking changes) and adds a dated entry
here. Do not batch multiple change-sets under one version.

## v1.2.2 — 2026-07-20

- **Fix CI build:** the release workflow (`.github/workflows/release.yml`)
  pointed PyInstaller at a non-existent `src/main.py`, causing "Script file
  'src/main.py' does not exist" and build exit code 1 on all three OS jobs. All
  build steps now target the real entry point `GitHub_PR_Agent.py` at the repo
  root.

## v1.2.1 — 2026-07-20

- **Auto-restart after update:** applying an update now backs up the current
  script, writes the new version, saves config, launches the updated script as a
  detached process, and closes the current window automatically — no manual
  restart. If the relaunch fails, the app warns and asks the user to reopen
  manually instead of exiting silently. (Packaged EXE builds still require a
  rebuild.)

## v1.2.0 — 2026-07-20

- **Per-tab config export/import:** each tab now has **💾 Save this tab…** and
  **📂 Load config…** buttons that write/read a user-chosen JSON file (in addition
  to the automatic all-fields config at `%LOCALAPPDATA%\GitHubPRAgent\config.json`).
  Saved files are tagged with their tab kind so loading applies the right fields;
  loading a matching file repopulates every step to save re-entry time.

## v1.1.2 — 2026-07-20

- **Accurate validation:** the "Validate files on repo" step now compares only
  git-tracked files (via `git ls-files`, respecting `.gitignore`) instead of every
  file on disk, eliminating false "missing" results for ignored files such as
  `__pycache__/*.pyc`.
- **Scaffolded `.gitignore`** now also ignores `vendor/` (bundled tooling) so
  new repos don't publish PortableGit / build output.

## v1.1.1 — 2026-07-20

- **Workflow-scope guard:** when the release agent is enabled, the push is blocked
  early with a clear message if the token lacks the `workflow` scope, and a remote
  rejection for the same reason is translated into actionable guidance.
- **UI hint** under the release checkboxes noting the `workflow` scope requirement.

## v1.1.0 — 2026-07-20

- **Tab order swapped:** *Create & Publish Repo* is now the first tab; *Contribute
  via Pull Request* is second.
- **Release agent (Create tab):** new option to add a GitHub Actions workflow
  (`.github/workflows/release.yml`) that builds and publishes release artifacts on
  tag push, with selectable targets: **Windows 10/11**, **Linux Fedora**, and
  **Linux Debian**.
- **Default project files:** on push, missing `README.md`, `.gitignore`, and a
  `src/` source folder are created automatically (opt-out checkbox).
- **Self-update mechanism:** a *Check for updates* button downloads the latest
  `GitHub_PR_Agent.py` from the update repo, backs up the current script, replaces
  it, and prompts to restart.
- **Version display:** the current version is shown next to the terminal buttons.

## v1.0.0

- Initial standalone, security-reviewed release.
- Tab 1: 7-step *Contribute via Pull Request* workflow (connect, fork, clone,
  merge files, commit/push, run locally, open PR).
- Tab 2: *Create & Publish Repo* (login, create repo, push files, validate, open).
- Zero runtime dependencies (Python stdlib + tkinter; bundled PortableGit).
- Token kept in memory only; all other fields persisted to
  `%LOCALAPPDATA%\GitHubPRAgent\config.json`.
