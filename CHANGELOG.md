# Changelog

All notable changes to GitHub PR Agent are documented here. Newest entries on top.

## v2.3.0 - 2026-07-23

### Changed
- **Release assets are now the raw executables, named after the app** (not generic
  `app-windows.zip` / `app-fedora.tar.gz`). For a repo named `GitHub-PR-Agent` the release
  now contains `GitHub-PR-Agent.exe`, `GitHub-PR-Agent-fedora`, and
  `GitHub-PR-Agent-debian`, downloadable directly with no unzip step.
  - The Windows Python job now builds a single-file `--onefile --windowed` `.exe` and
    uploads it directly; the Linux jobs upload the single-file binaries directly. The
    `Compress-Archive` / `tar` packaging steps were removed for Python builds.
  - `render_workflow` takes an `app_name`, derived from the repository name via the new
    `_product_name()` helper (sanitized to a filesystem-safe token). The chosen name is
    logged before pushing.

## v2.2.1 - 2026-07-23

### Fixed
- **"Publish GitHub Release" step failed** after successful builds. Hardened the generated
  release job: added job-level `permissions: contents: write`, an explicit
  `token: ${{ secrets.GITHUB_TOKEN }}`, `tag_name: ${{ github.ref_name }}`,
  `fail_on_unmatched_files: false`, and `generate_release_notes: true`. Regenerated
  `build.yml`.
- Reminder: if the release step still returns 403 "Resource not accessible by
  integration", set the repo's **Settings > Actions > General > Workflow permissions** to
  **Read and write permissions**. That repo setting caps the token and cannot be set from
  the app or the workflow file.

## v2.2.0 - 2026-07-23

### Added
- **⬆ Push main button** in the activity bar. Force-pushes the selected local folder
  (including `.github/workflows`) to the target repo's `main` branch so `main` always has
  the latest workflow. It preflights `repo` scope and, when a workflow is present,
  `workflow` scope, then reminds you that updating the version tag via **Build** re-runs
  the build (tag builds use the workflow from the tagged commit).

## v2.1.2 - 2026-07-23

### Fixed
- **Debian release build still hit PEP 668 `externally-managed-environment`.** Hardened
  the generated Debian job with a job-level `PIP_BREAK_SYSTEM_PACKAGES: "1"` environment
  variable, so every `pip3` call in the container (requirements and pyinstaller) is
  covered even if an inline flag is missed. Regenerated `build.yml`.
- Note: this failure recurs whenever GitHub runs an **older** copy of the workflow. The
  fixed `build.yml` must be pushed to `main` and the tag must point at a commit that
  contains it (tag builds use the workflow from the tagged commit).

## v2.1.1 - 2026-07-23

### Fixed
- **Debian release build failed at "Install project dependencies"** with PEP 668
  `externally-managed-environment`. The generated Python workflow now passes
  `--break-system-packages` on the Debian `pip3 install -r requirements.txt` step
  (matching the pyinstaller install), so the Debian job and the release publish succeed.

## v2.1.0 - 2026-07-23

Consolidation of the generic publishing-gateway rewrite (Option B) as the canonical
application, plus reliability hardening and a modern Windows 11 style UI.

### Added
- **Windows 11 style theming (stdlib only):** hand-crafted light/dark `ttk` themes with
  accent-colored primary buttons (Connect, Create/reuse repo, Push files, Build). A
  `🌙 Dark / ☀ Light` toggle in the activity bar persists the choice in config.
- **Reliability guardrails for publish -> workflow -> tag -> release:**
  - Real `git` output is now surfaced in push/commit/init/branch errors (last lines),
    with hints about missing `repo` / `workflow` scopes.
  - `_push_build_tag` preflights token scopes (`repo` required; `workflow` required when
    adding a workflow), warns when a tag does not match the `v*` release trigger, and
    reports whether any workflow file exists on the branch before tagging.
  - Connect now logs a scope advisory when `repo` or `workflow` scopes are absent.

### Changed
- Canonical file is now `GitHub_PR_Agent.py` (generic gateway, all WoA-specific logic
  removed). Self-update repo casing fixed to `SomeGuru/GitHub-PR-Agent`.
- The repository's own release workflow is generated as `.github/workflows/build.yml`
  (Windows + Fedora + Debian, release-on-tags) and targets `GitHub_PR_Agent.py`, replacing
  the broken static `release.yml` that referenced the non-existent `src/main.py`.

## v2.0.2 and earlier

Prior iterations of the Option B rewrite (generic multi-language publishing gateway with
git-based push, per-language workflow generation, encrypted PAT vault, one-click Build
tag push, and dual source/EXE self-update). Superseded by v2.1.0.
