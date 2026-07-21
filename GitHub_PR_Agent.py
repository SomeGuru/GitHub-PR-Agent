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

Optional PAT vault
------------------
Via the "PAT Vault" button, the token can be stored ENCRYPTED at rest in
%LOCALAPPDATA%/GitHubPRAgent/vault.json, protected by a user-chosen master
passphrase (PBKDF2-HMAC-SHA256 key derivation + HMAC-SHA256 stream cipher with
encrypt-then-MAC authentication; standard-library only). Unlocking with the
master passphrase decrypts the PAT back into the Step 1 field. Entering the
reset passphrase "MikeLariosWasHere!" wipes the vault (for a forgotten
passphrase); it deletes the stored token without ever revealing it.
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
import hmac
import base64
import shutil
import secrets
import hashlib
import tempfile
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
APP_VERSION = "1.4.2"
CONFIG_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "GitHubPRAgent"
CONFIG_FILE = CONFIG_DIR / "config.json"

# ---- PAT vault ------------------------------------------------------------
# The Personal Access Token can optionally be stored ENCRYPTED at rest in this
# file, protected by a user-chosen master passphrase. It is decrypted back into
# the Step 1 token field on demand. The master reset passphrase below wipes the
# whole vault (used when the master passphrase is forgotten); it never reveals
# the stored token — it only deletes it.
VAULT_FILE = CONFIG_DIR / "vault.json"
VAULT_RESET_PHRASE = "MikeLariosWasHere!"
VAULT_KDF_ITERATIONS = 200_000

# ---- self-update source ---------------------------------------------------
# The agent can update itself by downloading the newest GitHub_PR_Agent.py from
# the default branch of this repository and replacing the running script.
UPDATE_REPO = "SomeGuru/GitHub-PR-Agent"
UPDATE_BRANCH = "main"
UPDATE_SCRIPT_NAME = "GitHub_PR_Agent.py"
UPDATE_RAW_URL = (
    f"https://raw.githubusercontent.com/{UPDATE_REPO}/{UPDATE_BRANCH}/{UPDATE_SCRIPT_NAME}"
)

CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
# Prevent flashing consoles from headless git calls on Windows.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# Launch the updated app independently of the (dying) current process.
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


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
# PAT vault crypto — stdlib only (no third-party packages).
#
# Reversible authenticated encryption built from PBKDF2 + HMAC-SHA256:
#   * PBKDF2-HMAC-SHA256 stretches the passphrase (with a random salt) into a
#     64-byte key that is split into an encryption key and a MAC key.
#   * A keystream is produced by HMAC-SHA256(enc_key, nonce || counter) in
#     counter mode and XOR-ed with the plaintext (a stream cipher).
#   * Encrypt-then-MAC: HMAC-SHA256(mac_key, nonce || ciphertext) authenticates
#     the data, so a wrong passphrase or any tampering is detected on decrypt.
# This is the standard construction for reversible secret storage without AES.
# ---------------------------------------------------------------------------
def _vault_derive_keys(passphrase: str, salt: bytes, iterations: int) -> tuple:
    dk = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations, dklen=64)
    return dk[:32], dk[32:]


