# GitHub PR Agent

A self-contained Windows GUI that walks you through contributing to **any** GitHub
repository via a Pull Request — connect, fork, clone, merge files, commit/push,
optionally run locally, and open the PR — with a live activity terminal and error
alerts at the bottom.

**Zero system dependencies.** The runtime uses only the Python standard library +
tkinter, and `git` is provided by a bundled **PortableGit** (see below). Nothing
needs to be pre-installed on the end user's machine.

## The 7 steps

1. **Connect to GitHub** — enter a Personal Access Token (Show toggle), Connect, live status.
2. **Fork the upstream repo** — type any `owner/repo`, click *Fork to my account* (reuses an existing fork).
3. **Clone the fork** — Browse to a parent folder, click *Clone Fork*.
4. **Merge new files** — Browse a source, *Validate File*, set a Branch, and *+ Merge in files*.
   Two selectable modes: **Copy files/folder** into a target subfolder, or **Merge JSON array**
   into a target `.json` (de-duplicated by a key).
5. **Commit & Push** — enter a commit message, click *Commit & Push* (pushes to your fork).
6. **Run / test / demo locally** *(optional, skippable)* — remembers and reuses past run
   commands. PowerShell commands always run with `-ExecutionPolicy Bypass`.
7. **Open the Pull Request** — PR title + body, click *Open Pull Request*.

The bottom **terminal** captures all activity and errors; any error also raises an
OK alert dialog.

## Second tab — Create & Publish Repo

A separate tab publishes a local folder as a brand-new GitHub repo, streaming the
whole process to the same terminal:

1. **Log in** — Personal Access Token (shares the connection with the PR tab).
2. **Create repository** — name, description, Private toggle → creates it via the API (reuses if it already exists).
3. **Push files** — Browse a local folder, set branch + commit message, *Push files* (init/commit/force-push).
4. **Validate files on repo** — fetches the repo's file tree and confirms every local file (excluding `.git`) is present, listing any that are missing.
5. **Open repo in browser**.

Every field except the token is remembered between runs in
`%LOCALAPPDATA%\GitHubPRAgent\config.json` so steps can be reused for re-execution.
The token is kept in memory only, never written to disk or `.git/config`, and
redacted from all log output.

## Run from Python

```
python GitHub_PR_Agent.py
```
or double-click **`GitHub PR Agent.bat`** (no console) / **`Run GitHub PR Agent (debug).bat`** (console).

## Bundle PortableGit (one time, for true zero-dependency)

```
powershell -NoProfile -ExecutionPolicy Bypass -File tools\fetch_portable_git.ps1
```
Downloads the latest 64-bit PortableGit into `vendor\PortableGit`. The app auto-detects
it; if absent, it falls back to any `git` on `PATH`.

## Build a standalone EXE

```
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build_exe.ps1
```
Produces a self-contained one-folder app in `dist\GitHub_PR_Agent` with PortableGit
shipped alongside. Add `-OneFile` for a single EXE.

> **Important:** run the built EXE from a **non-OneDrive** folder (e.g. `C:\Users\<you>\...`)
> to avoid `Bad Image 0xc0e90002` DLL-load failures caused by OneDrive cloud placeholders.

## Publish / update the agent's own repo

```
powershell -NoProfile -ExecutionPolicy Bypass -File tools\submit_repo.ps1 `
    -Token ghp_xxx -Repo github-pr-agent -Message "Release v1.0.0"
```
Creates the GitHub repo if needed, writes `.gitignore`, commits, and pushes. Use this
as the repeatable process for shipping future versions.

## Requirements

- Windows, Python 3.9+ (tkinter is included with the standard python.org installer).
- Build only: `pyinstaller` (installed automatically by `build_exe.ps1`).
- No third-party **runtime** dependencies.
