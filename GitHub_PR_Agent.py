#!/usr/bin/env python3
"""
GitHub_PR_Agent.py
==================
A self-contained, zero-system-dependency GUI agent that walks a user through the
full "contribute via Pull Request" workflow against ANY GitHub repository:

  Step 1 — Connect to GitHub          (PAT entry, Show toggle, Connect, status)
  Step 2 — Fork the upstream repo      (user-specified owner/repo, Fork button)
  Step 3 — Clone the fork locally       (Browse parent folder, Clone Fork)
  Step 4 — Merge new files into a repo  (Browse source, Validate, Branch,
           subfolder; two modes: generic file/folder copy OR JSON-array merge)
  Step 5 — Commit & Push                (commit message, Commit & Push)
  Step 6 — Run/test/demo locally        (optional/skippable; remembers & reuses
           past run commands; PowerShell always runs with ExecutionPolicy Bypass)
  Step 7 — Open the Pull Request        (PR title, PR body, Open Pull Request)

Bottom panel: a live "GitHub Activity" terminal that captures all activity and
errors. Any error also raises an OK alert dialog.

Zero dependencies
-----------------
* Runtime uses only the Python standard library + tkinter (bundled with Python).
* git is provided by a bundled PortableGit under ./vendor/PortableGit (fetched by
  tools/fetch_portable_git.ps1 and shipped with the packaged EXE). If that is
  missing, the app falls back to any git already on PATH.

State (everything EXCEPT the token) is remembered between runs in
%LOCALAPPDATA%/GitHubPRAgent/config.json so steps can be reused for re-execution.
The Personal Access Token is held in memory only and is never written to disk or
to .git/config, and is redacted from all log output.
"""

# --- stdio guard (must precede any print / tk under pythonw / --windowed) ---
import sys
import os

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

import json
import re
import time
import shutil
import threading
import traceback
import webbrowser
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

APP_NAME = "GitHub PR Agent"
APP_VERSION = "1.0.0"
CONFIG_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "GitHubPRAgent"
CONFIG_FILE = CONFIG_DIR / "config.json"

CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
# Prevent flashing consoles from headless git calls on Windows.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------------------
# git resolution — prefer the bundled PortableGit, fall back to system git
# ---------------------------------------------------------------------------
def app_base_dir() -> Path:
    """Directory that holds the app / EXE (where ./vendor lives)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_git() -> str:
    """Locate a git executable. Bundled PortableGit wins; then system git."""
    override = os.environ.get("GITHUB_PR_AGENT_GIT")
    if override and Path(override).exists():
        return override

    candidates = []
    bases = [app_base_dir()]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bases.append(Path(meipass))
    for base in bases:
        candidates.append(base / "vendor" / "PortableGit" / "cmd" / "git.exe")
        candidates.append(base / "vendor" / "PortableGit" / "bin" / "git.exe")
    for c in candidates:
        if c.exists():
            return str(c)

    which = shutil.which("git")
    if which:
        return which
    # Last resort — let subprocess try the PATH and surface a clear error later.
    return "git"


# ---------------------------------------------------------------------------
class GitHubPRAgent:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(f"{APP_NAME} — v{APP_VERSION}")
        root.geometry("1180x900")
        root.minsize(980, 720)

        # ---- in-memory GitHub state (token NEVER persisted) ----
        self.gh_token = ""
        self.gh_user = ""
        self.gh_fork_full = ""
        self.gh_clone_dir = ""
        self.gh_branch = ""
        self.gh_default_branch = "main"
        self.gh_scopes = None

        # ---- publish-tab state ----
        self.pub_repo_full = ""
        self.pub_repo_url = ""
        self.pub_default_branch = "main"
        self.pub_source_dir = ""
        self.pub_repo_private = False
        self._status_labels = []

        self.git_exe = find_git()

        # ---- persisted configuration ----
        self.cfg = self._load_config()
        self.run_commands = self.cfg.get("run_commands", [])

        self._build_ui()
        self._install_excepthook()
        self.log(f"{APP_NAME} v{APP_VERSION} ready.")
        self.log(f"git: {self.git_exe}")

    # ===== configuration persistence =======================================
    def _load_config(self) -> dict:
        try:
            if CONFIG_FILE.exists():
                return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def save_config(self):
        """Persist all non-secret fields so past executions can be reused."""
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "upstream": self.upstream_var.get().strip(),
                "clone_parent": self.clone_parent_var.get().strip(),
                "clone_dir": self.gh_clone_dir,
                "merge_mode": self.merge_mode.get(),
                "source_path": self.source_var.get().strip(),
                "target_rel": self.target_var.get().strip(),
                "json_key": self.json_key_var.get().strip(),
                "branch": self.branch_var.get().strip(),
                "commit_msg": self.commit_var.get().strip(),
                "run_type": self.run_type.get(),
                "run_cmd": self.run_cmd_var.get().strip(),
                "run_commands": self.run_commands,
                "pr_title": self.pr_title_var.get().strip(),
                "pr_body": self.pr_body.get("1.0", "end").strip(),
                "pub_name": self.pub_name_var.get().strip(),
                "pub_desc": self.pub_desc_var.get().strip(),
                "pub_private": bool(self.pub_private_var.get()),
                "pub_source": self.pub_source_var.get().strip(),
                "pub_branch": self.pub_branch_var.get().strip(),
                "pub_commit": self.pub_commit_var.get().strip(),
            }
            CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            self.log(f"Could not save config: {e}", "WARN")

    # ===== UI ==============================================================
    def _build_ui(self):
        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True)

        # Tabbed step area on top, shared terminal at the bottom.
        paned = ttk.PanedWindow(outer, orient="vertical")
        paned.pack(fill="both", expand=True)

        nb = ttk.Notebook(paned)
        paned.add(nb, weight=3)

        # ---- Tab 1: Contribute via Pull Request ----
        pr_tab = ttk.Frame(nb)
        nb.add(pr_tab, text="Contribute via Pull Request")
        body = self._scrollable(pr_tab)
        intro = ("Contribute to any GitHub repo via Pull Request. Complete the steps top to bottom. "
                 "Your token is kept in memory only; every other field is remembered for reuse.")
        ttk.Label(body, text=intro, wraplength=1120, justify="left").pack(anchor="w", padx=10, pady=(8, 4))
        self._build_step1(body)
        self._build_step2(body)
        self._build_step3(body)
        self._build_step4(body)
        self._build_step5(body)
        self._build_step6(body)
        self._build_step7(body)

        # ---- Tab 2: Create & Publish Repo ----
        pub_tab = ttk.Frame(nb)
        nb.add(pub_tab, text="Create & Publish Repo")
        pbody = self._scrollable(pub_tab)
        self._build_publish_tab(pbody)

        # ---- bottom terminal ----
        term = ttk.LabelFrame(paned, text="GitHub Activity — terminal (captures all activity & errors)")
        paned.add(term, weight=1)
        self.console = scrolledtext.ScrolledText(term, height=12, wrap="word",
                                                 font=("Consolas", 9), bg="#0c0c0c", fg="#d0d0d0")
        self.console.pack(fill="both", expand=True, padx=4, pady=4)
        self.console.tag_config("ERROR", foreground="#ff6b6b")
        self.console.tag_config("WARN", foreground="#ffd166")
        self.console.tag_config("OK", foreground="#7bd88f")
        btns = ttk.Frame(term)
        btns.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Button(btns, text="Clear log", command=lambda: self.console.delete("1.0", "end")).pack(side="left")
        ttk.Button(btns, text="Save config now", command=self.save_config).pack(side="left", padx=6)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _scrollable(self, parent):
        """Return an inner frame inside a vertically scrollable canvas."""
        canvas = tk.Canvas(parent, highlightthickness=0)
        vbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=vbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")

        def _wheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        # Only capture the wheel while the pointer is over this canvas so the two
        # tabs' scroll regions don't fight over one global binding.
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        return inner

    def _build_step1(self, parent):
        s = ttk.LabelFrame(parent, text="Step 1 — Connect to GitHub")
        s.pack(fill="x", padx=10, pady=5)
        row = ttk.Frame(s); row.pack(fill="x", padx=6, pady=6)
        ttk.Label(row, text="Personal Access Token:").pack(side="left")
        self.token_var = tk.StringVar()
        self.token_entry = ttk.Entry(row, textvariable=self.token_var, show="\u2022", width=52)
        self.token_entry.pack(side="left", padx=6)
        self.show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Show PAT", variable=self.show_var,
                        command=self._toggle_token).pack(side="left")
        ttk.Button(row, text="\U0001f517 Connect", command=self._connect).pack(side="left", padx=6)
        self.status_label = ttk.Label(row, text="Not connected", foreground="#a00")
        self.status_label.pack(side="left", padx=8)
        self._status_labels.append(self.status_label)
        ttk.Label(s, foreground="#666",
                  text="Classic token needs 'repo' + 'workflow' scope; fine-grained needs Contents + Pull requests (read/write).").pack(
            anchor="w", padx=6, pady=(0, 4))

    def _build_step2(self, parent):
        s = ttk.LabelFrame(parent, text="Step 2 — Fork the upstream repository")
        s.pack(fill="x", padx=10, pady=5)
        row = ttk.Frame(s); row.pack(fill="x", padx=6, pady=6)
        ttk.Label(row, text="Upstream (owner/repo):").pack(side="left")
        self.upstream_var = tk.StringVar(value=self.cfg.get("upstream", ""))
        ttk.Entry(row, textvariable=self.upstream_var, width=40,
                  font=("Consolas", 9)).pack(side="left", padx=6)
        ttk.Button(row, text="\U0001f374 Fork to my account", command=self._fork).pack(side="left", padx=6)
        self.fork_label = ttk.Label(row, text="", foreground="#060")
        self.fork_label.pack(side="left", padx=8)

    def _build_step3(self, parent):
        s = ttk.LabelFrame(parent, text="Step 3 — Clone the fork locally")
        s.pack(fill="x", padx=10, pady=5)
        row = ttk.Frame(s); row.pack(fill="x", padx=6, pady=6)
        ttk.Label(row, text="Destination parent folder:").pack(side="left")
        self.clone_parent_var = tk.StringVar(value=self.cfg.get("clone_parent", str(Path.home())))
        ttk.Entry(row, textvariable=self.clone_parent_var, width=58).pack(side="left", padx=6)
        ttk.Button(row, text="Browse\u2026", command=self._pick_clone_parent).pack(side="left")
        ttk.Button(row, text="\u2b07 Clone Fork", command=self._clone).pack(side="left", padx=6)
        self.clone_label = ttk.Label(s, text=self.cfg.get("clone_dir", ""), foreground="#666")
        self.clone_label.pack(anchor="w", padx=6, pady=(0, 4))
        # restore a previous clone location so later steps can be reused
        if self.cfg.get("clone_dir") and Path(self.cfg["clone_dir"]).exists():
            self.gh_clone_dir = self.cfg["clone_dir"]

    def _build_step4(self, parent):
        s = ttk.LabelFrame(parent, text="Step 4 — Merge new files into the project folder")
        s.pack(fill="x", padx=10, pady=5)

        mrow = ttk.Frame(s); mrow.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(mrow, text="Mode:").pack(side="left")
        self.merge_mode = tk.StringVar(value=self.cfg.get("merge_mode", "copy"))
        ttk.Radiobutton(mrow, text="Copy files/folder", value="copy",
                        variable=self.merge_mode, command=self._merge_mode_changed).pack(side="left", padx=6)
        ttk.Radiobutton(mrow, text="Merge JSON array", value="json",
                        variable=self.merge_mode, command=self._merge_mode_changed).pack(side="left", padx=6)

        srow = ttk.Frame(s); srow.pack(fill="x", padx=6, pady=2)
        ttk.Label(srow, text="Source:").pack(side="left")
        self.source_var = tk.StringVar(value=self.cfg.get("source_path", ""))
        ttk.Entry(srow, textvariable=self.source_var, width=58).pack(side="left", padx=6)
        ttk.Button(srow, text="Browse file\u2026", command=lambda: self._pick_source(False)).pack(side="left")
        self.pick_folder_btn = ttk.Button(srow, text="Browse folder\u2026",
                                          command=lambda: self._pick_source(True))
        self.pick_folder_btn.pack(side="left", padx=4)
        ttk.Button(srow, text="\u2705 Validate File", command=self._validate).pack(side="left", padx=6)

        trow = ttk.Frame(s); trow.pack(fill="x", padx=6, pady=2)
        self.target_hint = ttk.Label(trow, text="Target folder (relative to repo root):")
        self.target_hint.pack(side="left")
        self.target_var = tk.StringVar(value=self.cfg.get("target_rel", ""))
        ttk.Entry(trow, textvariable=self.target_var, width=42).pack(side="left", padx=6)
        self.json_key_label = ttk.Label(trow, text="Dedupe key:")
        self.json_key_var = tk.StringVar(value=self.cfg.get("json_key", "slug"))
        self.json_key_entry = ttk.Entry(trow, textvariable=self.json_key_var, width=14)

        brow = ttk.Frame(s); brow.pack(fill="x", padx=6, pady=2)
        ttk.Label(brow, text="Branch:").pack(side="left")
        self.branch_var = tk.StringVar(value=self.cfg.get("branch", ""))
        ttk.Entry(brow, textvariable=self.branch_var, width=32).pack(side="left", padx=6)
        ttk.Label(brow, text="(blank = auto-generated)", foreground="#666").pack(side="left")
        ttk.Button(brow, text="\u2795 Merge in files", command=self._merge).pack(side="left", padx=10)

        self._merge_mode_changed()

    def _build_step5(self, parent):
        s = ttk.LabelFrame(parent, text="Step 5 — Commit & Push to your fork")
        s.pack(fill="x", padx=10, pady=5)
        row = ttk.Frame(s); row.pack(fill="x", padx=6, pady=6)
        ttk.Label(row, text="Commit message:").pack(side="left")
        self.commit_var = tk.StringVar(value=self.cfg.get("commit_msg", ""))
        ttk.Entry(row, textvariable=self.commit_var, width=64).pack(side="left", padx=6)
        ttk.Button(row, text="\u2b06 Commit & Push", command=self._commit_push).pack(side="left", padx=6)

    def _build_step6(self, parent):
        s = ttk.LabelFrame(parent, text="Step 6 — Run / test / demo locally (optional, skippable)")
        s.pack(fill="x", padx=10, pady=5)

        rrow = ttk.Frame(s); rrow.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(rrow, text="Reuse a saved command:").pack(side="left")
        self.saved_cmd_var = tk.StringVar()
        self.saved_combo = ttk.Combobox(rrow, textvariable=self.saved_cmd_var, width=60, state="readonly")
        self.saved_combo.pack(side="left", padx=6)
        self.saved_combo.bind("<<ComboboxSelected>>", self._load_saved_command)
        ttk.Button(rrow, text="Delete", command=self._delete_saved_command).pack(side="left")

        trow = ttk.Frame(s); trow.pack(fill="x", padx=6, pady=2)
        ttk.Label(trow, text="Shell:").pack(side="left")
        self.run_type = tk.StringVar(value=self.cfg.get("run_type", "powershell"))
        ttk.Radiobutton(trow, text="PowerShell (ExecutionPolicy Bypass)", value="powershell",
                        variable=self.run_type).pack(side="left", padx=6)
        ttk.Radiobutton(trow, text="cmd", value="cmd", variable=self.run_type).pack(side="left", padx=6)

        crow = ttk.Frame(s); crow.pack(fill="x", padx=6, pady=(2, 6))
        ttk.Label(crow, text="Command:").pack(side="left")
        self.run_cmd_var = tk.StringVar(value=self.cfg.get("run_cmd", "npm install; npm run dev"))
        ttk.Entry(crow, textvariable=self.run_cmd_var, width=64).pack(side="left", padx=6)
        ttk.Button(crow, text="\u25b6 Run locally", command=self._run_local).pack(side="left", padx=6)
        ttk.Button(crow, text="Skip", command=lambda: self.log("Step 6 skipped.")).pack(side="left")

        self._refresh_saved_combo()

    def _build_step7(self, parent):
        s = ttk.LabelFrame(parent, text="Step 7 — Open the Pull Request")
        s.pack(fill="x", padx=10, pady=5)
        r1 = ttk.Frame(s); r1.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(r1, text="PR title:").pack(side="left")
        self.pr_title_var = tk.StringVar(value=self.cfg.get("pr_title", ""))
        ttk.Entry(r1, textvariable=self.pr_title_var, width=80).pack(side="left", padx=6)
        ttk.Label(s, text="PR body:").pack(anchor="w", padx=6)
        self.pr_body = scrolledtext.ScrolledText(s, height=5, wrap="word", font=("Segoe UI", 9))
        self.pr_body.pack(fill="x", padx=6, pady=2)
        self.pr_body.insert("1.0", self.cfg.get("pr_body", ""))
        r3 = ttk.Frame(s); r3.pack(fill="x", padx=6, pady=(2, 8))
        ttk.Button(r3, text="\U0001f680 Open Pull Request", command=self._open_pr).pack(side="left")

    # ===== Tab 2 — Create & Publish Repo ===================================
    def _build_publish_tab(self, parent):
        ttk.Label(parent, wraplength=1120, justify="left",
                  text=("Create a brand-new GitHub repository, push a local folder to it, verify every file "
                        "landed on the repo, then open it in your browser. Progress streams to the terminal "
                        "below.")).pack(anchor="w", padx=10, pady=(8, 4))

        # Step 1 — Login
        s0 = ttk.LabelFrame(parent, text="Step 1 — Log in to GitHub")
        s0.pack(fill="x", padx=10, pady=5)
        row = ttk.Frame(s0); row.pack(fill="x", padx=6, pady=6)
        ttk.Label(row, text="Personal Access Token:").pack(side="left")
        self.pub_token_var = tk.StringVar()
        self.pub_token_entry = ttk.Entry(row, textvariable=self.pub_token_var, show="\u2022", width=52)
        self.pub_token_entry.pack(side="left", padx=6)
        self.pub_show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Show PAT", variable=self.pub_show_var,
                        command=self._pub_toggle_token).pack(side="left")
        ttk.Button(row, text="\U0001f517 Log in", command=self._pub_connect).pack(side="left", padx=6)
        self.pub_status_label = ttk.Label(row, text="Not connected", foreground="#a00")
        self.pub_status_label.pack(side="left", padx=8)
        self._status_labels.append(self.pub_status_label)
        ttk.Label(s0, foreground="#666",
                  text="Shares the connection with the PR tab. Classic token needs the 'repo' scope to create repos.").pack(
            anchor="w", padx=6, pady=(0, 4))

        # Step 2 — Create repository
        s1 = ttk.LabelFrame(parent, text="Step 2 — Create repository")
        s1.pack(fill="x", padx=10, pady=5)
        r1 = ttk.Frame(s1); r1.pack(fill="x", padx=6, pady=4)
        ttk.Label(r1, text="Repo name:").pack(side="left")
        self.pub_name_var = tk.StringVar(value=self.cfg.get("pub_name", ""))
        ttk.Entry(r1, textvariable=self.pub_name_var, width=36).pack(side="left", padx=6)
        self.pub_private_var = tk.BooleanVar(value=self.cfg.get("pub_private", False))
        ttk.Checkbutton(r1, text="Private", variable=self.pub_private_var).pack(side="left", padx=6)
        ttk.Button(r1, text="\u2795 Create repository", command=self._pub_create_repo).pack(side="left", padx=6)
        self.pub_repo_label = ttk.Label(r1, text="", foreground="#060")
        self.pub_repo_label.pack(side="left", padx=8)
        r1b = ttk.Frame(s1); r1b.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(r1b, text="Description:").pack(side="left")
        self.pub_desc_var = tk.StringVar(value=self.cfg.get("pub_desc", ""))
        ttk.Entry(r1b, textvariable=self.pub_desc_var, width=72).pack(side="left", padx=6)

        # Step 3 — Push files
        s2 = ttk.LabelFrame(parent, text="Step 3 — Push files to the repository")
        s2.pack(fill="x", padx=10, pady=5)
        r2 = ttk.Frame(s2); r2.pack(fill="x", padx=6, pady=4)
        ttk.Label(r2, text="Local folder:").pack(side="left")
        self.pub_source_var = tk.StringVar(value=self.cfg.get("pub_source", ""))
        ttk.Entry(r2, textvariable=self.pub_source_var, width=54).pack(side="left", padx=6)
        ttk.Button(r2, text="Browse\u2026", command=self._pub_pick_source).pack(side="left")
        r2b = ttk.Frame(s2); r2b.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(r2b, text="Branch:").pack(side="left")
        self.pub_branch_var = tk.StringVar(value=self.cfg.get("pub_branch", "main"))
        ttk.Entry(r2b, textvariable=self.pub_branch_var, width=20).pack(side="left", padx=6)
        ttk.Label(r2b, text="Commit message:").pack(side="left")
        self.pub_commit_var = tk.StringVar(value=self.cfg.get("pub_commit", "Initial commit"))
        ttk.Entry(r2b, textvariable=self.pub_commit_var, width=44).pack(side="left", padx=6)
        ttk.Button(r2b, text="\u2b06 Push files", command=self._pub_push).pack(side="left", padx=6)

        # Step 4 — Validate
        s3 = ttk.LabelFrame(parent, text="Step 4 — Validate files are present on the repo")
        s3.pack(fill="x", padx=10, pady=5)
        r3 = ttk.Frame(s3); r3.pack(fill="x", padx=6, pady=6)
        ttk.Button(r3, text="\u2705 Validate files on repo", command=self._pub_validate).pack(side="left")
        self.pub_validate_label = ttk.Label(r3, text="", foreground="#666")
        self.pub_validate_label.pack(side="left", padx=8)

        # Step 5 — Open in browser
        s4 = ttk.LabelFrame(parent, text="Step 5 — Open repository in browser")
        s4.pack(fill="x", padx=10, pady=5)
        r4 = ttk.Frame(s4); r4.pack(fill="x", padx=6, pady=6)
        ttk.Button(r4, text="\U0001f310 Open repo in browser", command=self._pub_open_browser).pack(side="left")

    # ===== logging / alerts =================================================
    def log(self, msg, level="INFO"):
        def _append():
            ts = datetime.now().strftime("%H:%M:%S")
            tag = level if level in ("ERROR", "WARN", "OK") else ""
            prefix = f"[{ts}] " + (f"{level}: " if tag else "")
            self.console.insert("end", prefix + str(msg) + "\n", tag)
            self.console.see("end")
        self.root.after(0, _append)

    def alert(self, title, msg, kind="error"):
        msg = str(msg)
        if len(msg) > 1500:
            msg = msg[:1500] + "\n\u2026 (truncated)"
        def _show():
            if kind == "info":
                messagebox.showinfo(title, msg)
            elif kind == "warn":
                messagebox.showwarning(title, msg)
            else:
                messagebox.showerror(title, msg)
        self.root.after(0, _show)

    def _install_excepthook(self):
        def hook(exc_type, exc, tb):
            text = "".join(traceback.format_exception(exc_type, exc, tb))
            self.log(text.strip(), "ERROR")
            self.alert("Unexpected error", str(exc))
        self.root.report_callback_exception = hook

    def _async(self, fn, name="Operation"):
        def runner():
            try:
                fn()
            except Exception as e:
                self.log(f"{name} failed: {e}", "ERROR")
                self.alert(f"{name} failed", str(e))
        threading.Thread(target=runner, daemon=True).start()

    # ===== GitHub REST + git ===============================================
    def _api(self, method, path, data=None):
        if not self.gh_token:
            raise RuntimeError("Not connected — enter a token and click Connect first.")
        url = path if path.startswith("http") else f"https://api.github.com{path}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bearer {self.gh_token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "GitHub-PR-Agent")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                scopes = resp.headers.get("X-OAuth-Scopes")
                if scopes is not None:
                    self.gh_scopes = scopes
                return resp.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            msg = self._clean_api_error(e.code, getattr(e, "reason", ""), raw)
            raise RuntimeError(f"GitHub API {e.code}: {msg}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Network error contacting GitHub: {e.reason}")

    @staticmethod
    def _clean_api_error(code, reason, raw):
        """Turn any error body (JSON, HTML outage page, plain text) into a short line."""
        raw = (raw or "").strip()
        # Normal GitHub API errors are JSON with a "message" field.
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("message"):
                msg = data["message"]
                errs = data.get("errors") or []
                details = "; ".join(
                    (e.get("message") or e.get("code", "")) for e in errs if isinstance(e, dict))
                return f"{msg} ({details})" if details else msg
        except Exception:
            pass
        # Non-JSON body — e.g. GitHub's HTML "unicorn" outage page. Don't dump it.
        if raw.startswith("<") or "<html" in raw[:200].lower():
            friendly = {
                500: "GitHub had an internal server error.",
                502: "GitHub returned a bad gateway.",
                503: "GitHub is temporarily unavailable (service outage).",
                504: "GitHub gateway timed out.",
            }.get(code, "GitHub returned a non-JSON error page.")
            return f"{friendly} Please wait a moment and try again."
        # Plain text — collapse whitespace and cap length.
        text = " ".join(raw.split())
        if not text:
            return str(reason) or "Unknown error"
        return text[:200] + "\u2026" if len(text) > 200 else text

    def _redact(self, text):
        return re.sub(r"//[^@/]+@", "//***@", str(text))

    def _run_git(self, args, cwd=None):
        cmd = [self.git_exe] + args
        shown = " ".join(self._redact(a) if "@github.com" in a else a for a in cmd)
        self.log(f"$ {shown}")
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              creationflags=NO_WINDOW)
        out = (proc.stdout or "") + (proc.stderr or "")
        for line in out.splitlines():
            if line.strip():
                self.log("  " + self._redact(line.rstrip()))
        return proc.returncode, out

    def _auth_url(self):
        return self._auth_url_for(self.gh_fork_full)

    def _auth_url_for(self, full):
        return f"https://{self.gh_user}:{self.gh_token}@github.com/{full}.git"

    def _upstream(self):
        up = self.upstream_var.get().strip()
        if "/" not in up:
            raise RuntimeError("Upstream must be in 'owner/repo' form.")
        owner, repo = up.split("/", 1)
        return owner.strip(), repo.strip()

    # ===== step handlers ===================================================
    def _toggle_token(self):
        self.token_entry.config(show="" if self.show_var.get() else "\u2022")

    def _connect(self):
        self._do_connect(self.token_var.get())

    def _do_connect(self, token):
        token = token.strip()
        if not token:
            self.alert("Connect", "Enter a Personal Access Token first.", "info")
            return
        self.gh_token = token

        def work():
            self.log("Connecting to GitHub \u2026")
            _, user = self._api("GET", "/user")
            self.gh_user = user.get("login", "")
            self.log(f"Connected as {self.gh_user}", "OK")
            if self.gh_scopes is not None:
                shown = self.gh_scopes.strip()
                if shown:
                    self.log(f"Classic token scopes: {shown}")
                    if "repo" not in shown and "public_repo" not in shown:
                        self.log("Warning: token lacks 'repo'/'public_repo' scope — creating "
                                 "repositories and forking will fail. Regenerate a CLASSIC token "
                                 "with the 'repo' scope.", "WARN")
                else:
                    self.log("Fine-grained token detected. To create repos it needs "
                             "Account permission 'Administration' (repo creation) = Read/Write.", "WARN")
            self.root.after(0, self._refresh_status)
        self._async(work, "Connect")

    def _refresh_status(self):
        for lbl in self._status_labels:
            lbl.config(text=f"Connected: {self.gh_user}", foreground="#060")

    def _fork(self):
        def work():
            if not self.gh_user:
                raise RuntimeError("Connect first (Step 1).")
            owner, repo = self._upstream()
            candidate = f"{self.gh_user}/{repo}"
            try:
                s, info = self._api("GET", f"/repos/{candidate}")
                if s == 200 and info.get("fork"):
                    self.gh_fork_full = info.get("full_name", candidate)
                    self.gh_default_branch = info.get("default_branch", "main")
                    self.log(f"Existing fork reused: {self.gh_fork_full}", "OK")
                    self.root.after(0, lambda: self.fork_label.config(text=f"\u2192 {self.gh_fork_full}"))
                    self.save_config()
                    return
            except Exception:
                pass
            self.log(f"Requesting fork of {owner}/{repo} \u2026")
            try:
                _, info = self._api("POST", f"/repos/{owner}/{repo}/forks")
            except RuntimeError as e:
                if "403" in str(e):
                    raise RuntimeError(
                        "Fork refused (403). Use a CLASSIC token with the 'repo' scope, "
                        "or fork it once in the browser and click Fork again to reuse it.")
                raise
            self.gh_fork_full = info.get("full_name", candidate)
            self.gh_default_branch = info.get("default_branch", "main")
            self.log("Waiting for fork to become available \u2026")
            for _ in range(20):
                try:
                    s, _info = self._api("GET", f"/repos/{self.gh_fork_full}")
                    if s == 200:
                        self.gh_default_branch = _info.get("default_branch", self.gh_default_branch)
                        break
                except Exception:
                    pass
                time.sleep(3)
            self.log(f"Fork ready: {self.gh_fork_full} (default branch {self.gh_default_branch})", "OK")
            self.root.after(0, lambda: self.fork_label.config(text=f"\u2192 {self.gh_fork_full}"))
            self.save_config()
        self._async(work, "Fork")

    def _pick_clone_parent(self):
        d = filedialog.askdirectory(title="Choose parent folder for the clone")
        if d:
            self.clone_parent_var.set(d)

    def _clone(self):
        def work():
            if not self.gh_fork_full:
                raise RuntimeError("Fork first (Step 2).")
            _, repo = self._upstream()
            parent = Path(self.clone_parent_var.get().strip())
            dest = parent / repo
            if dest.exists() and any(dest.iterdir()):
                if (dest / ".git").exists():
                    self.log(f"Clone exists at {dest} — fetching latest \u2026")
                    self._run_git(["-C", str(dest), "fetch", "origin"])
                    self.gh_clone_dir = str(dest)
                    self.root.after(0, lambda: self.clone_label.config(text=str(dest)))
                    self.save_config()
                    return
                raise RuntimeError(f"Target exists and is not a git repo: {dest}")
            parent.mkdir(parents=True, exist_ok=True)
            rc, _ = self._run_git(["clone", f"https://github.com/{self.gh_fork_full}.git", str(dest)])
            if rc != 0:
                raise RuntimeError("git clone failed (see terminal).")
            self._run_git(["-C", str(dest), "config", "user.name", self.gh_user])
            self._run_git(["-C", str(dest), "config", "user.email",
                           f"{self.gh_user}@users.noreply.github.com"])
            owner, urepo = self._upstream()
            self._run_git(["-C", str(dest), "remote", "add", "upstream",
                           f"https://github.com/{owner}/{urepo}.git"])
            self.gh_clone_dir = str(dest)
            self.log(f"Cloned to {dest}", "OK")
            self.root.after(0, lambda: self.clone_label.config(text=str(dest)))
            self.save_config()
        self._async(work, "Clone")

    # ---- Step 4 ----
    def _merge_mode_changed(self):
        if self.merge_mode.get() == "json":
            self.target_hint.config(text="Target JSON file (relative to repo root):")
            self.json_key_label.pack(side="left", padx=(10, 2))
            self.json_key_entry.pack(side="left")
            self.pick_folder_btn.state(["disabled"])
        else:
            self.target_hint.config(text="Target folder (relative to repo root):")
            self.json_key_label.pack_forget()
            self.json_key_entry.pack_forget()
            self.pick_folder_btn.state(["!disabled"])

    def _pick_source(self, folder):
        if folder:
            p = filedialog.askdirectory(title="Select source folder to copy")
        else:
            p = filedialog.askopenfilename(title="Select source file")
        if p:
            self.source_var.set(p)

    def _validate(self):
        src = self.source_var.get().strip()
        if not src or not Path(src).exists():
            self.alert("Validate", "Choose a valid source path first.", "info")
            return
        if self.merge_mode.get() == "json":
            try:
                data = json.loads(Path(src).read_text(encoding="utf-8"))
            except Exception as e:
                self.alert("Validate", f"Source is not valid JSON:\n{e}")
                return
            if not isinstance(data, list):
                self.alert("Validate", "Source JSON must be a top-level array.", "warn")
                return
            self.log(f"Validated JSON source: {len(data)} item(s).", "OK")
            self.alert("Validate", f"Source JSON is valid with {len(data)} item(s).", "info")
        else:
            p = Path(src)
            if p.is_dir():
                n = sum(1 for f in p.rglob("*") if f.is_file())
                self.log(f"Validated folder: {n} file(s) under {p}.", "OK")
                self.alert("Validate", f"Folder is valid — {n} file(s) will be copied.", "info")
            else:
                self.log(f"Validated file: {p.name}.", "OK")
                self.alert("Validate", f"File '{p.name}' exists and will be copied.", "info")

    def _merge(self):
        if not self.gh_clone_dir:
            self.alert("Merge", "Clone the fork first (Step 3).", "info")
            return
        src = self.source_var.get().strip()
        if not src or not Path(src).exists():
            self.alert("Merge", "Choose a valid source path first.", "info")
            return
        target_rel = self.target_var.get().strip()
        if not target_rel:
            self.alert("Merge", "Enter a target path (relative to repo root).", "info")
            return
        target = Path(self.gh_clone_dir) / target_rel

        try:
            if self.merge_mode.get() == "json":
                added, skipped, total = self._merge_json(Path(src), target)
                self.log(f"JSON merge \u2192 added {added}, skipped {skipped}. Total now {total}.", "OK")
                self.alert("Merge complete",
                           f"Added {added} item(s), skipped {skipped} duplicate(s).\n"
                           f"{target_rel} now has {total} item(s).\n\nNext: Step 5.", "info")
            else:
                copied = self._copy_files(Path(src), target)
                self.log(f"Copied {len(copied)} file(s) into {target_rel}.", "OK")
                self.alert("Merge complete",
                           f"Copied {len(copied)} file(s) into {target_rel}.\n\nNext: Step 5.", "info")
        except Exception as e:
            self.log(f"Merge failed: {e}", "ERROR")
            self.alert("Merge failed", str(e))
            return
        self.save_config()

    def _copy_files(self, src: Path, dst_dir: Path):
        dst_dir.mkdir(parents=True, exist_ok=True)
        copied = []
        if src.is_dir():
            for item in src.rglob("*"):
                if item.is_file():
                    rel = item.relative_to(src)
                    out = dst_dir / rel
                    out.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, out)
                    copied.append(str(rel))
        else:
            out = dst_dir / src.name
            shutil.copy2(src, out)
            copied.append(src.name)
        return copied

    def _merge_json(self, src_file: Path, target_file: Path):
        source = json.loads(src_file.read_text(encoding="utf-8"))
        if not isinstance(source, list):
            raise RuntimeError("Source JSON must be a top-level array.")
        if target_file.exists():
            target = json.loads(target_file.read_text(encoding="utf-8"))
            if not isinstance(target, list):
                raise RuntimeError(f"Target {target_file.name} is not a JSON array.")
        else:
            target = []
            target_file.parent.mkdir(parents=True, exist_ok=True)
        key = self.json_key_var.get().strip()
        seen = set()
        if key:
            for e in target:
                if isinstance(e, dict) and key in e:
                    seen.add(json.dumps(e.get(key), sort_keys=True))
        added = skipped = 0
        for item in source:
            if key and isinstance(item, dict) and key in item:
                sig = json.dumps(item.get(key), sort_keys=True)
                if sig in seen:
                    skipped += 1
                    continue
                seen.add(sig)
            target.append(item)
            added += 1
        target_file.write_text(json.dumps(target, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return added, skipped, len(target)

    # ---- Step 5 ----
    def _commit_push(self):
        def work():
            if not self.gh_clone_dir:
                raise RuntimeError("Clone the fork first (Step 3).")
            d = self.gh_clone_dir
            branch = self.branch_var.get().strip() or f"pr-agent-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            self.gh_branch = branch
            self.branch_var.set(branch)
            rc, _ = self._run_git(["-C", d, "checkout", "-B", branch])
            if rc != 0:
                raise RuntimeError("Could not create/switch branch.")
            self._run_git(["-C", d, "add", "-A"])
            msg = self.commit_var.get().strip() or "Update via GitHub PR Agent"
            rc, out = self._run_git(["-C", d, "commit", "-m", msg])
            if rc != 0 and "nothing to commit" in out.lower():
                self.log("Nothing to commit — run Step 4 first.", "WARN")
                self.alert("Commit", "Nothing to commit. Merge files in Step 4 first.", "warn")
                return
            rc, _ = self._run_git(["-C", d, "push", self._auth_url(), f"HEAD:{branch}", "--force"])
            if rc != 0:
                raise RuntimeError("git push failed (see terminal).")
            self.log(f"Pushed branch '{branch}' to {self.gh_fork_full}", "OK")
            self.save_config()
        self._async(work, "Commit & Push")

    # ---- Step 6 ----
    def _refresh_saved_combo(self):
        labels = [f"[{c['type']}] {c['command']}" for c in self.run_commands]
        self.saved_combo["values"] = labels

    def _load_saved_command(self, _evt=None):
        idx = self.saved_combo.current()
        if 0 <= idx < len(self.run_commands):
            c = self.run_commands[idx]
            self.run_type.set(c["type"])
            self.run_cmd_var.set(c["command"])

    def _delete_saved_command(self):
        idx = self.saved_combo.current()
        if 0 <= idx < len(self.run_commands):
            removed = self.run_commands.pop(idx)
            self._refresh_saved_combo()
            self.saved_cmd_var.set("")
            self.log(f"Removed saved command: {removed['command']}")
            self.save_config()

    def _remember_command(self, run_type, cmd):
        for c in self.run_commands:
            if c["type"] == run_type and c["command"] == cmd:
                return
        self.run_commands.insert(0, {"type": run_type, "command": cmd})
        self.run_commands = self.run_commands[:15]
        self._refresh_saved_combo()

    def _run_local(self):
        if not self.gh_clone_dir:
            self.alert("Run locally", "Clone the fork first (Step 3).", "info")
            return
        cmd = self.run_cmd_var.get().strip()
        if not cmd:
            self.alert("Run locally", "Enter a command to run.", "info")
            return
        run_type = self.run_type.get()
        self._remember_command(run_type, cmd)
        self.save_config()
        try:
            if run_type == "powershell":
                # Always run PowerShell with the security bypass, per requirement.
                full = ["powershell.exe", "-NoExit", "-NoProfile",
                        "-ExecutionPolicy", "Bypass", "-Command", cmd]
                self.log(f"Launching PowerShell (Bypass): {cmd}")
                subprocess.Popen(full, cwd=self.gh_clone_dir, creationflags=CREATE_NEW_CONSOLE)
            else:
                self.log(f"Launching cmd: {cmd}")
                subprocess.Popen(f'cmd /k "{cmd}"', cwd=self.gh_clone_dir,
                                 shell=True, creationflags=CREATE_NEW_CONSOLE)
        except Exception as e:
            self.log(f"Launch failed: {e}", "ERROR")
            self.alert("Run locally", str(e))

    # ---- Step 7 ----
    def _open_pr(self):
        def work():
            if not self.gh_user:
                raise RuntimeError("Connect first (Step 1).")
            if not self.gh_branch:
                raise RuntimeError("Commit & push a branch first (Step 5).")
            owner, repo = self._upstream()
            title = self.pr_title_var.get().strip()
            if not title:
                raise RuntimeError("Enter a PR title.")
            payload = {
                "title": title,
                "head": f"{self.gh_user}:{self.gh_branch}",
                "base": self.gh_default_branch,
                "body": self.pr_body.get("1.0", "end").strip(),
                "maintainer_can_modify": True,
            }
            self.log(f"Opening PR {payload['head']} \u2192 {owner}/{repo}:{self.gh_default_branch} \u2026")
            _, pr = self._api("POST", f"/repos/{owner}/{repo}/pulls", payload)
            url = pr.get("html_url", "")
            self.log(f"Pull Request opened: {url}", "OK")
            self.save_config()

            def prompt():
                if messagebox.askyesno("Pull Request opened",
                                       f"PR created:\n{url}\n\nOpen it in your browser?"):
                    webbrowser.open(url)
            self.root.after(0, prompt)
        self._async(work, "Open Pull Request")

    # ===== Tab 2 handlers ==================================================
    def _pub_toggle_token(self):
        self.pub_token_entry.config(show="" if self.pub_show_var.get() else "\u2022")

    def _pub_connect(self):
        self._do_connect(self.pub_token_var.get())

    def _pub_create_repo(self):
        def work():
            if not self.gh_user:
                raise RuntimeError("Log in first (Step 1).")
            name = self.pub_name_var.get().strip()
            if not name:
                raise RuntimeError("Enter a repository name.")
            full = f"{self.gh_user}/{name}"

            # Does it already exist? Only a 404 means "no" — surface anything else.
            exists = False
            try:
                s, info = self._api("GET", f"/repos/{full}")
                exists = (s == 200)
            except RuntimeError as e:
                if "404" not in str(e):
                    raise
            if exists:
                self.pub_repo_full = info.get("full_name", full)
                self.pub_repo_url = info.get("html_url", f"https://github.com/{full}")
                self.pub_default_branch = info.get("default_branch", "main")
                self.pub_repo_private = bool(info.get("private"))
                self.log(f"Repository already exists, reusing: {self.pub_repo_url}", "OK")
                if info.get("private"):
                    self.log("Note: this repo is PRIVATE — it 404s in a browser not signed in "
                             "as its owner.", "WARN")
                self.root.after(0, lambda: self.pub_repo_label.config(text=f"\u2192 {self.pub_repo_full}"))
                self.save_config()
                return

            payload = {
                "name": name,
                "description": self.pub_desc_var.get().strip(),
                "private": bool(self.pub_private_var.get()),
                "auto_init": False,
            }
            self.log(f"Creating repository {full} \u2026")
            try:
                _, created = self._api("POST", "/user/repos", payload)
            except RuntimeError as e:
                if "403" in str(e) or "404" in str(e):
                    raise RuntimeError(
                        f"{e}\n\nGitHub refused to create the repo. Your token cannot create "
                        "repositories. Use a CLASSIC token with the 'repo' scope, or a fine-grained "
                        "token with Account permission 'Administration' = Read/Write.")
                raise

            # Verify it actually exists before reporting success (creation can lag).
            info = None
            for _ in range(6):
                try:
                    s, info = self._api("GET", f"/repos/{full}")
                    if s == 200:
                        break
                except Exception:
                    info = None
                time.sleep(2)
            if not info:
                raise RuntimeError(
                    "The create request returned OK but the repository is not reachable. "
                    "This usually means the token lacks permission to create repos.")

            self.pub_repo_full = info.get("full_name", full)
            self.pub_repo_url = info.get("html_url", f"https://github.com/{full}")
            self.pub_default_branch = (info.get("default_branch")
                                       or self.pub_branch_var.get().strip() or "main")
            self.pub_repo_private = bool(info.get("private"))
            self.log(f"Repository created and verified: {self.pub_repo_url}", "OK")
            if info.get("private"):
                self.log("Note: repo is PRIVATE — it 404s in a browser not signed in as its owner.",
                         "WARN")
            self.root.after(0, lambda: self.pub_repo_label.config(text=f"\u2192 {self.pub_repo_full}"))
            self.save_config()
        self._async(work, "Create repository")

    def _pub_pick_source(self):
        d = filedialog.askdirectory(title="Choose the local folder to publish")
        if d:
            self.pub_source_var.set(d)

    def _pub_push(self):
        def work():
            if not self.pub_repo_full:
                raise RuntimeError("Create the repository first (Step 2).")
            src = Path(self.pub_source_var.get().strip())
            if not src.exists() or not src.is_dir():
                raise RuntimeError("Choose a valid local folder to push.")
            if not any(src.iterdir()):
                raise RuntimeError("The selected folder is empty.")
            d = str(src)
            branch = self.pub_branch_var.get().strip() or "main"
            if not (src / ".git").exists():
                rc, _ = self._run_git(["-C", d, "init"])
                if rc != 0:
                    raise RuntimeError("git init failed (see terminal).")
            self._run_git(["-C", d, "config", "user.name", self.gh_user])
            self._run_git(["-C", d, "config", "user.email", f"{self.gh_user}@users.noreply.github.com"])
            rc, _ = self._run_git(["-C", d, "checkout", "-B", branch])
            if rc != 0:
                raise RuntimeError("Could not create/switch branch.")
            self._run_git(["-C", d, "add", "-A"])
            rc, out = self._run_git(["-C", d, "commit", "-m",
                                     self.pub_commit_var.get().strip() or "Initial commit"])
            if rc != 0 and "nothing to commit" not in out.lower():
                raise RuntimeError("git commit failed (see terminal).")
            rc, _ = self._run_git(["-C", d, "push", self._auth_url_for(self.pub_repo_full),
                                   f"HEAD:{branch}", "--force"])
            if rc != 0:
                raise RuntimeError("git push failed (see terminal).")
            self.pub_default_branch = branch
            self.pub_source_dir = d
            self.log(f"Pushed {d} \u2192 {self.pub_repo_full} ({branch})", "OK")
            self.save_config()
        self._async(work, "Push files")

    def _pub_validate(self):
        def work():
            if not self.pub_repo_full:
                raise RuntimeError("Create and push the repository first.")
            src = Path(self.pub_source_var.get().strip())
            if not src.is_dir():
                raise RuntimeError("Choose the local folder you pushed.")
            branch = self.pub_default_branch or self.pub_branch_var.get().strip() or "main"
            self.log(f"Fetching file tree of {self.pub_repo_full}@{branch} \u2026")
            _, tree = self._api("GET", f"/repos/{self.pub_repo_full}/git/trees/{branch}?recursive=1")
            remote = {t["path"] for t in tree.get("tree", []) if t.get("type") == "blob"}
            if tree.get("truncated"):
                self.log("Warning: repo tree is truncated by the API; validation may be partial.", "WARN")
            local = []
            for f in src.rglob("*"):
                rel = f.relative_to(src)
                if f.is_file() and ".git" not in rel.parts:
                    local.append(rel.as_posix())
            missing = sorted(p for p in local if p not in remote)
            present = len(local) - len(missing)
            self.log(f"Validation: {present}/{len(local)} local file(s) present on repo; "
                     f"{len(missing)} missing.", "OK" if not missing else "WARN")
            self.root.after(0, lambda: self.pub_validate_label.config(
                text=(f"{present}/{len(local)} present \u2714" if not missing
                      else f"{present}/{len(local)} present, {len(missing)} missing"),
                foreground="#060" if not missing else "#a00"))
            if missing:
                preview = "\n".join(missing[:20]) + (f"\n\u2026 and {len(missing) - 20} more"
                                                     if len(missing) > 20 else "")
                self.alert("Validation",
                           f"{present}/{len(local)} present.\nMissing {len(missing)} file(s):\n\n{preview}", "warn")
            else:
                self.alert("Validation",
                           f"All {len(local)} file(s) are present on {self.pub_repo_full}. \u2714", "info")
        self._async(work, "Validate files")

    def _pub_open_browser(self):
        if not self.pub_repo_url:
            self.alert("Open repo", "Create the repository first (Step 2).", "info")
            return
        if self.pub_repo_private:
            self.alert("Open repo",
                       "This repository is PRIVATE. GitHub shows a 404 unless the browser is "
                       "signed in as its owner. Sign in to github.com first, or recreate the "
                       "repo with 'Private' unchecked.", "warn")
        self.log(f"Opening {self.pub_repo_url}")
        webbrowser.open(self.pub_repo_url)

    def _on_close(self):
        self.save_config()
        self.root.destroy()


def main():
    root = tk.Tk()
    GitHubPRAgent(root)
    root.mainloop()


if __name__ == "__main__":
    main()
