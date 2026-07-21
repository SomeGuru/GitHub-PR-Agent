# Changelog

All notable changes to **GitHub PR Agent** are recorded here. The version in
`APP_VERSION` (top of `GitHub_PR_Agent.py`) is the source of truth and increments
with every release. Newest entries go on top.

The self-update feature ("Check for updates") compares this local `APP_VERSION`
against `APP_VERSION` in the copy on the default branch of
`https://github.com/SomeGuru/GitHub-PR-Agent` and offers to replace the local
script when the remote is newer.

**Process:** every change-set bumps `APP_VERSION` (patch for fixes/small
additions, minor for features, major for breaking changes) and adds a dated entry
here. Do not batch multiple change-sets under one version.

## v1.4.2 — 2026-07-21

- **Point self-update at the real repo:** `UPDATE_REPO` is now
  `SomeGuru/GitHub-PR-Agent` (was the `someguru` placeholder casing), so "Check
  for updates" fetches the actual published `GitHub_PR_Agent.py`. Updated the
  matching URLs in README and this changelog.

## v1.4.1 — 2026-07-21

- **Fix self-update "Could not write the update: [WinError 2]":** the updater
  assumed `__file__` always pointed at an existing file, so `shutil.copy2` of the
  backup aborted the whole update when the running script path couldn't be
  resolved (odd launch method / moved or cloud-placeholder copy). The updater now
  resolves the running script robustly (tries `__file__`, `sys.argv[0]`, then the
  app base dir), treats the pre-update backup as non-fatal (logs a warning and
  continues), reports the actual target path in any write error, and falls back
  to a `pythonw`/`python` interpreter for the auto-restart.

## v1.4.0 — 2026-07-21

- **"🏗 Build" button (one-click release):** new bottom-bar button opens a Build
  Release dialog that pushes the tag `v{APP_VERSION}` to a target `owner/repo`
  via the GitHub REST API, which triggers `.github/workflows/release.yml` to
  build the Windows/Fedora/Debian executables and publish a GitHub Release — no
  local git clone or manual `git tag` needed. Resolves the repo's default branch
  automatically, remembers the target repo (`build_repo` in config), detects an
  existing tag (with an opt-in "Recreate the tag" checkbox to re-run the build),
  and offers to open the Actions page afterward. Requires a Step 1 connection
  with 'repo' scope.
  - Note: creating the tag ref through the API with a user PAT *does* trigger the
    tag-push workflow (unlike Actions' own `GITHUB_TOKEN`). Tags/commit messages
    alone never trigger it — only a real pushed tag does.

## v1.3.1 — 2026-07-20

- **"Save Activity Window" button:** the bottom-bar "Save config now" button is
  replaced by "💾 Save Activity Window", which copies the activity terminal to
  the clipboard and saves it to a user-chosen `.txt` file.
- **PAT Vault moved up top:** the vault button now sits next to "Load config…"
  in each tab's top toolbar (removed from the bottom bar and the Step 1 rows).
- **Single master login + Fill button:** the vault dialog is streamlined to one
  master passphrase. Unlocking (or saving) enables a dedicated "⬇ Fill Step 1
  PAT" button that pushes the stored token into Step 1; the button stays
  disabled until the correct master passphrase is entered. The reset passphrase
  `MikeLariosWasHere!` still wipes the vault.

## v1.3.0 — 2026-07-20

- **PAT vault (encrypted at rest):** new "🔐 Vault" button (both Step 1 rows and
  the bottom bar) opens a vault dialog that encrypts the Personal Access Token
  to `%LOCALAPPDATA%/GitHubPRAgent/vault.json` under a user-chosen master
  passphrase, and decrypts it back into the Step 1 token field on demand.
  Encryption is standard-library only: PBKDF2-HMAC-SHA256 key derivation
  (200k iterations, random salt) + an HMAC-SHA256 counter-mode stream cipher
  with encrypt-then-MAC authentication (wrong passphrase / tampering is
  detected). The reserved reset passphrase `MikeLariosWasHere!` wipes the vault
  for a forgotten master passphrase — it erases the stored token without
  revealing it. Added `hmac`, `base64`, `secrets`, `hashlib` to the PyInstaller
  spec hidden imports.

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
