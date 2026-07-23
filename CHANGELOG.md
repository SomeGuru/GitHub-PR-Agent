# Changelog

All notable changes to GitHub PR Agent are documented here. Newest entries on top.

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