def _vault_keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hmac.new(enc_key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(out[:length])


def vault_encrypt(passphrase: str, plaintext: str) -> dict:
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(16)
    enc_key, mac_key = _vault_derive_keys(passphrase, salt, VAULT_KDF_ITERATIONS)
    pt = plaintext.encode("utf-8")
    ks = _vault_keystream(enc_key, nonce, len(pt))
    ct = bytes(a ^ b for a, b in zip(pt, ks))
    tag = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    return {
        "v": 1,
        "kdf": "pbkdf2-sha256",
        "iter": VAULT_KDF_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ct": base64.b64encode(ct).decode("ascii"),
        "tag": base64.b64encode(tag).decode("ascii"),
    }


def vault_decrypt(passphrase: str, blob: dict) -> str:
    try:
        salt = base64.b64decode(blob["salt"])
        nonce = base64.b64decode(blob["nonce"])
        ct = base64.b64decode(blob["ct"])
        tag = base64.b64decode(blob["tag"])
        iterations = int(blob.get("iter", VAULT_KDF_ITERATIONS))
    except (KeyError, ValueError, TypeError) as e:
        raise ValueError("Vault file is malformed or corrupted.") from e
    enc_key, mac_key = _vault_derive_keys(passphrase, salt, iterations)
    expected = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, tag):
        raise ValueError("Wrong passphrase, or the vault has been tampered with.")
    ks = _vault_keystream(enc_key, nonce, len(ct))
    return bytes(a ^ b for a, b in zip(ct, ks)).decode("utf-8")


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
        self.build_repo = self.cfg.get("build_repo", "")

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

    def _pr_config_dict(self) -> dict:
        """All persisted fields for the Contribute-via-Pull-Request tab."""
        return {
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
        }

    def _pub_config_dict(self) -> dict:
        """All persisted fields for the Create-&-Publish-Repo tab."""
        return {
            "pub_name": self.pub_name_var.get().strip(),
            "pub_desc": self.pub_desc_var.get().strip(),
            "pub_private": bool(self.pub_private_var.get()),
            "pub_source": self.pub_source_var.get().strip(),
            "pub_branch": self.pub_branch_var.get().strip(),
            "pub_commit": self.pub_commit_var.get().strip(),
            "pub_scaffold": bool(self.pub_scaffold_var.get()),
            "rel_enable": bool(self.rel_enable_var.get()),
            "rel_win": bool(self.rel_win_var.get()),
            "rel_fedora": bool(self.rel_fedora_var.get()),
            "rel_debian": bool(self.rel_debian_var.get()),
            "build_repo": getattr(self, "build_repo", ""),
        }

    def save_config(self):
        """Persist all non-secret fields so past executions can be reused."""
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            data = {**self._pr_config_dict(), **self._pub_config_dict()}
            CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            self.log(f"Could not save config: {e}", "WARN")

    # ===== per-tab config export / import ==================================
    def _export_config(self, which):
        """Save one tab's config (or both) to a user-chosen JSON file."""
        if which == "pr":
            data = {"_agent_config": "pr", "_version": APP_VERSION, **self._pr_config_dict()}
            default = "pr_agent_pr_tab.json"
        elif which == "pub":
            data = {"_agent_config": "pub", "_version": APP_VERSION, **self._pub_config_dict()}
            default = "pr_agent_create_tab.json"
        else:
            data = {"_agent_config": "all", "_version": APP_VERSION,
                    **self._pr_config_dict(), **self._pub_config_dict()}
            default = "pr_agent_config.json"
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        path = filedialog.asksaveasfilename(
            title="Save configuration to file",
            defaultextension=".json",
            initialdir=str(CONFIG_DIR),
            initialfile=default,
            filetypes=[("JSON config", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            self.log(f"Saved {which} config \u2192 {path}", "OK")
        except Exception as e:
            self.alert("Save configuration", f"Could not save config: {e}", "error")

    def _import_config(self, which):
        """Load a tab's config (or both) from a user-chosen JSON file."""
        path = filedialog.askopenfilename(
            title="Load configuration from file",
            initialdir=str(CONFIG_DIR),
            filetypes=[("JSON config", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("File is not a valid config object.")
        except Exception as e:
            self.alert("Load configuration", f"Could not read config: {e}", "error")
            return
        kind = data.get("_agent_config")
        applied = []
        if which in ("pr", "all") and kind in (None, "pr", "all"):
            self._apply_pr_config(data); applied.append("Pull Request")
        if which in ("pub", "all") and kind in (None, "pub", "all"):
            self._apply_pub_config(data); applied.append("Create & Publish")
        if not applied:
            self.alert("Load configuration",
                       f"This file is a '{kind}' config and doesn't match the requested tab.", "warn")
            return
        self.save_config()
        self.log(f"Loaded config for: {', '.join(applied)} \u2190 {path}", "OK")

    def _apply_pr_config(self, d):
        if "upstream" in d: self.upstream_var.set(d.get("upstream", ""))
        if "clone_parent" in d: self.clone_parent_var.set(d.get("clone_parent", ""))
        if d.get("clone_dir"): self.gh_clone_dir = d.get("clone_dir", "")
        if "merge_mode" in d: self.merge_mode.set(d.get("merge_mode", "copy"))
        if "source_path" in d: self.source_var.set(d.get("source_path", ""))
        if "target_rel" in d: self.target_var.set(d.get("target_rel", ""))
        if "json_key" in d: self.json_key_var.set(d.get("json_key", ""))
        if "branch" in d: self.branch_var.set(d.get("branch", ""))
        if "commit_msg" in d: self.commit_var.set(d.get("commit_msg", ""))
        if "run_type" in d: self.run_type.set(d.get("run_type", "powershell"))
        if "run_cmd" in d: self.run_cmd_var.set(d.get("run_cmd", ""))
        if isinstance(d.get("run_commands"), list):
            self.run_commands = d["run_commands"]
            self._refresh_saved_combo()
        if "pr_title" in d: self.pr_title_var.set(d.get("pr_title", ""))
        if "pr_body" in d:
            self.pr_body.delete("1.0", "end")
            self.pr_body.insert("1.0", d.get("pr_body", ""))
        self._merge_mode_changed()

    def _apply_pub_config(self, d):
        if "pub_name" in d: self.pub_name_var.set(d.get("pub_name", ""))
        if "pub_desc" in d: self.pub_desc_var.set(d.get("pub_desc", ""))
        if "pub_private" in d: self.pub_private_var.set(bool(d.get("pub_private")))
        if "pub_source" in d: self.pub_source_var.set(d.get("pub_source", ""))
        if "pub_branch" in d: self.pub_branch_var.set(d.get("pub_branch", "main"))
        if "pub_commit" in d: self.pub_commit_var.set(d.get("pub_commit", ""))
        if "pub_scaffold" in d: self.pub_scaffold_var.set(bool(d.get("pub_scaffold")))
        if "rel_enable" in d: self.rel_enable_var.set(bool(d.get("rel_enable")))
        if "rel_win" in d: self.rel_win_var.set(bool(d.get("rel_win")))
        if "rel_fedora" in d: self.rel_fedora_var.set(bool(d.get("rel_fedora")))
        if "rel_debian" in d: self.rel_debian_var.set(bool(d.get("rel_debian")))
        if "build_repo" in d: self.build_repo = d.get("build_repo", "")
        self._rel_toggle()

    # ===== UI ==============================================================
    def _build_ui(self):
        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True)

        # Tabbed step area on top, shared terminal at the bottom.
        paned = ttk.PanedWindow(outer, orient="vertical")
        paned.pack(fill="both", expand=True)

        nb = ttk.Notebook(paned)
        paned.add(nb, weight=3)

        # ---- Tab 1: Create & Publish Repo ----
        pub_tab = ttk.Frame(nb)
        nb.add(pub_tab, text="Create & Publish Repo")
        pbody = self._scrollable(pub_tab)
        self._build_publish_tab(pbody)

        # ---- Tab 2: Contribute via Pull Request ----
        pr_tab = ttk.Frame(nb)
        nb.add(pr_tab, text="Contribute via Pull Request")
        body = self._scrollable(pr_tab)
        intro = ("Contribute to any GitHub repo via Pull Request. Complete the steps top to bottom. "
                 "Your token is kept in memory only; every other field is remembered for reuse.")
        ttk.Label(body, text=intro, wraplength=1120, justify="left").pack(anchor="w", padx=10, pady=(8, 4))
        prbar = ttk.Frame(body); prbar.pack(fill="x", padx=10, pady=(0, 4))
        ttk.Label(prbar, text="Tab config:").pack(side="left")
        ttk.Button(prbar, text="\U0001f4be Save this tab\u2026",
                   command=lambda: self._export_config("pr")).pack(side="left", padx=4)
        ttk.Button(prbar, text="\U0001f4c2 Load config\u2026",
                   command=lambda: self._import_config("pr")).pack(side="left", padx=4)
        ttk.Button(prbar, text="\U0001f512 PAT Vault",
                   command=self.open_vault).pack(side="left", padx=4)
        self._build_step1(body)
        self._build_step2(body)
        self._build_step3(body)
        self._build_step4(body)
        self._build_step5(body)
        self._build_step6(body)
        self._build_step7(body)

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
        ttk.Button(btns, text="\U0001f4be Save Activity Window",
                   command=self._save_activity_window).pack(side="left", padx=6)
        ttk.Button(btns, text="\U0001f3d7 Build",
                   command=self.open_build_release).pack(side="left", padx=6)
        ttk.Button(btns, text="\u2b73 Check for updates", command=self._check_for_updates).pack(side="left", padx=6)
        ttk.Label(btns, text=f"v{APP_VERSION}", foreground="#888").pack(side="right", padx=6)

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
        pubbar = ttk.Frame(parent); pubbar.pack(fill="x", padx=10, pady=(0, 4))
        ttk.Label(pubbar, text="Tab config:").pack(side="left")
        ttk.Button(pubbar, text="\U0001f4be Save this tab\u2026",
                   command=lambda: self._export_config("pub")).pack(side="left", padx=4)
        ttk.Button(pubbar, text="\U0001f4c2 Load config\u2026",
                   command=lambda: self._import_config("pub")).pack(side="left", padx=4)
        ttk.Button(pubbar, text="\U0001f512 PAT Vault",
                   command=self.open_vault).pack(side="left", padx=4)

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

        # Step 2b — Project scaffolding & release automation
        sr = ttk.LabelFrame(parent, text="Step 2b — Project scaffolding & release automation")
        sr.pack(fill="x", padx=10, pady=5)
        sc = ttk.Frame(sr); sc.pack(fill="x", padx=6, pady=(6, 2))
        self.pub_scaffold_var = tk.BooleanVar(value=self.cfg.get("pub_scaffold", True))
        ttk.Checkbutton(sc, text="Create missing default files before push (README.md, .gitignore, src/ source folder)",
                        variable=self.pub_scaffold_var).pack(side="left")
        rr = ttk.Frame(sr); rr.pack(fill="x", padx=6, pady=(2, 2))
        self.rel_enable_var = tk.BooleanVar(value=self.cfg.get("rel_enable", False))
        ttk.Checkbutton(rr, text="Add a GitHub release agent (Actions workflow that builds & publishes releases on tag)",
                        variable=self.rel_enable_var, command=self._rel_toggle).pack(side="left")
        pr = ttk.Frame(sr); pr.pack(fill="x", padx=26, pady=(0, 6))
        ttk.Label(pr, text="Target platforms:").pack(side="left")
        self.rel_win_var = tk.BooleanVar(value=self.cfg.get("rel_win", True))
        self.rel_fedora_var = tk.BooleanVar(value=self.cfg.get("rel_fedora", True))
        self.rel_debian_var = tk.BooleanVar(value=self.cfg.get("rel_debian", True))
        self._rel_checks = [
            ttk.Checkbutton(pr, text="Windows 10/11", variable=self.rel_win_var),
            ttk.Checkbutton(pr, text="Linux Fedora", variable=self.rel_fedora_var),
            ttk.Checkbutton(pr, text="Linux Debian", variable=self.rel_debian_var),
        ]
        for c in self._rel_checks:
            c.pack(side="left", padx=8)
        ttk.Label(sr, foreground="#666", wraplength=1100, justify="left",
                  text=("Note: pushing workflow files requires a token with the 'workflow' scope "
                        "(in addition to 'repo'). Tag a commit 'vX.Y.Z' to trigger a release.")
                  ).pack(anchor="w", padx=26, pady=(0, 6))
        self._rel_toggle()

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
        # Force non-interactive git: our token is already embedded in the push URL,
        # so disable every credential helper / prompt. Otherwise PortableGit's
        # Git Credential Manager can pop a GUI selector that hangs the (captured,
        # stdin-less) subprocess forever.
        prefix = ["-c", "credential.helper=",
                  "-c", "credential.interactive=false",
                  "-c", "core.askpass="]
        cmd = [self.git_exe] + prefix + args
        shown = " ".join(self._redact(a) if "@github.com" in a else a for a in cmd)
        self.log(f"$ {shown}")
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"      # never prompt on the terminal
        env["GCM_INTERACTIVE"] = "Never"      # never show the GCM GUI
        env["GIT_ASKPASS"] = ""               # no external askpass program
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              creationflags=NO_WINDOW, env=env)
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
        # Accept a bare "owner/repo" OR a pasted GitHub URL in many shapes:
        #   https://github.com/owner/repo
        #   https://github.com/owner/repo.git
        #   git@github.com:owner/repo.git
        #   https://github.com/owner/repo/tree/main/...
        up = up.strip().strip("<>").strip()
        # strip scheme + host (http/https/ssh/git@)
        up = re.sub(r"^[a-zA-Z]+://", "", up)          # https://, ssh://, git://
        up = re.sub(r"^git@", "", up)                  # git@github.com:owner/repo
        up = re.sub(r"^[^/]*github\.com[:/]+", "", up)  # host + separator
        up = up.strip("/")
        if up.endswith(".git"):
            up = up[:-4]
        parts = [p for p in up.split("/") if p]
        if len(parts) < 2:
            raise RuntimeError(
                "Upstream must be 'owner/repo' (or a github.com repo URL).")
        owner, repo = parts[0], parts[1]              # ignore /tree/..., /blob/...
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
            # Verify the upstream is actually reachable with this token first,
            # so we can give a precise message instead of a raw 404 from /forks.
            try:
                s, up_info = self._api("GET", f"/repos/{owner}/{repo}")
            except RuntimeError as e:
                if "404" in str(e):
                    raise RuntimeError(
                        f"Upstream '{owner}/{repo}' not found (404). Check the owner/repo "
                        "spelling. If it's a PRIVATE repo, a fine-grained token only sees "
                        "repos you granted it — use a CLASSIC token with the 'repo' scope, "
                        "or a fine-grained token scoped to that repository.")
                raise
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
                if "404" in str(e):
                    raise RuntimeError(
                        "Fork endpoint returned 404. Your token can read the repo but is "
                        "not allowed to fork it. Use a CLASSIC token with the 'repo' scope "
                        "(or 'public_repo' for public repos).")
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

    @staticmethod
    def _normalize_dir(raw):
        """Clean a user-typed folder path: strip quotes/space, expand ~ and %VARS%."""
        s = (raw or "").strip().strip('"').strip("'").strip()
        if not s:
            return ""
        return os.path.expandvars(os.path.expanduser(s))

    def _ensure_git(self):
        """Fail early with a clear message if no git executable can be found."""
        gp = self.git_exe
        if not (Path(gp).exists() or shutil.which(gp)):
            raise RuntimeError(
                "Bundled git was not found. Keep the 'vendor' folder next to the app "
                "(don't move the .exe out of its folder). Expected: "
                f"{app_base_dir() / 'vendor' / 'PortableGit' / 'cmd' / 'git.exe'}")

    def _sync_into(self, src, dst):
        """Copy files from src into dst, replacing only those that differ by size
        or modified-time. Returns (added, updated, unchanged) counts."""
        src, dst = Path(src), Path(dst)
        added = updated = unchanged = 0
        for root, _dirs, files in os.walk(src):
            rel = Path(root).relative_to(src)
            target_root = dst / rel
            target_root.mkdir(parents=True, exist_ok=True)
            for name in files:
                sfile = Path(root) / name
                dfile = target_root / name
                try:
                    if not dfile.exists():
                        shutil.copy2(sfile, dfile); added += 1
                    else:
                        ss, ds = sfile.stat(), dfile.stat()
                        if ss.st_size != ds.st_size or int(ss.st_mtime) != int(ds.st_mtime):
                            shutil.copy2(sfile, dfile); updated += 1
                        else:
                            unchanged += 1
                except OSError as e:
                    self.log(f"  skip {dfile}: {e}", "WARN")
        return added, updated, unchanged

    def _clone(self):
        def work():
            self._ensure_git()
            if not self.gh_fork_full:
                raise RuntimeError("Fork first (Step 2).")
            _, repo = self._upstream()
            parent_str = self._normalize_dir(self.clone_parent_var.get())
            if not parent_str:
                parent_str = str(Path.home())
                self.log(f"No destination given — using {parent_str}", "WARN")
            parent = Path(parent_str)
            dest = parent / repo
            url = f"https://github.com/{self.gh_fork_full}.git"

            # Make sure the destination parent exists (auto-create).
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise RuntimeError(f"Cannot create destination folder '{parent}': {e}")

            owner, urepo = self._upstream()

            # Case 1: existing git repo — just refresh it.
            if (dest / ".git").exists():
                self.log(f"Clone exists at {dest} — fetching latest \u2026")
                self._run_git(["-C", str(dest), "fetch", "origin"])
                self._finish_clone(dest, owner, urepo)
                return

            # Case 2: folder exists with content but is NOT a git repo — clone to a
            # temp dir and reconcile by size/date/time instead of failing.
            if dest.exists() and any(dest.iterdir()):
                self.log(f"Target '{dest}' already exists — cloning to a temp area and "
                         "reconciling changed files (by size + modified time) \u2026", "WARN")
                tmp = Path(tempfile.mkdtemp(prefix="prclone_"))
                tmp_clone = tmp / repo
                rc, _ = self._run_git(["clone", url, str(tmp_clone)])
                if rc != 0:
                    shutil.rmtree(tmp, ignore_errors=True)
                    raise RuntimeError("git clone failed (see terminal).")
                added, updated, unchanged = self._sync_into(tmp_clone, dest)
                shutil.rmtree(tmp, ignore_errors=True)
                self.log(f"Reconciled into {dest}: {added} added, {updated} updated, "
                         f"{unchanged} unchanged", "OK")
                self._finish_clone(dest, owner, urepo)
                return

            # Case 3: fresh clone into a new/empty folder.
            rc, _ = self._run_git(["clone", url, str(dest)])
            if rc != 0:
                raise RuntimeError("git clone failed (see terminal).")
            self._finish_clone(dest, owner, urepo)
        self._async(work, "Clone")

    def _finish_clone(self, dest, owner, urepo):
        """Common post-clone setup: identity, upstream remote, persist location."""
        self._run_git(["-C", str(dest), "config", "user.name", self.gh_user])
        self._run_git(["-C", str(dest), "config", "user.email",
                       f"{self.gh_user}@users.noreply.github.com"])
        # (Re)point the 'upstream' remote at the source repo.
        rc, _ = self._run_git(["-C", str(dest), "remote", "get-url", "upstream"])
        verb = "set-url" if rc == 0 else "add"
        self._run_git(["-C", str(dest), "remote", verb, "upstream",
                       f"https://github.com/{owner}/{urepo}.git"])
        self.gh_clone_dir = str(dest)
        self.log(f"Clone ready at {dest}", "OK")
        self.root.after(0, lambda: self.clone_label.config(text=str(dest)))
        self.save_config()

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

    # ===== PAT vault (encrypted-at-rest token storage) ====================
    def _vault_current_pat(self) -> str:
        """The PAT to store: whichever Step 1 field/connection has one."""
        for var_name in ("token_var", "pub_token_var"):
            var = getattr(self, var_name, None)
            if var is not None and var.get().strip():
                return var.get().strip()
        return (self.gh_token or "").strip()

    def _vault_fill_step1(self, token: str):
        """Load a recovered PAT back into both Step 1 token fields."""
        if hasattr(self, "token_var"):
            self.token_var.set(token)
        if hasattr(self, "pub_token_var"):
            self.pub_token_var.set(token)

    def open_vault(self):
        """Modal vault dialog: one master passphrase unlocks a Fill button that
        pushes the stored PAT into Step 1. A reserved reset passphrase wipes it."""
        win = tk.Toplevel(self.root)
        win.title("\U0001f512 PAT Vault")
        win.transient(self.root)
        win.resizable(False, False)
        win.grab_set()

        # Holds the decrypted PAT after a successful unlock (kept in memory only).
        recovered = {"pat": None}

        status = ttk.Label(win, foreground="#444", wraplength=540, justify="left")
        status.pack(anchor="w", padx=12, pady=(12, 4))

        entries = []

        def make_pw_entry(parent, width=32):
            e = ttk.Entry(parent, show="\u2022", width=width)
            entries.append(e)
            return e

        # --- Master login -------------------------------------------------
        login_fr = ttk.LabelFrame(win, text="Master login")
        login_fr.pack(fill="x", padx=12, pady=6)
        lr1 = ttk.Frame(login_fr); lr1.pack(fill="x", padx=6, pady=(4, 3))
        ttk.Label(lr1, text="Master passphrase:", width=18).pack(side="left")
        master_pw = make_pw_entry(lr1); master_pw.pack(side="left", padx=4)
        ttk.Button(lr1, text="\U0001f513 Unlock", command=lambda: do_unlock()).pack(side="left", padx=4)
        lr2 = ttk.Frame(login_fr); lr2.pack(fill="x", padx=6, pady=(0, 6))
        fill_btn = ttk.Button(lr2, text="\u2b07 Fill Step 1 PAT", state="disabled",
                              command=lambda: do_fill())
        fill_btn.pack(side="left")
        ttk.Button(lr2, text="\U0001f512 Save current PAT to vault",
                   command=lambda: do_save()).pack(side="left", padx=6)

        def refresh_status():
            if recovered["pat"] is not None:
                status.config(text="Vault unlocked. Click \u201cFill Step 1 PAT\u201d to push the "
                                   "stored token into the Step 1 field.")
            elif VAULT_FILE.exists():
                status.config(text="A PAT is stored (encrypted). Enter your master passphrase and "
                                   "click Unlock to enable the Fill button.")
            else:
                status.config(text="The vault is empty. Enter a master passphrase and click "
                                   "\u201cSave current PAT to vault\u201d to store your Step 1 token.")

        def do_unlock():
            if not VAULT_FILE.exists():
                self.alert("PAT Vault", "The vault is empty \u2014 save a PAT first.", "info")
                return
            try:
                blob = json.loads(VAULT_FILE.read_text(encoding="utf-8"))
                recovered["pat"] = vault_decrypt(master_pw.get(), blob)
            except ValueError as e:
                self.alert("PAT Vault", str(e), "error")
                return
            except Exception as e:
                self.alert("PAT Vault", f"Could not open the vault: {e}", "error")
                return
            fill_btn.config(state="normal")
            self.log("Vault unlocked \u2014 Fill button enabled.", "OK")
            refresh_status()

        def do_fill():
            if recovered["pat"] is None:
                self.alert("PAT Vault", "Unlock with your master passphrase first.", "warn")
                return
            self._vault_fill_step1(recovered["pat"])
            self.log("PAT filled into Step 1 from the vault. Click Connect / Log in.", "OK")
            win.destroy()

        def do_save():
            pat = self._vault_current_pat()
            if not pat:
                self.alert("PAT Vault", "No PAT found. Enter your token in Step 1 first.", "warn")
                return
            p1 = master_pw.get()
            if len(p1) < 4:
                self.alert("PAT Vault", "Enter a master passphrase of at least 4 characters "
                                        "in the field above first.", "warn")
                return
            if p1 == VAULT_RESET_PHRASE:
                self.alert("PAT Vault", "That passphrase is reserved for resetting the vault. "
                                        "Choose a different master passphrase.", "warn")
                return
            if not messagebox.askyesno("PAT Vault",
                                       "Encrypt and store the current Step 1 PAT in the vault "
                                       "using the master passphrase entered above?"):
                return
            try:
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                blob = vault_encrypt(p1, pat)
                VAULT_FILE.write_text(json.dumps(blob, indent=2), encoding="utf-8")
            except Exception as e:
                self.alert("PAT Vault", f"Could not save the vault: {e}", "error")
                return
            recovered["pat"] = pat
            fill_btn.config(state="normal")
            self.log("PAT saved to the encrypted vault.", "OK")
            refresh_status()
            self.alert("PAT Vault", "Your PAT is now stored encrypted at rest, and the Fill "
                                    "button is enabled.", "info")

        # --- Reset --------------------------------------------------------
        reset_fr = ttk.LabelFrame(win, text="Reset the whole vault (forgotten passphrase)")
        reset_fr.pack(fill="x", padx=12, pady=6)
        rr1 = ttk.Frame(reset_fr); rr1.pack(fill="x", padx=6, pady=(4, 6))
        ttk.Label(rr1, text="Reset passphrase:", width=18).pack(side="left")
        reset_pw = make_pw_entry(rr1); reset_pw.pack(side="left", padx=4)

        def do_reset():
            if reset_pw.get() != VAULT_RESET_PHRASE:
                self.alert("PAT Vault", "Incorrect reset passphrase.", "error")
                return
            if not messagebox.askyesno("PAT Vault",
                                       "This permanently erases the stored PAT from the vault. Continue?"):
                return
            try:
                if VAULT_FILE.exists():
                    VAULT_FILE.unlink()
            except Exception as e:
                self.alert("PAT Vault", f"Could not reset the vault: {e}", "error")
                return
            reset_pw.delete(0, "end")
            recovered["pat"] = None
            fill_btn.config(state="disabled")
            self.log("Vault reset \u2014 stored PAT erased.", "WARN")
            refresh_status()
            self.alert("PAT Vault", "The vault has been reset and is now empty.", "info")

        ttk.Button(rr1, text="\u267b Reset vault", command=do_reset).pack(side="left", padx=4)

        # --- shared controls ---------------------------------------------
        foot = ttk.Frame(win); foot.pack(fill="x", padx=12, pady=(2, 12))
        show_pw = tk.BooleanVar(value=False)

        def toggle_show():
            ch = "" if show_pw.get() else "\u2022"
            for e in entries:
                e.config(show=ch)

        ttk.Checkbutton(foot, text="Show passphrases", variable=show_pw,
                        command=toggle_show).pack(side="left")
        ttk.Button(foot, text="Close", command=win.destroy).pack(side="right")

        refresh_status()
        master_pw.focus_set()
        win.update_idletasks()
        try:
            x = self.root.winfo_rootx() + 60
            y = self.root.winfo_rooty() + 60
            win.geometry(f"+{x}+{y}")
        except Exception:
            pass

    def _save_activity_window(self):
        """Save the activity terminal to a text file and copy it to the clipboard."""
        text = self.console.get("1.0", "end").rstrip("\n")
        copied = False
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
            copied = True
        except Exception as e:
            self.log(f"Could not copy activity to clipboard: {e}", "WARN")
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = filedialog.asksaveasfilename(
            title="Save activity window to file",
            defaultextension=".txt",
            initialdir=str(CONFIG_DIR),
            initialfile=f"github_pr_agent_activity_{ts}.txt",
            filetypes=[("Text log", "*.txt"), ("All files", "*.*")])
        if path:
            try:
                Path(path).write_text(text, encoding="utf-8")
                self.log(f"Activity window saved \u2192 {path}"
                         + (" (also copied to clipboard)" if copied else ""), "OK")
            except Exception as e:
                self.alert("Save Activity Window", f"Could not save file: {e}", "error")
        elif copied:
            self.log("Activity window copied to clipboard.", "OK")

    # ===== Build release (push a version tag → run Actions) ===============
    def open_build_release(self):
        """Dialog that pushes tag v{APP_VERSION} to a repo to trigger the
        release workflow that builds the executables."""
        tag = f"v{APP_VERSION}"
        win = tk.Toplevel(self.root)
        win.title("\U0001f3d7 Build Release")
        win.transient(self.root)
        win.resizable(False, False)
        win.grab_set()

        ttk.Label(win, wraplength=560, justify="left", foreground="#444",
                  text=(f"This pushes the tag \u201c{tag}\u201d (from APP_VERSION) to your GitHub "
                        "repository. That tag push triggers the release workflow "
                        "(.github/workflows/release.yml), which builds the Windows, Fedora and "
                        "Debian executables and publishes them to a GitHub Release.\n\n"
                        "You must be connected in Step 1 with a token that has 'repo' scope on "
                        "the target repository.")
                  ).pack(anchor="w", padx=12, pady=(12, 6))

        form = ttk.Frame(win); form.pack(fill="x", padx=12, pady=2)
        ttk.Label(form, text="Target repo (owner/repo):", width=22).grid(row=0, column=0, sticky="w", pady=3)
        repo_var = tk.StringVar(value=self.build_repo or "")
        repo_entry = ttk.Entry(form, textvariable=repo_var, width=40, font=("Consolas", 9))
        repo_entry.grid(row=0, column=1, sticky="w", padx=4, pady=3)

        ttk.Label(form, text="Branch to tag:", width=22).grid(row=1, column=0, sticky="w", pady=3)
        branch_var = tk.StringVar(value="")
        ttk.Entry(form, textvariable=branch_var, width=24).grid(row=1, column=1, sticky="w", padx=4, pady=3)
        ttk.Label(form, text="(blank = repo default branch)", foreground="#888").grid(
            row=1, column=2, sticky="w")

        ttk.Label(form, text="Tag to push:", width=22).grid(row=2, column=0, sticky="w", pady=3)
        ttk.Label(form, text=tag, font=("Consolas", 10, "bold"),
                  foreground="#060").grid(row=2, column=1, sticky="w", padx=4, pady=3)

        recreate_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(win, text=f"Recreate the tag if \u201c{tag}\u201d already exists "
                                  "(re-runs the build)", variable=recreate_var).pack(
            anchor="w", padx=12, pady=(2, 4))

        foot = ttk.Frame(win); foot.pack(fill="x", padx=12, pady=(4, 12))

        def do_build():
            repo_full = repo_var.get().strip()
            if not self.gh_user:
                self.alert("Build Release", "Connect to GitHub in Step 1 first.", "warn")
                return
            if repo_full.count("/") != 1 or not all(repo_full.split("/")):
                self.alert("Build Release", "Enter the target repository as owner/repo "
                                            "(e.g. myname/GitHub-PR-Agent).", "warn")
                return
            branch = branch_var.get().strip()
            recreate = recreate_var.get()
            win.destroy()
            self._async(lambda: self._push_build_tag(repo_full, branch, recreate), "Build Release")

        ttk.Button(foot, text=f"\U0001f3d7 Build (push {tag} & run Actions)",
                   command=do_build).pack(side="left")
        ttk.Button(foot, text="Close", command=win.destroy).pack(side="right")

        repo_entry.focus_set()
        win.update_idletasks()
        try:
            x = self.root.winfo_rootx() + 60
            y = self.root.winfo_rooty() + 60
            win.geometry(f"+{x}+{y}")
        except Exception:
            pass

    def _push_build_tag(self, repo_full, branch, recreate):
        if not self.gh_user:
            raise RuntimeError("Connect first (Step 1).")
        owner, repo = repo_full.split("/", 1)
        tag = f"v{APP_VERSION}"
        self.log(f"Preparing release build: tag {tag} \u2192 {owner}/{repo} \u2026")

        if not branch:
            _, info = self._api("GET", f"/repos/{owner}/{repo}")
            branch = info.get("default_branch", "main")
            self.log(f"Using default branch '{branch}'.")

        try:
            _, ref = self._api("GET", f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
        except RuntimeError as e:
            if "404" in str(e):
                raise RuntimeError(f"Branch '{branch}' not found on {owner}/{repo}. Check the "
                                   "repo/branch, or that your token can see it.")
            raise
        sha = ref["object"]["sha"]

        exists = False
        try:
            self._api("GET", f"/repos/{owner}/{repo}/git/ref/tags/{tag}")
            exists = True
        except RuntimeError as e:
            if "404" not in str(e):
                raise

        if exists:
            if not recreate:
                raise RuntimeError(
                    f"Tag {tag} already exists on {owner}/{repo}. Bump APP_VERSION for a new "
                    "release, or re-open Build and tick 'Recreate the tag' to re-run the build.")
            self._api("DELETE", f"/repos/{owner}/{repo}/git/refs/tags/{tag}")
            self.log(f"Deleted existing tag {tag} to re-trigger the build.", "WARN")
            time.sleep(1)

        self._api("POST", f"/repos/{owner}/{repo}/git/refs",
                  {"ref": f"refs/tags/{tag}", "sha": sha})
        self.log(f"Pushed tag {tag} \u2192 {owner}/{repo}@{sha[:7]}. Release build started.", "OK")

        self.build_repo = repo_full
        self.save_config()

        actions_url = f"https://github.com/{owner}/{repo}/actions"
        self.log(f"Track the build here: {actions_url}")

        def prompt_open():
            if messagebox.askyesno("Build Release",
                                   f"Tag {tag} pushed to {owner}/{repo}.\n\n"
                                   "The Actions build is now running. Open the Actions page "
                                   "in your browser to watch it?"):
                webbrowser.open(actions_url)
        self.root.after(0, prompt_open)


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

            # Pushing .github/workflows/* requires the token's 'workflow' scope.
            # Classic tokens report scopes via the API; block early with a clear
            # message rather than committing and hitting a remote rejection.
            if self.rel_enable_var.get():
                scope_set = {s.strip() for s in (self.gh_scopes or "").split(",") if s.strip()}
                if scope_set and "workflow" not in scope_set:
                    raise RuntimeError(
                        "The release agent writes '.github/workflows/release.yml', but your "
                        "token cannot push workflow files.\n\n"
                        "Fix: add the 'workflow' scope to your classic PAT (keep 'repo' too) at "
                        "github.com/settings/tokens, reconnect, and push again \u2014 OR uncheck "
                        "'Add a GitHub release agent' to publish without the workflow.")

            # Guard against GitHub's hard 100 MB-per-file limit and warn on bloat.
            oversized, total = [], 0
            for f in src.rglob("*"):
                if f.is_file() and ".git" not in f.relative_to(src).parts:
                    try:
                        sz = f.stat().st_size
                    except OSError:
                        continue
                    total += sz
                    if sz > 95 * 1024 * 1024:
                        oversized.append((f.relative_to(src).as_posix(), sz))
            if oversized:
                lst = "\n".join(f"  {p} ({s // (1024*1024)} MB)" for p, s in oversized[:10])
                raise RuntimeError(
                    "These files exceed GitHub's 100 MB per-file limit, so the whole push "
                    "would be rejected. Remove or .gitignore them first:\n\n" + lst)
            if total > 150 * 1024 * 1024:
                self.log(f"Warning: pushing ~{total // (1024*1024)} MB. If this folder contains "
                         "build artifacts (vendor/, dist/, build/), add a .gitignore so only your "
                         "source is published.", "WARN")

            d = str(src)
            branch = self.pub_branch_var.get().strip() or "main"

            # Create sensible defaults if the user opted in and they are absent.
            if self.pub_scaffold_var.get():
                self._scaffold_project_files(src)
            if self.rel_enable_var.get():
                self._write_release_workflow(src)

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
            rc, out = self._run_git(["-C", d, "push", self._auth_url_for(self.pub_repo_full),
                                     f"HEAD:{branch}", "--force"])
            if rc != 0:
                low = out.lower()
                if "workflow" in low and "scope" in low:
                    raise RuntimeError(
                        "GitHub rejected the push because the token lacks the 'workflow' scope "
                        "required to create/update '.github/workflows/release.yml'.\n\n"
                        "Add the 'workflow' scope to your PAT and reconnect, or uncheck "
                        "'Add a GitHub release agent', then push again.")
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
            used_git = False
            if (src / ".git").exists():
                # Validate only what git would actually push: tracked files,
                # respecting .gitignore. Avoids false "missing" on ignored files
                # like __pycache__/*.pyc.
                rc, out = self._run_git(["-C", str(src), "ls-files"])
                if rc == 0:
                    local = [line.strip() for line in out.splitlines() if line.strip()]
                    used_git = True
            if not used_git:
                for f in src.rglob("*"):
                    rel = f.relative_to(src)
                    if f.is_file() and ".git" not in rel.parts:
                        local.append(rel.as_posix())
            missing = sorted(p for p in local if p not in remote)
            present = len(local) - len(missing)
            scope_note = " (tracked files only)" if used_git else ""
            self.log(f"Validation: {present}/{len(local)} local file(s) present on repo{scope_note}; "
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

    # ===== project scaffolding =============================================
    def _scaffold_project_files(self, src: Path):
        """Create README.md, .gitignore and a src/ source folder if missing."""
        created = []

        readme = src / "README.md"
        if not readme.exists():
            name = self.pub_name_var.get().strip() or src.name
            desc = self.pub_desc_var.get().strip() or "Project published with GitHub PR Agent."
            readme.write_text(
                f"# {name}\n\n{desc}\n\n"
                "## Getting started\n\n"
                "Source code lives in the `src/` folder.\n\n"
                "## Build & release\n\n"
                "Tag a commit as `vX.Y.Z` and push the tag to trigger the release workflow\n"
                "(see `.github/workflows/release.yml`) if enabled.\n",
                encoding="utf-8")
            created.append("README.md")

        gitignore = src / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(
                "# Python\n__pycache__/\n*.py[cod]\n*.egg-info/\n.venv/\nvenv/\n"
                "# Build output & bundled tooling\nbuild/\ndist/\nvendor/\n*.spec.bak\n"
                "# OS / editor\n.DS_Store\nThumbs.db\n.idea/\n.vscode/\n",
                encoding="utf-8")
            created.append(".gitignore")

        src_dir = src / "src"
        if not src_dir.exists():
            src_dir.mkdir(parents=True, exist_ok=True)
            keep = src_dir / ".gitkeep"
            keep.write_text("", encoding="utf-8")
            created.append("src/")

        if created:
            self.log("Scaffolded missing default files: " + ", ".join(created), "OK")
        else:
            self.log("Scaffolding: all default files already present.")

    def _write_release_workflow(self, src: Path):
        """Write .github/workflows/release.yml for the selected platforms."""
        targets = []
        if self.rel_win_var.get():
            targets.append("windows")
        if self.rel_fedora_var.get():
            targets.append("fedora")
        if self.rel_debian_var.get():
            targets.append("debian")
        if not targets:
            self.log("Release agent enabled but no target platform selected; skipping workflow.",
                     "WARN")
            return

        wf_dir = src / ".github" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        wf_file = wf_dir / "release.yml"
        wf_file.write_text(self._render_release_workflow(targets), encoding="utf-8")
        self.log(f"Wrote release workflow for [{', '.join(targets)}] \u2192 "
                 ".github/workflows/release.yml", "OK")

    @staticmethod
    def _render_release_workflow(targets) -> str:
        header = (
            "name: Build & Release\n\n"
            "# Builds standalone binaries and publishes them to a GitHub Release\n"
            "# whenever a tag like v1.2.3 is pushed.\n"
            "on:\n"
            "  push:\n"
            "    tags:\n"
            "      - 'v*'\n"
            "  workflow_dispatch:\n\n"
            "permissions:\n"
            "  contents: write\n\n"
            "jobs:\n"
        )

        win_job = (
            "  build-windows:\n"
            "    name: Build (Windows 10/11)\n"
            "    runs-on: windows-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "      - uses: actions/setup-python@v5\n"
            "        with:\n"
            "          python-version: '3.12'\n"
            "      - name: Install PyInstaller\n"
            "        run: python -m pip install --upgrade pip pyinstaller\n"
            "      - name: Build\n"
            "        run: pyinstaller --noconfirm --windowed --name app --distpath dist src/main.py || pyinstaller --noconfirm --onefile --name app --distpath dist src/main.py\n"
            "      - name: Package\n"
            "        run: Compress-Archive -Path dist/* -DestinationPath app-windows.zip\n"
            "      - uses: actions/upload-artifact@v4\n"
            "        with:\n"
            "          name: app-windows\n"
            "          path: app-windows.zip\n"
        )

        def linux_job(distro, image, install):
            return (
                f"  build-{distro}:\n"
                f"    name: Build (Linux {distro.capitalize()})\n"
                "    runs-on: ubuntu-latest\n"
                f"    container: {image}\n"
                "    steps:\n"
                "      - uses: actions/checkout@v4\n"
                "      - name: Install build dependencies\n"
                f"        run: {install}\n"
                "      - name: Build\n"
                f"        run: pyinstaller --noconfirm --onefile --name app-{distro} --distpath dist src/main.py\n"
                "      - name: Package\n"
                f"        run: tar -czf app-{distro}.tar.gz -C dist .\n"
                "      - uses: actions/upload-artifact@v4\n"
                "        with:\n"
                f"          name: app-{distro}\n"
                f"          path: app-{distro}.tar.gz\n"
            )

        fedora_job = linux_job(
            "fedora", "fedora:latest",
            "dnf -y install python3 python3-pip python3-tkinter binutils && "
            "pip3 install --upgrade pyinstaller",
        )
        debian_job = linux_job(
            "debian", "debian:latest",
            "apt-get update && apt-get -y install python3 python3-pip python3-tk binutils && "
            "pip3 install --break-system-packages --upgrade pyinstaller",
        )

        jobs = []
        needs = []
        if "windows" in targets:
            jobs.append(win_job); needs.append("build-windows")
        if "fedora" in targets:
            jobs.append(fedora_job); needs.append("build-fedora")
        if "debian" in targets:
            jobs.append(debian_job); needs.append("build-debian")

        needs_yaml = "".join(f"      - {n}\n" for n in needs)
        release_job = (
            "  release:\n"
            "    name: Publish GitHub Release\n"
            "    needs:\n"
            f"{needs_yaml}"
            "    runs-on: ubuntu-latest\n"
            "    if: startsWith(github.ref, 'refs/tags/')\n"
            "    steps:\n"
            "      - uses: actions/download-artifact@v4\n"
            "        with:\n"
            "          path: artifacts\n"
            "      - name: Publish release\n"
            "        uses: softprops/action-gh-release@v2\n"
            "        with:\n"
            "          files: artifacts/**/*\n"
        )

        return header + "\n".join(jobs) + "\n" + release_job

    def _rel_toggle(self):
        state = "normal" if self.rel_enable_var.get() else "disabled"
        for c in getattr(self, "_rel_checks", []):
            c.configure(state=state)

    # ===== self-update =====================================================
    @staticmethod
    def _parse_version(text):
        m = re.search(r"APP_VERSION\s*=\s*[\"']([0-9]+(?:\.[0-9]+)*)[\"']", text or "")
        return m.group(1) if m else None

    @staticmethod
    def _version_tuple(v):
        try:
            return tuple(int(x) for x in v.split("."))
        except Exception:
            return (0,)

    def _check_for_updates(self):
        def work():
            self.log(f"Checking for updates from {UPDATE_REPO}\u2026")
            req = urllib.request.Request(UPDATE_RAW_URL, method="GET")
            req.add_header("User-Agent", "GitHub-PR-Agent")
            req.add_header("Accept", "text/plain")
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    remote_src = resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                raise RuntimeError(
                    f"Update check failed (HTTP {e.code}). The update repo "
                    f"'{UPDATE_REPO}' or file '{UPDATE_SCRIPT_NAME}' may not exist yet.")
            except urllib.error.URLError as e:
                raise RuntimeError(f"Network error checking for updates: {e.reason}")

            remote_ver = self._parse_version(remote_src)
            if not remote_ver:
                raise RuntimeError("Could not read the version of the remote script.")
            self.log(f"Installed v{APP_VERSION}; latest v{remote_ver}.")

            if self._version_tuple(remote_ver) <= self._version_tuple(APP_VERSION):
                self.log("Already up to date.", "OK")
                self.alert("Check for updates",
                           f"You are on the latest version (v{APP_VERSION}).", "info")
                return

            self.root.after(0, lambda: self._prompt_apply_update(remote_src, remote_ver))
        self._async(work, "Check for updates")

    def _prompt_apply_update(self, remote_src, remote_ver):
        if not messagebox.askyesno(
                "Update available",
                f"A newer version is available.\n\nInstalled: v{APP_VERSION}\n"
                f"Latest: v{remote_ver}\n\nDownload and install it now? "
                "The current script is backed up, this window closes, and the new "
                "version launches automatically."):
            return
        self._async(lambda: self._apply_update(remote_src, remote_ver), "Apply update")

    def _resolve_self_path(self) -> Path:
        """Best-effort absolute path of the running .py script, tolerant of odd
        launch methods (pythonw, moved cwd, argv[0])."""
        candidates = []
        for getter in (lambda: Path(__file__).resolve(),
                       lambda: Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None,
                       lambda: app_base_dir() / UPDATE_SCRIPT_NAME):
            try:
                p = getter()
            except Exception:
                p = None
            if p is not None and p not in candidates:
                candidates.append(p)
        for p in candidates:
            try:
                if p.exists():
                    return p
            except Exception:
                pass
        return candidates[0] if candidates else Path(__file__)

    def _apply_update(self, remote_src, remote_ver):
        if getattr(sys, "frozen", False):
            raise RuntimeError(
                "This is a packaged EXE build; self-update replaces the source script only. "
                "Rebuild the EXE from the updated source, or run from Python to self-update.")
        target = self._resolve_self_path()
        if not target.exists():
            raise RuntimeError(
                f"Could not locate the running script to update (looked at: {target}). "
                "If you launched from a moved, renamed, or cloud-placeholder copy, restart the "
                "app from the installed GitHub_PR_Agent.py and try again.")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        backup = target.with_suffix(f".py.bak-v{APP_VERSION}")
        # The backup is a safety convenience — never let it abort the update.
        try:
            shutil.copy2(target, backup)
            self.log(f"Backed up current script \u2192 {backup.name}", "OK")
        except Exception as e:
            backup = None
            self.log(f"Could not back up current script (continuing anyway): {e}", "WARN")
        try:
            target.write_text(remote_src, encoding="utf-8")
            self.log(f"Updated {target.name} to v{remote_ver}.", "OK")
        except Exception as e:
            raise RuntimeError(f"Could not write the update to {target}: {e}")
        backup_name = backup.name if backup else "none"
        self.root.after(0, lambda: self._restart_into_new_version(target, remote_ver, backup_name))

    def _restart_into_new_version(self, target, remote_ver, backup_name):
        """Persist config, launch the updated script detached, and close this app."""
        self.save_config()
        self.log(f"Restarting into v{remote_ver} \u2026", "OK")
        py = sys.executable or shutil.which("pythonw") or shutil.which("python") or "python"
        try:
            flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            subprocess.Popen([py, str(target)],
                             cwd=str(target.parent),
                             creationflags=flags, close_fds=True)
        except Exception as e:
            # If relaunch fails, don't kill the app silently — tell the user.
            self.alert("Update installed",
                       f"Updated to v{remote_ver} (backup: {backup_name}), but the automatic "
                       f"restart failed: {e}\n\nPlease close and reopen GitHub PR Agent manually.",
                       "warn")
            return
        # Give the new process a moment to spawn, then exit this one.
        self.root.after(400, self.root.destroy)


def main():
    root = tk.Tk()
    GitHubPRAgent(root)
    root.mainloop()


if __name__ == "__main__":
    main()
