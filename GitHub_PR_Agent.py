#!/usr/bin/env python3
"""
GitHub_PR_Agent_OptionB.py
==========================
Generic GitHub publishing gateway with optional build automation.

Purpose
-------
This tool lets users publish different local projects to GitHub from the same
machine without relying on GitHub Desktop. It does not assume a fixed project
name, a fixed Python file, WoA_Sync_Agent.py, GitHub_PR_Agent.py, or src/main.py.

Key behavior
------------
1. If build automation is NOT selected, the app creates or reuses a GitHub repo,
   pushes the selected folder, validates files, and opens the repo. No workflow
   files are created.
2. If build automation IS selected, the app detects or asks for the project type,
   creates missing dependency files when safe, writes a matching GitHub Actions
   workflow under .github/workflows/build.yml, then pushes it with the project.
3. The Build button keeps the prior behavior: push or recreate a version tag to
   trigger Actions. It can also create the workflow before tagging if selected.

Runtime dependencies
--------------------
Python standard library only plus tkinter. Git must be available on PATH or via
./vendor/PortableGit next to this script or packaged executable.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

APP_NAME = "GitHub PR Agent"
APP_VERSION = "2.4.0"
UPDATE_REPO = "SomeGuru/GitHub-PR-Agent"
UPDATE_BRANCH = "main"
UPDATE_SCRIPT_NAME = "GitHub_PR_Agent.py"
UPDATE_RAW_URL = f"https://raw.githubusercontent.com/{UPDATE_REPO}/{UPDATE_BRANCH}/{UPDATE_SCRIPT_NAME}"
UPDATE_RELEASES_API = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
UPDATE_RELEASES_URL = f"https://github.com/{UPDATE_REPO}/releases/latest"
CONFIG_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "GitHubPRAgent"
CONFIG_FILE = CONFIG_DIR / "config.json"
VAULT_FILE = CONFIG_DIR / "vault.json"
VAULT_RESET_PHRASE = "MikeLariosWasHere!"
VAULT_KDF_ITERATIONS = 200_000

CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

BUILD_TYPES = [
    "Auto Detect",
    "Python",
    "C# / .NET",
    "JavaScript / Node",
    "Static Website",
    "Go",
    "Rust",
    "Java Maven",
    "Java Gradle",
    "None",
]
OUTPUT_MODES = ["Upload artifacts only", "Release on tags"]
CSHARP_KINDS = ["Windows Desktop App", "Console App"]

# ---------------------------------------------------------------------------
# Windows 11 style theming (stdlib ttk only, no external dependencies)
# ---------------------------------------------------------------------------
THEMES = {
    "light": {
        "bg": "#f3f3f3",
        "surface": "#ffffff",
        "surface_alt": "#fafafa",
        "border": "#d9d9d9",
        "fg": "#1b1b1b",
        "muted": "#5c5c5c",
        "accent": "#0067c0",
        "accent_hover": "#1a75c7",
        "accent_fg": "#ffffff",
        "field": "#ffffff",
        "field_fg": "#1b1b1b",
        "ok": "#0a7d28",
        "err": "#c42b1c",
        "warn": "#9d5d00",
        "console_bg": "#0c0c0c",
        "console_fg": "#d0d0d0",
    },
    "dark": {
        "bg": "#202020",
        "surface": "#2b2b2b",
        "surface_alt": "#262626",
        "border": "#3d3d3d",
        "fg": "#eaeaea",
        "muted": "#a8a8a8",
        "accent": "#4cc2ff",
        "accent_hover": "#69cbff",
        "accent_fg": "#00131f",
        "field": "#1c1c1c",
        "field_fg": "#eaeaea",
        "ok": "#6ccb7a",
        "err": "#ff99a4",
        "warn": "#ffcf70",
        "console_bg": "#0c0c0c",
        "console_fg": "#d0d0d0",
    },
}

# ---------------------------------------------------------------------------
# Git location
# ---------------------------------------------------------------------------
def app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_git() -> str:
    override = os.environ.get("GITHUB_PR_AGENT_GIT")
    if override and Path(override).exists():
        return override
    bases = [app_base_dir()]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bases.append(Path(meipass))
    for base in bases:
        for rel in ("vendor/PortableGit/cmd/git.exe", "vendor/PortableGit/bin/git.exe"):
            p = base / rel
            if p.exists():
                return str(p)
    return shutil.which("git") or "git"


# ---------------------------------------------------------------------------
# Lightweight encrypted PAT vault, stdlib only
# ---------------------------------------------------------------------------
def _vault_derive_keys(passphrase: str, salt: bytes, iterations: int) -> tuple[bytes, bytes]:
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
    except Exception as e:
        raise ValueError("Vault file is malformed or corrupted.") from e
    enc_key, mac_key = _vault_derive_keys(passphrase, salt, iterations)
    expected = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, tag):
        raise ValueError("Wrong passphrase, or the vault has been tampered with.")
    ks = _vault_keystream(enc_key, nonce, len(ct))
    return bytes(a ^ b for a, b in zip(ct, ks)).decode("utf-8")


def vault_load() -> dict:
    """Return the multi-user vault as {"v": 2, "entries": {label: blob}}.
    Transparently wraps a legacy v1 single-entry file (encrypted blob at the top
    level) under the label 'default' without needing the passphrase."""
    if not VAULT_FILE.exists():
        return {"v": 2, "entries": {}}
    try:
        data = json.loads(VAULT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"v": 2, "entries": {}}
    if isinstance(data, dict) and isinstance(data.get("entries"), dict):
        return {"v": 2, "entries": data["entries"]}
    # Legacy v1: the file itself is a single encrypted blob.
    if isinstance(data, dict) and data.get("ct") and data.get("tag"):
        return {"v": 2, "entries": {"default": data}}
    return {"v": 2, "entries": {}}


def vault_save(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"v": 2, "entries": data.get("entries", {})}
    VAULT_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Build profile engine
# ---------------------------------------------------------------------------
def safe_rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def find_files(root: Path, patterns: tuple[str, ...]) -> list[Path]:
    found = []
    for pat in patterns:
        found.extend(p for p in root.rglob(pat) if p.is_file() and ".git" not in p.parts)
    return sorted(set(found))


def detect_project_type(root: Path) -> str | None:
    checks = [
        (("*.csproj", "*.sln"), "C# / .NET"),
        (("package.json",), "JavaScript / Node"),
        (("pyproject.toml", "requirements.txt", "*.py"), "Python"),
        (("go.mod",), "Go"),
        (("Cargo.toml",), "Rust"),
        (("pom.xml",), "Java Maven"),
        (("build.gradle", "build.gradle.kts"), "Java Gradle"),
        (("index.html",), "Static Website"),
    ]
    for patterns, name in checks:
        if find_files(root, patterns):
            return name
    return None


def choose_python_entry_interactive(root: tk.Tk, project_root: Path, current: str = "") -> str:
    py_files = [p for p in find_files(project_root, ("*.py",)) if p.name not in {"setup.py"}]
    if current and (project_root / current).exists():
        return current
    if not py_files:
        raise RuntimeError("Python build selected, but no .py files were found.")
    preferred = ["main.py", "app.py", "run.py", "gui.py"]
    for name in preferred:
        for p in py_files:
            if p.name.lower() == name:
                if len(py_files) == 1:
                    return safe_rel(p, project_root)
                break
    if len(py_files) == 1:
        return safe_rel(py_files[0], project_root)
    messagebox.showinfo(
        "Select Python entry file",
        "Multiple Python files were found. Select the file that starts the application.",
        parent=root,
    )
    selected = filedialog.askopenfilename(
        title="Select main Python application file",
        initialdir=str(project_root),
        filetypes=[("Python files", "*.py"), ("All files", "*.*")],
        parent=root,
    )
    if not selected:
        raise RuntimeError("Python build needs a main .py file. Select one or change the build type.")
    p = Path(selected).resolve()
    try:
        return safe_rel(p, project_root.resolve())
    except ValueError as e:
        raise RuntimeError("Selected Python file must be inside the project folder.") from e


def ensure_python_files(project_root: Path, entry_file: str) -> list[str]:
    created = []
    if not (project_root / "requirements.txt").exists():
        (project_root / "requirements.txt").write_text("", encoding="utf-8")
        created.append("requirements.txt")
    if not entry_file:
        py_files = find_files(project_root, ("*.py",))
        if not py_files:
            (project_root / "main.py").write_text(
                "def main():\n    print('Hello from Python')\n\nif __name__ == '__main__':\n    main()\n",
                encoding="utf-8",
            )
            created.append("main.py")
    return created


def ensure_node_files(project_root: Path) -> list[str]:
    created = []
    pkg = project_root / "package.json"
    if not pkg.exists():
        js_files = find_files(project_root, ("*.js", "*.mjs", "*.cjs"))
        entry = safe_rel(js_files[0], project_root) if js_files else "index.js"
        pkg.write_text(json.dumps({
            "name": re.sub(r"[^a-zA-Z0-9_-]+", "-", project_root.name).lower(),
            "version": "1.0.0",
            "private": True,
            "scripts": {
                "build": "echo No build script configured",
                "test": "echo No tests configured"
            },
            "main": entry,
        }, indent=2) + "\n", encoding="utf-8")
        created.append("package.json")
    if not find_files(project_root, ("*.js", "*.mjs", "*.cjs", "*.ts", "*.tsx", "*.jsx")):
        (project_root / "index.js").write_text("console.log('Hello from Node');\n", encoding="utf-8")
        created.append("index.js")
    return created


def ensure_csharp_files(project_root: Path, kind: str) -> list[str]:
    created = []
    if not find_files(project_root, ("*.csproj",)):
        name = re.sub(r"[^A-Za-z0-9_.-]+", "", project_root.name) or "App"
        if kind == "Windows Desktop App":
            text = (
                '<Project Sdk="Microsoft.NET.Sdk">\n'
                '  <PropertyGroup>\n'
                '    <OutputType>WinExe</OutputType>\n'
                '    <TargetFramework>net8.0-windows</TargetFramework>\n'
                '    <UseWindowsForms>true</UseWindowsForms>\n'
                '    <Nullable>enable</Nullable>\n'
                '    <ImplicitUsings>enable</ImplicitUsings>\n'
                '  </PropertyGroup>\n'
                '</Project>\n'
            )
        else:
            text = (
                '<Project Sdk="Microsoft.NET.Sdk">\n'
                '  <PropertyGroup>\n'
                '    <OutputType>Exe</OutputType>\n'
                '    <TargetFramework>net8.0</TargetFramework>\n'
                '    <Nullable>enable</Nullable>\n'
                '    <ImplicitUsings>enable</ImplicitUsings>\n'
                '  </PropertyGroup>\n'
                '</Project>\n'
            )
        (project_root / f"{name}.csproj").write_text(text, encoding="utf-8")
        created.append(f"{name}.csproj")
    if not find_files(project_root, ("*.cs",)):
        if kind == "Windows Desktop App":
            (project_root / "Program.cs").write_text(
                "using System;\nusing System.Windows.Forms;\n\nApplicationConfiguration.Initialize();\nMessageBox.Show(\"Hello from Windows Desktop App\");\n",
                encoding="utf-8",
            )
        else:
            (project_root / "Program.cs").write_text("Console.WriteLine(\"Hello from .NET\");\n", encoding="utf-8")
        created.append("Program.cs")
    return created


def ensure_go_files(project_root: Path) -> list[str]:
    created = []
    if not (project_root / "go.mod").exists():
        module = re.sub(r"[^a-zA-Z0-9_./-]+", "", project_root.name).lower() or "app"
        (project_root / "go.mod").write_text(f"module {module}\n\ngo 1.22\n", encoding="utf-8")
        created.append("go.mod")
    if not find_files(project_root, ("*.go",)):
        (project_root / "main.go").write_text('package main\n\nimport "fmt"\n\nfunc main() { fmt.Println("Hello from Go") }\n', encoding="utf-8")
        created.append("main.go")
    return created


def ensure_rust_files(project_root: Path) -> list[str]:
    created = []
    if not (project_root / "Cargo.toml").exists():
        (project_root / "Cargo.toml").write_text(
            f"[package]\nname = \"{re.sub(r'[^a-zA-Z0-9_-]+', '-', project_root.name).lower() or 'app'}\"\nversion = \"0.1.0\"\nedition = \"2021\"\n\n[dependencies]\n",
            encoding="utf-8",
        )
        created.append("Cargo.toml")
    src = project_root / "src"
    if not find_files(project_root, ("*.rs",)):
        src.mkdir(exist_ok=True)
        (src / "main.rs").write_text('fn main() { println!("Hello from Rust"); }\n', encoding="utf-8")
        created.append("src/main.rs")
    return created


def ensure_java_files(project_root: Path, build_type: str) -> list[str]:
    created = []
    if build_type == "Java Maven":
        if not (project_root / "pom.xml").exists():
            (project_root / "pom.xml").write_text(
                "<project xmlns=\"http://maven.apache.org/POM/4.0.0\" xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\" xsi:schemaLocation=\"http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd\">\n"
                "  <modelVersion>4.0.0</modelVersion>\n  <groupId>local.app</groupId>\n  <artifactId>app</artifactId>\n  <version>1.0.0</version>\n"
                "  <properties><maven.compiler.source>17</maven.compiler.source><maven.compiler.target>17</maven.compiler.target></properties>\n</project>\n",
                encoding="utf-8",
            )
            created.append("pom.xml")
    else:
        if not (project_root / "build.gradle").exists() and not (project_root / "build.gradle.kts").exists():
            (project_root / "build.gradle").write_text("plugins { id 'java' }\n\ngroup = 'local.app'\nversion = '1.0.0'\n", encoding="utf-8")
            created.append("build.gradle")
    if not find_files(project_root, ("*.java",)):
        src_dir = project_root / "src" / "main" / "java"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "Main.java").write_text('public class Main { public static void main(String[] args) { System.out.println("Hello from Java"); } }\n', encoding="utf-8")
        created.append("src/main/java/Main.java")
    return created


def ensure_static_files(project_root: Path) -> list[str]:
    created = []
    if not (project_root / "index.html").exists():
        (project_root / "index.html").write_text("<!doctype html><html><body><h1>Hello from GitHub PR Agent</h1></body></html>\n", encoding="utf-8")
        created.append("index.html")
    return created


def ensure_project_dependencies(project_root: Path, build_type: str, py_entry: str, csharp_kind: str) -> list[str]:
    if build_type == "Python":
        return ensure_python_files(project_root, py_entry)
    if build_type == "JavaScript / Node":
        return ensure_node_files(project_root)
    if build_type == "C# / .NET":
        return ensure_csharp_files(project_root, csharp_kind)
    if build_type == "Go":
        return ensure_go_files(project_root)
    if build_type == "Rust":
        return ensure_rust_files(project_root)
    if build_type in {"Java Maven", "Java Gradle"}:
        return ensure_java_files(project_root, build_type)
    if build_type == "Static Website":
        return ensure_static_files(project_root)
    return []


def yaml_list(items: list[str], indent: str = "      ") -> str:
    return "\n".join(f"{indent}- {item}" for item in items)


def render_workflow(build_type: str, branch: str, output_mode: str, py_entry: str, csharp_kind: str, targets: dict, app_name: str = "app") -> str:
    branch = branch or "main"
    app = re.sub(r"[^A-Za-z0-9._-]+", "-", (app_name or "app").strip()).strip("-") or "app"
    release_enabled = output_mode == "Release on tags"
    on_block = (
        "on:\n"
        "  workflow_dispatch:\n"
        "  push:\n"
        "    branches:\n"
        f"      - {branch}\n"
        "    tags:\n"
        "      - 'v*'\n"
    )
    header = (
        "name: Build Project\n\n"
        "# Generated by GitHub PR Agent. Build profile is selected by the user.\n"
        f"# Build type: {build_type}\n"
        f"# Output mode: {output_mode}\n\n"
        f"{on_block}\n"
        "permissions:\n"
        "  contents: write\n\n"
        "jobs:\n"
    )

    jobs: list[str] = []
    needs: list[str] = []

    if build_type == "Python":
        entry = py_entry or "main.py"
        if targets.get("windows", True):
            jobs.append(f"""  build-windows:
    name: Build {app} (Windows)
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - name: Install dependencies
        shell: pwsh
        run: |
          python -m pip install --upgrade pip pyinstaller
          if (Test-Path requirements.txt) {{ python -m pip install -r requirements.txt }}
      - name: Build single-file executable
        run: pyinstaller --noconfirm --onefile --windowed --name "{app}" --distpath dist "{entry}"
      - uses: actions/upload-artifact@v4
        with:
          name: {app}-windows
          path: dist/{app}.exe
""")
            needs.append("build-windows")
        for distro, image, install, pipflags, envblock in [
            ("fedora", "fedora:latest", "dnf -y install python3 python3-pip python3-tkinter binutils && pip3 install --upgrade pyinstaller", "", ""),
            ("debian", "debian:latest", "apt-get update && apt-get -y install python3 python3-pip python3-tk binutils && pip3 install --break-system-packages --upgrade pyinstaller", "--break-system-packages ", "    env:\n      PIP_BREAK_SYSTEM_PACKAGES: \"1\"\n"),
        ]:
            if targets.get(distro, False):
                jobs.append(f"""  build-{distro}:
    name: Build {app} ({distro})
    runs-on: ubuntu-latest
    container: {image}
{envblock}    steps:
      - uses: actions/checkout@v4
      - name: Install dependencies
        run: {install}
      - name: Install project dependencies
        run: if [ -f requirements.txt ]; then pip3 install {pipflags}-r requirements.txt; fi
      - name: Build single-file executable
        run: pyinstaller --noconfirm --onefile --name "{app}-{distro}" --distpath dist "{entry}"
      - uses: actions/upload-artifact@v4
        with:
          name: {app}-{distro}
          path: dist/{app}-{distro}
""")
                needs.append(f"build-{distro}")

    elif build_type == "C# / .NET":
        # Windows desktop is Windows-only. Console can run cross-platform, but keep Windows default.
        win_only = csharp_kind == "Windows Desktop App"
        if targets.get("windows", True) or win_only:
            jobs.append("""  build-windows:
    name: Build .NET (Windows)
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-dotnet@v4
        with:
          dotnet-version: '8.0.x'
      - name: Restore
        run: dotnet restore
      - name: Publish
        run: dotnet publish -c Release -o dist
      - name: Package
        run: Compress-Archive -Path dist/* -DestinationPath app-windows.zip
      - uses: actions/upload-artifact@v4
        with:
          name: app-windows
          path: app-windows.zip
""")
            needs.append("build-windows")
        if not win_only:
            for osname, runner in [("linux", "ubuntu-latest"), ("macos", "macos-latest")]:
                if targets.get(osname, False):
                    jobs.append(f"""  build-{osname}:
    name: Build .NET ({osname})
    runs-on: {runner}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-dotnet@v4
        with:
          dotnet-version: '8.0.x'
      - name: Restore
        run: dotnet restore
      - name: Publish
        run: dotnet publish -c Release -o dist
      - name: Package
        run: tar -czf app-{osname}.tar.gz -C dist .
      - uses: actions/upload-artifact@v4
        with:
          name: app-{osname}
          path: app-{osname}.tar.gz
""")
                    needs.append(f"build-{osname}")

    elif build_type == "JavaScript / Node":
        jobs.append("""  build-node:
    name: Build JavaScript / Node
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
      - name: Install dependencies
        run: npm install
      - name: Build if script exists
        run: npm run build --if-present
      - name: Test if script exists
        run: npm test --if-present
      - name: Package
        run: |
          mkdir -p dist
          tar --exclude=.git --exclude=node_modules -czf app-node.tar.gz .
      - uses: actions/upload-artifact@v4
        with:
          name: app-node
          path: app-node.tar.gz
""")
        needs.append("build-node")

    elif build_type == "Go":
        jobs.append("""  build-go:
    name: Build Go
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with:
          go-version: '1.22'
      - name: Build
        run: |
          mkdir -p dist
          go build -o dist/app ./...
      - name: Package
        run: tar -czf app-go.tar.gz -C dist .
      - uses: actions/upload-artifact@v4
        with:
          name: app-go
          path: app-go.tar.gz
""")
        needs.append("build-go")

    elif build_type == "Rust":
        jobs.append("""  build-rust:
    name: Build Rust
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions-rust-lang/setup-rust-toolchain@v1
      - name: Build
        run: cargo build --release
      - name: Package
        run: tar -czf app-rust.tar.gz -C target/release .
      - uses: actions/upload-artifact@v4
        with:
          name: app-rust
          path: app-rust.tar.gz
""")
        needs.append("build-rust")

    elif build_type == "Java Maven":
        jobs.append("""  build-java-maven:
    name: Build Java Maven
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-java@v4
        with:
          distribution: 'temurin'
          java-version: '17'
      - name: Build
        run: mvn -B package
      - uses: actions/upload-artifact@v4
        with:
          name: app-java-maven
          path: target/**/*
""")
        needs.append("build-java-maven")

    elif build_type == "Java Gradle":
        jobs.append("""  build-java-gradle:
    name: Build Java Gradle
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-java@v4
        with:
          distribution: 'temurin'
          java-version: '17'
      - name: Build
        run: gradle build
      - uses: actions/upload-artifact@v4
        with:
          name: app-java-gradle
          path: build/libs/**/*
""")
        needs.append("build-java-gradle")

    elif build_type == "Static Website":
        jobs.append("""  package-static:
    name: Package static website
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Package
        run: tar --exclude=.git -czf static-site.tar.gz .
      - uses: actions/upload-artifact@v4
        with:
          name: static-site
          path: static-site.tar.gz
""")
        needs.append("package-static")

    if not jobs:
        jobs.append("""  package-source:
    name: Package source
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Package source
        run: tar --exclude=.git -czf source.tar.gz .
      - uses: actions/upload-artifact@v4
        with:
          name: source
          path: source.tar.gz
""")
        needs.append("package-source")

    release_job = ""
    if release_enabled:
        release_job = (
            "\n  release:\n"
            "    name: Publish GitHub Release\n"
            "    if: startsWith(github.ref, 'refs/tags/')\n"
            "    needs:\n"
            f"{yaml_list(needs)}\n"
            "    runs-on: ubuntu-latest\n"
            "    permissions:\n"
            "      contents: write\n"
            "    steps:\n"
            "      - uses: actions/download-artifact@v4\n"
            "        with:\n"
            "          path: artifacts\n"
            "      - name: Publish release\n"
            "        uses: softprops/action-gh-release@v2\n"
            "        with:\n"
            "          tag_name: ${{ github.ref_name }}\n"
            "          files: artifacts/**/*\n"
            "          fail_on_unmatched_files: false\n"
            "          generate_release_notes: true\n"
            "          token: ${{ secrets.GITHUB_TOKEN }}\n"
        )

    return header + "\n".join(jobs) + release_job + "\n"


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------
class GitHubPRAgent:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(f"{APP_NAME} v{APP_VERSION}")
        root.geometry("1180x880")
        root.minsize(980, 700)

        self.git_exe = find_git()
        self.gh_token = ""
        self.gh_user = ""
        self.gh_scopes = None
        self.pub_repo_full = ""
        self.pub_repo_url = ""
        self.pub_default_branch = "main"
        self.pub_repo_private = False
        self.pub_source_dir = ""
        self.build_repo = ""
        self._status_labels = []
        self._canvases = []

        self.cfg = self._load_config()
        self.theme_name = "dark" if str(self.cfg.get("theme", "light")).lower() == "dark" else "light"
        self.style = ttk.Style(self.root)
        self._build_ui()
        self._apply_theme()
        self._install_excepthook()
        self.log(f"{APP_NAME} ready. Version {APP_VERSION}", "OK")
        self.log(f"git: {self.git_exe}")

    def _load_config(self) -> dict:
        try:
            if CONFIG_FILE.exists():
                return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def save_config(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "pub_name": self.pub_name_var.get().strip(),
                "pub_desc": self.pub_desc_var.get().strip(),
                "pub_private": bool(self.pub_private_var.get()),
                "pub_source": self.pub_source_var.get().strip(),
                "pub_branch": self.pub_branch_var.get().strip(),
                "pub_commit": self.pub_commit_var.get().strip(),
                "pub_scaffold": bool(self.pub_scaffold_var.get()),
                "build_enabled": bool(self.build_enabled_var.get()),
                "build_type": self.build_type_var.get(),
                "build_output_mode": self.build_output_var.get(),
                "py_entry": self.py_entry_var.get().strip(),
                "csharp_kind": self.csharp_kind_var.get(),
                "target_windows": bool(self.target_windows_var.get()),
                "target_linux": bool(self.target_linux_var.get()),
                "target_macos": bool(self.target_macos_var.get()),
                "target_fedora": bool(self.target_fedora_var.get()),
                "target_debian": bool(self.target_debian_var.get()),
                "build_repo": self.build_repo,
                "theme": self.theme_name,
            }
            CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            self.log(f"Could not save config: {e}", "WARN")

    # Theming ---------------------------------------------------------------
    def _apply_theme(self):
        p = THEMES.get(self.theme_name, THEMES["light"])
        st = self.style
        try:
            st.theme_use("clam")
        except Exception:
            pass
        self.root.configure(bg=p["bg"])
        st.configure(".", background=p["bg"], foreground=p["fg"], fieldbackground=p["field"],
                     bordercolor=p["border"], focuscolor=p["accent"])
        st.configure("TFrame", background=p["bg"])
        st.configure("TPanedwindow", background=p["bg"])
        st.configure("TLabel", background=p["bg"], foreground=p["fg"])
        st.configure("Muted.TLabel", background=p["bg"], foreground=p["muted"])
        st.configure("TLabelframe", background=p["bg"], bordercolor=p["border"], relief="solid")
        st.configure("TLabelframe.Label", background=p["bg"], foreground=p["accent"], font=("Segoe UI", 10, "bold"))
        st.configure("TCheckbutton", background=p["bg"], foreground=p["fg"])
        st.map("TCheckbutton", background=[("active", p["bg"])], foreground=[("disabled", p["muted"])])
        st.configure("TButton", background=p["surface"], foreground=p["fg"], bordercolor=p["border"],
                     focusthickness=1, focuscolor=p["accent"], padding=(10, 5), relief="flat", font=("Segoe UI", 9))
        st.map("TButton",
               background=[("active", p["surface_alt"]), ("pressed", p["border"]), ("disabled", p["bg"])],
               foreground=[("disabled", p["muted"])],
               bordercolor=[("active", p["accent"])])
        st.configure("Accent.TButton", background=p["accent"], foreground=p["accent_fg"], bordercolor=p["accent"],
                     padding=(12, 6), relief="flat", font=("Segoe UI", 9, "bold"))
        st.map("Accent.TButton",
               background=[("active", p["accent_hover"]), ("pressed", p["accent"]), ("disabled", p["border"])],
               foreground=[("disabled", p["muted"])])
        st.configure("TEntry", fieldbackground=p["field"], foreground=p["field_fg"], bordercolor=p["border"],
                     insertcolor=p["fg"], padding=3)
        st.map("TEntry", bordercolor=[("focus", p["accent"])])
        st.configure("TCombobox", fieldbackground=p["field"], foreground=p["field_fg"], bordercolor=p["border"],
                     arrowcolor=p["fg"], padding=3)
        st.map("TCombobox", fieldbackground=[("readonly", p["field"])], bordercolor=[("focus", p["accent"])])
        st.configure("Vertical.TScrollbar", background=p["surface_alt"], troughcolor=p["bg"],
                     bordercolor=p["bg"], arrowcolor=p["fg"])
        st.configure("Horizontal.TScrollbar", background=p["surface_alt"], troughcolor=p["bg"],
                     bordercolor=p["bg"], arrowcolor=p["fg"])
        for cv in self._canvases:
            try:
                cv.configure(bg=p["bg"])
            except Exception:
                pass
        if hasattr(self, "console"):
            self.console.configure(bg=p["console_bg"], fg=p["console_fg"], insertbackground=p["console_fg"])
            self.console.tag_config("ERROR", foreground="#ff6b6b")
            self.console.tag_config("WARN", foreground="#ffd166")
            self.console.tag_config("OK", foreground="#7bd88f")
        if hasattr(self, "theme_btn"):
            self.theme_btn.config(text=("🌙 Dark" if self.theme_name == "light" else "☀ Light"))

    def _toggle_theme(self):
        self.theme_name = "dark" if self.theme_name == "light" else "light"
        self._apply_theme()
        self.log(f"Theme switched to {self.theme_name}.", "OK")
        try:
            self.save_config()
        except Exception:
            pass

    def _build_ui(self):
        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True)
        paned = ttk.PanedWindow(outer, orient="vertical")
        paned.pack(fill="both", expand=True)
        top = ttk.Frame(paned)
        paned.add(top, weight=3)
        body = self._scrollable(top)

        ttk.Label(
            body,
            text=(
                "Create or reuse a GitHub repo, push any local project, and optionally add selected build automation. "
                "No build workflow is written unless you enable build automation."
            ),
            wraplength=1120,
            justify="left",
        ).pack(anchor="w", padx=10, pady=(8, 4))

        self._build_login(body)
        self._build_create_repo(body)
        self._build_build_options(body)
        self._build_push_validate_open(body)

        term = ttk.LabelFrame(paned, text="GitHub Activity")
        paned.add(term, weight=1)
        self.console = scrolledtext.ScrolledText(term, height=12, wrap="word", font=("Consolas", 9), bg="#0c0c0c", fg="#d0d0d0")
        self.console.pack(fill="both", expand=True, padx=4, pady=4)
        self.console.tag_config("ERROR", foreground="#ff6b6b")
        self.console.tag_config("WARN", foreground="#ffd166")
        self.console.tag_config("OK", foreground="#7bd88f")
        btns = ttk.Frame(term)
        btns.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Button(btns, text="Clear log", command=lambda: self.console.delete("1.0", "end")).pack(side="left")
        ttk.Button(btns, text="Save Activity Window", command=self._save_activity_window).pack(side="left", padx=6)
        ttk.Button(btns, text="⬆ Push main", command=self._push_main).pack(side="left", padx=6)
        ttk.Button(btns, text="Build", command=self.open_build_release, style="Accent.TButton").pack(side="left", padx=6)
        ttk.Button(btns, text="🔒 PAT Vault", command=self.open_vault).pack(side="left", padx=6)
        ttk.Button(btns, text="⬭ Check for updates", command=self._check_for_updates).pack(side="left", padx=6)
        ttk.Label(btns, text=f"v{APP_VERSION}", style="Muted.TLabel").pack(side="right", padx=6)
        self.theme_btn = ttk.Button(btns, text="🌙 Dark", command=self._toggle_theme)
        self.theme_btn.pack(side="right", padx=6)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _scrollable(self, parent):
        canvas = tk.Canvas(parent, highlightthickness=0)
        self._canvases.append(canvas)
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
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        return inner

    def _build_login(self, parent):
        s = ttk.LabelFrame(parent, text="Step 1: 🔐 Log in to GitHub")
        s.pack(fill="x", padx=10, pady=5)
        row = ttk.Frame(s)
        row.pack(fill="x", padx=6, pady=6)
        ttk.Label(row, text="🔑 Personal Access Token:").pack(side="left")
        self.token_var = tk.StringVar()
        self.token_entry = ttk.Entry(row, textvariable=self.token_var, show="•", width=52)
        self.token_entry.pack(side="left", padx=6)
        self.show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="👁 Show PAT", variable=self.show_var, command=self._toggle_token).pack(side="left")
        ttk.Button(row, text="🔗 Connect", command=self._connect, style="Accent.TButton").pack(side="left", padx=6)
        ttk.Button(row, text="🔒 PAT Vault", command=self.open_vault).pack(side="left", padx=6)
        self.status_label = ttk.Label(row, text="Not connected", foreground="#a00")
        self.status_label.pack(side="left", padx=8)
        self._status_labels.append(self.status_label)
        ttk.Label(s, foreground="#666", text="Classic token needs repo scope. Add workflow scope only if you enable build automation.").pack(anchor="w", padx=6, pady=(0, 4))

    def _build_create_repo(self, parent):
        s = ttk.LabelFrame(parent, text="Step 2: Create or reuse repository")
        s.pack(fill="x", padx=10, pady=5)
        r1 = ttk.Frame(s)
        r1.pack(fill="x", padx=6, pady=4)
        ttk.Label(r1, text="Repo name:").pack(side="left")
        self.pub_name_var = tk.StringVar(value=self.cfg.get("pub_name", ""))
        ttk.Entry(r1, textvariable=self.pub_name_var, width=36).pack(side="left", padx=6)
        self.pub_private_var = tk.BooleanVar(value=self.cfg.get("pub_private", False))
        ttk.Checkbutton(r1, text="Private", variable=self.pub_private_var).pack(side="left", padx=6)
        ttk.Button(r1, text="Create or reuse repository", command=self._pub_create_repo, style="Accent.TButton").pack(side="left", padx=6)
        self.pub_repo_label = ttk.Label(r1, text="", foreground="#060")
        self.pub_repo_label.pack(side="left", padx=8)
        r2 = ttk.Frame(s)
        r2.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(r2, text="Description:").pack(side="left")
        self.pub_desc_var = tk.StringVar(value=self.cfg.get("pub_desc", ""))
        ttk.Entry(r2, textvariable=self.pub_desc_var, width=72).pack(side="left", padx=6)

    def _build_build_options(self, parent):
        s = ttk.LabelFrame(parent, text="Step 2b: Optional build automation")
        s.pack(fill="x", padx=10, pady=5)
        row = ttk.Frame(s)
        row.pack(fill="x", padx=6, pady=(6, 2))
        self.pub_scaffold_var = tk.BooleanVar(value=self.cfg.get("pub_scaffold", True))
        ttk.Checkbutton(row, text="Create missing safe default files before push", variable=self.pub_scaffold_var).pack(side="left")
        self.build_enabled_var = tk.BooleanVar(value=self.cfg.get("build_enabled", False))
        ttk.Checkbutton(row, text="Add build automation on push", variable=self.build_enabled_var, command=self._build_toggle).pack(side="left", padx=16)

        r2 = ttk.Frame(s)
        r2.pack(fill="x", padx=6, pady=2)
        ttk.Label(r2, text="Build type:").pack(side="left")
        self.build_type_var = tk.StringVar(value=self.cfg.get("build_type", "Auto Detect"))
        self.build_type_combo = ttk.Combobox(r2, textvariable=self.build_type_var, values=BUILD_TYPES, width=22, state="readonly")
        self.build_type_combo.pack(side="left", padx=6)
        self.build_type_combo.bind("<<ComboboxSelected>>", lambda e: self._build_toggle())
        ttk.Button(r2, text="Detect now", command=self._detect_now).pack(side="left", padx=4)
        ttk.Label(r2, text="Output:").pack(side="left", padx=(16, 2))
        self.build_output_var = tk.StringVar(value=self.cfg.get("build_output_mode", "Upload artifacts only"))
        self.output_combo = ttk.Combobox(r2, textvariable=self.build_output_var, values=OUTPUT_MODES, width=22, state="readonly")
        self.output_combo.pack(side="left", padx=6)

        r3 = ttk.Frame(s)
        r3.pack(fill="x", padx=6, pady=2)
        ttk.Label(r3, text="Python main file:").pack(side="left")
        self.py_entry_var = tk.StringVar(value=self.cfg.get("py_entry", ""))
        self.py_entry_entry = ttk.Entry(r3, textvariable=self.py_entry_var, width=38)
        self.py_entry_entry.pack(side="left", padx=6)
        self.py_pick_btn = ttk.Button(r3, text="Select .py", command=self._pick_py_entry)
        self.py_pick_btn.pack(side="left")
        ttk.Label(r3, text="C# kind:").pack(side="left", padx=(16, 2))
        self.csharp_kind_var = tk.StringVar(value=self.cfg.get("csharp_kind", "Windows Desktop App"))
        self.csharp_combo = ttk.Combobox(r3, textvariable=self.csharp_kind_var, values=CSHARP_KINDS, width=20, state="readonly")
        self.csharp_combo.pack(side="left", padx=6)

        r4 = ttk.Frame(s)
        r4.pack(fill="x", padx=6, pady=(2, 6))
        ttk.Label(r4, text="Targets:").pack(side="left")
        self.target_windows_var = tk.BooleanVar(value=self.cfg.get("target_windows", True))
        self.target_linux_var = tk.BooleanVar(value=self.cfg.get("target_linux", False))
        self.target_macos_var = tk.BooleanVar(value=self.cfg.get("target_macos", False))
        self.target_fedora_var = tk.BooleanVar(value=self.cfg.get("target_fedora", False))
        self.target_debian_var = tk.BooleanVar(value=self.cfg.get("target_debian", False))
        self.target_checks = [
            ttk.Checkbutton(r4, text="Windows", variable=self.target_windows_var),
            ttk.Checkbutton(r4, text="Linux", variable=self.target_linux_var),
            ttk.Checkbutton(r4, text="macOS", variable=self.target_macos_var),
            ttk.Checkbutton(r4, text="Fedora", variable=self.target_fedora_var),
            ttk.Checkbutton(r4, text="Debian", variable=self.target_debian_var),
        ]
        for c in self.target_checks:
            c.pack(side="left", padx=6)
        self.build_note = ttk.Label(s, foreground="#666", wraplength=1120, justify="left", text="")
        self.build_note.pack(anchor="w", padx=6, pady=(0, 6))
        self._build_toggle()

    def _build_push_validate_open(self, parent):
        s = ttk.LabelFrame(parent, text="Step 3: Push, validate, and open")
        s.pack(fill="x", padx=10, pady=5)
        r1 = ttk.Frame(s)
        r1.pack(fill="x", padx=6, pady=4)
        ttk.Label(r1, text="Local folder:").pack(side="left")
        self.pub_source_var = tk.StringVar(value=self.cfg.get("pub_source", ""))
        ttk.Entry(r1, textvariable=self.pub_source_var, width=60).pack(side="left", padx=6)
        ttk.Button(r1, text="Browse", command=self._pub_pick_source).pack(side="left")
        r2 = ttk.Frame(s)
        r2.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(r2, text="Branch:").pack(side="left")
        self.pub_branch_var = tk.StringVar(value=self.cfg.get("pub_branch", "main"))
        ttk.Entry(r2, textvariable=self.pub_branch_var, width=20).pack(side="left", padx=6)
        ttk.Label(r2, text="Commit message:").pack(side="left")
        self.pub_commit_var = tk.StringVar(value=self.cfg.get("pub_commit", "Initial commit"))
        ttk.Entry(r2, textvariable=self.pub_commit_var, width=44).pack(side="left", padx=6)
        ttk.Button(r2, text="Push files", command=self._pub_push, style="Accent.TButton").pack(side="left", padx=6)
        ttk.Button(r2, text="Validate files", command=self._pub_validate).pack(side="left", padx=6)
        ttk.Button(r2, text="Open repo", command=self._pub_open_browser).pack(side="left", padx=6)
        self.pub_validate_label = ttk.Label(s, text="", foreground="#666")
        self.pub_validate_label.pack(anchor="w", padx=6, pady=(0, 6))

    # Logging and async -----------------------------------------------------
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
            msg = msg[:1500] + "\n... (truncated)"
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

    # GitHub and git --------------------------------------------------------
    def _api(self, method, path, data=None):
        if not self.gh_token:
            raise RuntimeError("Not connected. Enter a token and click Connect first.")
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
            raise RuntimeError(f"GitHub API {e.code}: {self._clean_api_error(e.code, getattr(e, 'reason', ''), raw)}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Network error contacting GitHub: {e.reason}")

    @staticmethod
    def _clean_api_error(code, reason, raw):
        raw = (raw or "").strip()
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("message"):
                return data["message"]
        except Exception:
            pass
        if raw.startswith("<") or "<html" in raw[:200].lower():
            return "GitHub returned a non-JSON error page. Try again."
        text = " ".join(raw.split())
        return text[:200] if text else (str(reason) or "Unknown error")

    def _redact(self, text):
        text = str(text)
        if self.gh_token:
            text = text.replace(self.gh_token, "***")
        return re.sub(r"//[^/@]+:[^/@]+@", "//***@", text)

    def _run_git(self, args, cwd=None):
        prefix = ["-c", "credential.helper=", "-c", "credential.interactive=false", "-c", "core.askpass="]
        cmd = [self.git_exe] + prefix + args
        self.log("$ " + " ".join(self._redact(a) for a in cmd))
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "Never"
        env["GIT_ASKPASS"] = ""
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, creationflags=NO_WINDOW, env=env)
        out = (proc.stdout or "") + (proc.stderr or "")
        for line in out.splitlines():
            if line.strip():
                self.log("  " + self._redact(line.rstrip()))
        return proc.returncode, out

    def _auth_url_for(self, full):
        return f"https://{self.gh_user}:{self.gh_token}@github.com/{full}.git"

    def _ensure_git(self):
        if not (Path(self.git_exe).exists() or shutil.which(self.git_exe)):
            raise RuntimeError("Git was not found. Put PortableGit next to the app or install Git on PATH.")

    # UI handlers ----------------------------------------------------------
    def _toggle_token(self):
        self.token_entry.config(show="" if self.show_var.get() else "•")

    def _connect(self):
        token = self.token_var.get().strip()
        if not token:
            self.alert("Connect", "Enter a Personal Access Token first.", "info")
            return
        self.gh_token = token
        def work():
            self.log("Connecting to GitHub...")
            _, user = self._api("GET", "/user")
            self.gh_user = user.get("login", "")
            self.log(f"Connected as {self.gh_user}", "OK")
            if self.gh_scopes is not None:
                self.log(f"Token scopes: {self.gh_scopes or 'fine-grained or not reported'}")
                scope_set = {s.strip() for s in (self.gh_scopes or "").split(",") if s.strip()}
                if scope_set and "repo" not in scope_set and "public_repo" not in scope_set:
                    self.log("Heads up: token has no 'repo' scope. Creating repos and pushing may fail.", "WARN")
                elif scope_set and "workflow" not in scope_set:
                    self.log("Note: token has no 'workflow' scope. Enable it before pushing build automation or tagging releases.", "WARN")
            self.root.after(0, self._refresh_status)
        self._async(work, "Connect")

    def _refresh_status(self):
        for lbl in self._status_labels:
            lbl.config(text=f"Connected: {self.gh_user}", foreground="#060")

    def _pub_create_repo(self):
        def work():
            if not self.gh_user:
                raise RuntimeError("Connect first.")
            name = self.pub_name_var.get().strip()
            if not name:
                raise RuntimeError("Enter a repository name.")
            full = f"{self.gh_user}/{name}"
            info = None
            try:
                s, info = self._api("GET", f"/repos/{full}")
            except RuntimeError as e:
                if "404" not in str(e):
                    raise
            if info:
                self.pub_repo_full = info.get("full_name", full)
                self.pub_repo_url = info.get("html_url", f"https://github.com/{full}")
                self.pub_default_branch = info.get("default_branch", "main")
                self.pub_repo_private = bool(info.get("private"))
                self.log(f"Repository already exists, reusing: {self.pub_repo_url}", "OK")
                self.root.after(0, lambda: self.pub_repo_label.config(text=f"-> {self.pub_repo_full}"))
                self.save_config()
                return
            payload = {
                "name": name,
                "description": self.pub_desc_var.get().strip(),
                "private": bool(self.pub_private_var.get()),
                "auto_init": False,
            }
            self.log(f"Creating repository {full}...")
            _, created = self._api("POST", "/user/repos", payload)
            self.pub_repo_full = created.get("full_name", full)
            self.pub_repo_url = created.get("html_url", f"https://github.com/{full}")
            self.pub_default_branch = created.get("default_branch") or self.pub_branch_var.get().strip() or "main"
            self.pub_repo_private = bool(created.get("private"))
            self.log(f"Repository created: {self.pub_repo_url}", "OK")
            self.root.after(0, lambda: self.pub_repo_label.config(text=f"-> {self.pub_repo_full}"))
            self.save_config()
        self._async(work, "Create repository")

    def _pub_pick_source(self):
        d = filedialog.askdirectory(title="Choose the local folder to publish")
        if d:
            self.pub_source_var.set(d)

    def _pick_py_entry(self):
        src = self._get_source_path()
        selected = filedialog.askopenfilename(
            title="Select main Python application file",
            initialdir=str(src) if src else str(Path.home()),
            filetypes=[("Python files", "*.py"), ("All files", "*.*")],
            parent=self.root,
        )
        if selected:
            try:
                rel = safe_rel(Path(selected).resolve(), src.resolve())
            except Exception:
                rel = selected
            self.py_entry_var.set(rel)

    def _detect_now(self):
        try:
            src = self._get_source_path()
            detected = detect_project_type(src)
            if detected:
                self.build_type_var.set(detected)
                self.log(f"Detected project type: {detected}", "OK")
            else:
                self.log("Could not detect project type. Choose one manually.", "WARN")
                self.alert("Detect build type", "Could not detect project type. Choose one manually.", "warn")
            self._build_toggle()
        except Exception as e:
            self.alert("Detect build type", str(e), "warn")

    def _build_toggle(self):
        enabled = self.build_enabled_var.get()
        state = "normal" if enabled else "disabled"
        for w in [self.build_type_combo, self.output_combo, self.py_entry_entry, self.py_pick_btn, self.csharp_combo, *getattr(self, "target_checks", [])]:
            try:
                w.configure(state=state if not isinstance(w, ttk.Combobox) else ("readonly" if enabled else "disabled"))
            except Exception:
                pass
        if not enabled:
            self.build_note.config(text="Build automation is off. Push will not create .github/workflows or trigger Actions.")
        else:
            self.build_note.config(text="Build automation is on. The app writes .github/workflows/build.yml only for this push. Token needs workflow scope.")

    def _get_source_path(self) -> Path:
        raw = self.pub_source_var.get().strip().strip('"').strip("'")
        if not raw:
            raise RuntimeError("Choose a local folder first.")
        src = Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
        if not src.exists() or not src.is_dir():
            raise RuntimeError("Choose a valid local folder.")
        if not any(src.iterdir()):
            raise RuntimeError("The selected folder is empty.")
        return src

    def _prepare_build_files(self, src: Path, interactive=True) -> tuple[str, list[str]]:
        build_type = self.build_type_var.get()
        if build_type == "None":
            return "None", []
        if build_type == "Auto Detect":
            detected = detect_project_type(src)
            if not detected:
                if interactive:
                    raise RuntimeError("Auto Detect could not identify the project type. Choose a build type manually.")
                detected = "Static Website"
            build_type = detected
            self.root.after(0, lambda: self.build_type_var.set(build_type))
            self.log(f"Build type: {build_type}", "OK")
        py_entry = self.py_entry_var.get().strip()
        if build_type == "Python":
            py_entry = choose_python_entry_interactive(self.root, src, py_entry) if interactive else (py_entry or "main.py")
            self.root.after(0, lambda: self.py_entry_var.set(py_entry))
        created = []
        if self.pub_scaffold_var.get():
            created = ensure_project_dependencies(src, build_type, py_entry, self.csharp_kind_var.get())
            for item in created:
                self.log(f"Created missing dependency/default file: {item}", "OK")
        targets = self._targets()
        app_name = self._product_name()
        self.log(f"Release/executable name: {app_name}", "OK")
        workflow = render_workflow(build_type, self.pub_branch_var.get().strip() or "main", self.build_output_var.get(), py_entry, self.csharp_kind_var.get(), targets, app_name)
        wf = src / ".github" / "workflows" / "build.yml"
        wf.parent.mkdir(parents=True, exist_ok=True)
        wf.write_text(workflow, encoding="utf-8")
        self.log("Wrote .github/workflows/build.yml", "OK")
        return build_type, created + [".github/workflows/build.yml"]

    def _product_name(self) -> str:
        """Name used for build executables and release assets. Prefers the repo name,
        falls back to the target repo's name, then to 'app'."""
        candidates = [
            self.pub_name_var.get().strip(),
            (self.pub_repo_full.split("/")[-1] if self.pub_repo_full else ""),
            (self.build_repo.split("/")[-1] if self.build_repo else ""),
        ]
        raw = next((c for c in candidates if c), "app")
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-")
        return cleaned or "app"

    def _targets(self) -> dict:
        return {
            "windows": bool(self.target_windows_var.get()),
            "linux": bool(self.target_linux_var.get()),
            "macos": bool(self.target_macos_var.get()),
            "fedora": bool(self.target_fedora_var.get()),
            "debian": bool(self.target_debian_var.get()),
        }

    def _pub_push(self):
        def work():
            if not self.pub_repo_full:
                raise RuntimeError("Create or reuse the repository first.")
            self._ensure_git()
            src = self._get_source_path()
            if self.build_enabled_var.get():
                scope_set = {s.strip() for s in (self.gh_scopes or "").split(",") if s.strip()}
                if scope_set and "workflow" not in scope_set:
                    raise RuntimeError("Build automation writes .github/workflows/build.yml. Add workflow scope to the token or turn build automation off.")
                self._prepare_build_files(src, interactive=True)
            else:
                self.log("Build automation is off. No workflow files were created.")
            self._push_folder(src, self.pub_repo_full, self.pub_branch_var.get().strip() or "main", self.pub_commit_var.get().strip() or "Initial commit")
            self.pub_default_branch = self.pub_branch_var.get().strip() or "main"
            self.pub_source_dir = str(src)
            self.save_config()
        self._async(work, "Push files")

    def _push_folder(self, src: Path, repo_full: str, branch: str, commit_msg: str):
        d = str(src)
        if not (src / ".git").exists():
            rc, out = self._run_git(["-C", d, "init"])
            if rc != 0:
                raise RuntimeError(f"git init failed:\n{self._git_tail(out)}")
        self._run_git(["-C", d, "config", "user.name", self.gh_user])
        self._run_git(["-C", d, "config", "user.email", f"{self.gh_user}@users.noreply.github.com"])
        rc, out = self._run_git(["-C", d, "checkout", "-B", branch])
        if rc != 0:
            raise RuntimeError(f"Could not create or switch to branch '{branch}':\n{self._git_tail(out)}")
        rc, _ = self._run_git(["-C", d, "remote", "get-url", "origin"])
        if rc == 0:
            self._run_git(["-C", d, "remote", "set-url", "origin", f"https://github.com/{repo_full}.git"])
        else:
            self._run_git(["-C", d, "remote", "add", "origin", f"https://github.com/{repo_full}.git"])
        self._run_git(["-C", d, "add", "-A"])
        rc, out = self._run_git(["-C", d, "commit", "-m", commit_msg])
        if rc != 0 and "nothing to commit" not in out.lower():
            raise RuntimeError(f"git commit failed:\n{self._git_tail(out)}")
        rc, out = self._run_git(["-C", d, "push", self._auth_url_for(repo_full), f"HEAD:{branch}", "--force"])
        if rc != 0:
            raise RuntimeError(f"git push failed:\n{self._git_tail(out)}\n\n"
                               "Common causes: the token lacks 'repo' scope, the token cannot access this repo, "
                               "or (when pushing a workflow) it lacks 'workflow' scope.")
        self.log(f"Pushed {src} -> {repo_full} ({branch})", "OK")

    def _git_tail(self, out: str, lines: int = 8) -> str:
        cleaned = [self._redact(ln.rstrip()) for ln in (out or "").splitlines() if ln.strip()]
        if not cleaned:
            return "(no git output captured)"
        return "\n".join(cleaned[-lines:])

    def _push_main(self):
        """Force-push the selected local folder (including any .github/workflows) to the
        target repo's main branch, so main always has the latest workflow. After this,
        updating the version tag with the Build button re-runs the build."""
        def work():
            if not self.gh_user:
                raise RuntimeError("Connect first.")
            self._ensure_git()
            repo_full = (self.build_repo or self.pub_repo_full or "").strip()
            if repo_full.count("/") != 1:
                raise RuntimeError("No target repo is set. Use Step 2 to create or reuse a repo, "
                                   "or run Build once so the app knows owner/repo.")
            src = self._get_source_path()
            scope_set = {s.strip() for s in (self.gh_scopes or "").split(",") if s.strip()}
            if scope_set and "repo" not in scope_set and "public_repo" not in scope_set:
                raise RuntimeError("Token is missing 'repo' scope, which is required to push. "
                                   "Regenerate a classic token with 'repo' (and 'workflow' for workflows).")
            has_workflow = (src / ".github" / "workflows").exists()
            if has_workflow and scope_set and "workflow" not in scope_set:
                raise RuntimeError("This folder contains .github/workflows, so pushing it requires the token "
                                   "'workflow' scope. Add it to the token, or remove the workflow before pushing.")
            self.log(f"Pushing '{src}' to {repo_full}@main ...")
            self._push_folder(src, repo_full, "main", self.pub_commit_var.get().strip() or "Update main")
            self.build_repo = repo_full
            self.save_config()
            self.log("main is up to date. To rebuild, bump APP_VERSION and update the tag with the Build button "
                     "(tag builds use the workflow from the tagged commit).", "OK")
            self.root.after(0, lambda: self.alert(
                "Push main",
                f"Pushed to {repo_full}@main.\n\nTo trigger a rebuild, click Build and push an updated version tag.",
                "info"))
        self._async(work, "Push main")

    def _pub_validate(self):
        def work():
            if not self.pub_repo_full:
                raise RuntimeError("Create and push the repository first.")
            src = self._get_source_path()
            branch = self.pub_default_branch or self.pub_branch_var.get().strip() or "main"
            self.log(f"Fetching file tree of {self.pub_repo_full}@{branch}...")
            _, tree = self._api("GET", f"/repos/{self.pub_repo_full}/git/trees/{branch}?recursive=1")
            remote = {t["path"] for t in tree.get("tree", []) if t.get("type") == "blob"}
            local = []
            if (src / ".git").exists():
                rc, out = self._run_git(["-C", str(src), "ls-files"])
                if rc == 0:
                    local = [line.strip() for line in out.splitlines() if line.strip()]
            if not local:
                local = [safe_rel(f, src) for f in src.rglob("*") if f.is_file() and ".git" not in f.parts]
            missing = sorted(p for p in local if p not in remote)
            present = len(local) - len(missing)
            self.log(f"Validation: {present}/{len(local)} local file(s) present on repo.", "OK" if not missing else "WARN")
            self.root.after(0, lambda: self.pub_validate_label.config(
                text=(f"{present}/{len(local)} present" if not missing else f"{present}/{len(local)} present, {len(missing)} missing"),
                foreground="#060" if not missing else "#a00"))
            if missing:
                preview = "\n".join(missing[:20])
                self.alert("Validation", f"Missing {len(missing)} file(s):\n\n{preview}", "warn")
            else:
                self.alert("Validation", f"All {len(local)} file(s) are present.", "info")
        self._async(work, "Validate files")

    def _pub_open_browser(self):
        if not self.pub_repo_url:
            self.alert("Open repo", "Create the repository first.", "info")
            return
        if self.pub_repo_private:
            self.alert("Open repo", "This repo is private. GitHub shows 404 unless the browser is signed in as its owner.", "warn")
        self.log(f"Opening {self.pub_repo_url}")
        webbrowser.open(self.pub_repo_url)

    # Build tag -------------------------------------------------------------
    def open_build_release(self):
        win = tk.Toplevel(self.root)
        win.title("Build")
        win.transient(self.root)
        win.resizable(False, False)
        win.grab_set()
        ttk.Label(win, wraplength=620, justify="left", text=(
            "Push or recreate a version tag to trigger GitHub Actions. If build automation is enabled here, "
            "the app writes the selected workflow before tagging. If it is not enabled and no workflow exists, no build will run."
        )).pack(anchor="w", padx=12, pady=(12, 6))
        frm = ttk.Frame(win)
        frm.pack(fill="x", padx=12, pady=4)
        ttk.Label(frm, text="Target repo owner/repo:", width=24).grid(row=0, column=0, sticky="w", pady=3)
        repo_var = tk.StringVar(value=self.build_repo or self.pub_repo_full or "")
        ttk.Entry(frm, textvariable=repo_var, width=42).grid(row=0, column=1, sticky="w", padx=4, pady=3)
        ttk.Label(frm, text="Branch to tag:", width=24).grid(row=1, column=0, sticky="w", pady=3)
        branch_var = tk.StringVar(value=self.pub_branch_var.get().strip() or "main")
        ttk.Entry(frm, textvariable=branch_var, width=24).grid(row=1, column=1, sticky="w", padx=4, pady=3)
        ttk.Label(frm, text="Tag:", width=24).grid(row=2, column=0, sticky="w", pady=3)
        tag_var = tk.StringVar(value=f"v{APP_VERSION}")
        ttk.Entry(frm, textvariable=tag_var, width=24).grid(row=2, column=1, sticky="w", padx=4, pady=3)
        enable_var = tk.BooleanVar(value=self.build_enabled_var.get())
        ttk.Checkbutton(win, text="Add or update build workflow before tagging", variable=enable_var).pack(anchor="w", padx=12, pady=(4, 2))
        recreate_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(win, text="Recreate tag if it already exists", variable=recreate_var).pack(anchor="w", padx=12, pady=(0, 6))
        foot = ttk.Frame(win)
        foot.pack(fill="x", padx=12, pady=(4, 12))
        def do_build():
            repo_full = repo_var.get().strip()
            branch = branch_var.get().strip() or "main"
            tag = tag_var.get().strip()
            if not self.gh_user:
                self.alert("Build", "Connect first.", "warn")
                return
            if repo_full.count("/") != 1:
                self.alert("Build", "Enter target repo as owner/repo.", "warn")
                return
            win.destroy()
            self._async(lambda: self._push_build_tag(repo_full, branch, tag, recreate_var.get(), enable_var.get()), "Build")
        ttk.Button(foot, text="Build", command=do_build).pack(side="left")
        ttk.Button(foot, text="Close", command=win.destroy).pack(side="right")

    def _push_build_tag(self, repo_full: str, branch: str, tag: str, recreate: bool, prepare_workflow: bool):
        if repo_full.count("/") != 1:
            raise RuntimeError("Target repo must be in owner/repo form.")
        if not tag:
            raise RuntimeError("Enter a tag, for example v1.0.0.")
        if not re.match(r"^v\d", tag):
            self.log(f"Tag '{tag}' does not start with 'v'. Generated release workflows trigger on tags matching 'v*', "
                     f"so this tag may not start a release build.", "WARN")
        scope_set = {s.strip() for s in (self.gh_scopes or "").split(",") if s.strip()}
        if scope_set and "repo" not in scope_set and "public_repo" not in scope_set:
            raise RuntimeError("Token is missing 'repo' scope, which is required to create tags. "
                               "Regenerate a classic token with 'repo' (and 'workflow' for build automation).")
        if prepare_workflow and scope_set and "workflow" not in scope_set:
            raise RuntimeError("Adding a workflow requires the token 'workflow' scope. "
                               "Add it to the token, or uncheck the workflow option.")
        src = None
        if prepare_workflow:
            src = self._get_source_path()
            self._prepare_build_files(src, interactive=True)
            self._push_folder(src, repo_full, branch, f"Add build workflow for {tag}")
        else:
            self.log("Build workflow option not selected. Tag will be pushed only. If no workflow exists, no Actions build will run.", "WARN")
        owner, repo = repo_full.split("/", 1)
        if not prepare_workflow:
            try:
                _, wf = self._api("GET", f"/repos/{owner}/{repo}/contents/.github/workflows?ref={branch}")
                names = [w.get("name") for w in wf] if isinstance(wf, list) else []
                if names:
                    self.log(f"Found workflow file(s) on {branch}: {', '.join(names)}", "OK")
                else:
                    self.log("No workflow files found on the branch. Tagging will not start any build.", "WARN")
            except RuntimeError as e:
                if "404" in str(e):
                    self.log("No .github/workflows folder on the branch. Tagging will not start any build.", "WARN")
        _, ref = self._api("GET", f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
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
                raise RuntimeError(f"Tag {tag} already exists. Check recreate tag, or use a new tag.")
            self._api("DELETE", f"/repos/{owner}/{repo}/git/refs/tags/{tag}")
            self.log(f"Deleted existing tag {tag}", "WARN")
            time.sleep(1)
        self._api("POST", f"/repos/{owner}/{repo}/git/refs", {"ref": f"refs/tags/{tag}", "sha": sha})
        self.log(f"Pushed tag {tag} -> {repo_full}@{sha[:7]}", "OK")
        self.build_repo = repo_full
        self.save_config()
        actions_url = f"https://github.com/{owner}/{repo}/actions"
        self.log(f"Actions page: {actions_url}")
        self.root.after(0, lambda: webbrowser.open(actions_url) if messagebox.askyesno("Build", "Open the Actions page in your browser?") else None)

    # Vault and misc --------------------------------------------------------
    def _vault_current_pat(self) -> str:
        return self.token_var.get().strip() or self.gh_token.strip()

    def open_vault(self):
        win = tk.Toplevel(self.root)
        win.title("PAT Vault")
        win.transient(self.root)
        win.resizable(False, False)
        win.grab_set()
        ttk.Label(win, wraplength=560, justify="left", text=(
            "Store multiple users' PATs, each encrypted at rest under its own master "
            "passphrase. Pick a user, enter that user's passphrase, and Unlock to fill "
            "Step 1 and connect automatically."
        )).pack(anchor="w", padx=12, pady=(12, 6))

        vault = vault_load()

        frm = ttk.Frame(win)
        frm.pack(fill="x", padx=12, pady=6)
        ttk.Label(frm, text="User / label:", width=18).grid(row=0, column=0, sticky="w", pady=3)
        labels = sorted(vault["entries"].keys())
        default_label = self.gh_user or (labels[0] if labels else "")
        user_var = tk.StringVar(value=default_label)
        user_combo = ttk.Combobox(frm, textvariable=user_var, values=labels, width=32)
        user_combo.grid(row=0, column=1, sticky="w", padx=4, pady=3)

        ttk.Label(frm, text="Master passphrase:", width=18).grid(row=1, column=0, sticky="w", pady=3)
        pw = ttk.Entry(frm, show="•", width=34)
        pw.grid(row=1, column=1, sticky="w", padx=4, pady=3)

        status = ttk.Label(win, text=(f"{len(labels)} user(s) stored." if labels else "Vault is empty."),
                           style="Muted.TLabel")
        status.pack(anchor="w", padx=12, pady=(0, 4))

        def refresh_labels(select=None):
            v = vault_load()
            vault["entries"] = v["entries"]
            new_labels = sorted(vault["entries"].keys())
            user_combo.configure(values=new_labels)
            status.config(text=(f"{len(new_labels)} user(s) stored." if new_labels else "Vault is empty."))
            if select is not None:
                user_var.set(select)

        def unlock():
            label = user_var.get().strip()
            if not label:
                self.alert("PAT Vault", "Pick or type the user/label to unlock.", "warn")
                return
            entry = vault["entries"].get(label)
            if not entry:
                self.alert("PAT Vault", f"No stored PAT for '{label}'.", "warn")
                return
            try:
                pat = vault_decrypt(pw.get(), entry)
            except Exception as e:
                self.alert("PAT Vault", str(e), "error")
                return
            self.token_var.set(pat)
            self.log(f"PAT for '{label}' filled from vault. Connecting...", "OK")
            win.destroy()
            self._connect()

        def save():
            pat = self._vault_current_pat()
            if not pat:
                self.alert("PAT Vault", "Enter or fill a PAT in Step 1 first.", "warn")
                return
            label = user_var.get().strip() or self.gh_user.strip()
            if not label:
                self.alert("PAT Vault", "Enter a user/label to save this PAT under.", "warn")
                return
            if len(pw.get()) < 4 or pw.get() == VAULT_RESET_PHRASE:
                self.alert("PAT Vault", "Use a master passphrase of at least 4 characters. Do not use the reset phrase.", "warn")
                return
            existed = label in vault["entries"]
            if existed and not messagebox.askyesno("PAT Vault", f"Replace the stored PAT for '{label}'?"):
                return
            vault["entries"][label] = vault_encrypt(pw.get(), pat)
            vault_save(vault)
            self.log(f"PAT for '{label}' saved to encrypted vault.", "OK")
            refresh_labels(select=label)
            pw.delete(0, "end")

        def delete_entry():
            label = user_var.get().strip()
            entry = vault["entries"].get(label)
            if not entry:
                self.alert("PAT Vault", f"No stored PAT for '{label}'.", "warn")
                return
            try:
                vault_decrypt(pw.get(), entry)
            except Exception:
                self.alert("PAT Vault", "Enter that user's correct passphrase to delete their entry.", "error")
                return
            if not messagebox.askyesno("PAT Vault", f"Delete stored PAT for '{label}'?"):
                return
            vault["entries"].pop(label, None)
            vault_save(vault)
            self.log(f"Deleted vault entry for '{label}'.", "WARN")
            refresh_labels(select="")
            pw.delete(0, "end")

        def reset():
            if pw.get() != VAULT_RESET_PHRASE:
                self.alert("PAT Vault", "Enter the reset passphrase to erase ALL stored users.", "error")
                return
            if not messagebox.askyesno("PAT Vault", "This erases EVERY stored user's PAT. Continue?"):
                return
            if VAULT_FILE.exists():
                VAULT_FILE.unlink()
            vault["entries"] = {}
            self.log("Vault reset. All stored PATs erased.", "WARN")
            refresh_labels(select="")
            pw.delete(0, "end")

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=12, pady=(4, 12))
        ttk.Button(btns, text="Unlock and fill", command=unlock, style="Accent.TButton").pack(side="left")
        ttk.Button(btns, text="Save PAT", command=save).pack(side="left", padx=6)
        ttk.Button(btns, text="Delete entry", command=delete_entry).pack(side="left", padx=6)
        ttk.Button(btns, text="Reset all", command=reset).pack(side="left", padx=6)
        ttk.Button(btns, text="Close", command=win.destroy).pack(side="right")

    def _save_activity_window(self):
        text = self.console.get("1.0", "end").rstrip("\n")
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except Exception:
            pass
        path = filedialog.asksaveasfilename(
            title="Save activity window",
            defaultextension=".txt",
            initialdir=str(CONFIG_DIR),
            initialfile=f"github_pr_agent_activity_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            filetypes=[("Text log", "*.txt"), ("All files", "*.*")],
        )
        if path:
            Path(path).write_text(text, encoding="utf-8")
            self.log(f"Activity saved -> {path}", "OK")

    # Update / upgrade -------------------------------------------------------
    @staticmethod
    def _parse_version(text: str) -> str | None:
        m = re.search(r"APP_VERSION\s*=\s*[\"']([0-9]+(?:\.[0-9]+)*)[\"']", text or "")
        return m.group(1) if m else None

    @staticmethod
    def _version_tuple(version: str) -> tuple:
        try:
            return tuple(int(x) for x in re.findall(r"\d+", version or "0"))
        except Exception:
            return (0,)

    def _check_for_updates(self):
        """Source mode updates from the latest Python file on GitHub.
        EXE mode checks the latest GitHub Release and opens the release asset.
        """
        def work():
            self.log(f"Checking for updates from https://github.com/{UPDATE_REPO}...")
            if getattr(sys, "frozen", False):
                self._check_exe_release_update()
            else:
                self._check_python_source_update()
        self._async(work, "Check for updates")

    def _check_python_source_update(self):
        req = urllib.request.Request(UPDATE_RAW_URL, method="GET")
        req.add_header("User-Agent", "GitHub-PR-Agent")
        req.add_header("Accept", "text/plain")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                remote_src = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"Update check failed HTTP {e.code}. Expected latest Python at {UPDATE_RAW_URL}.")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Network error checking for updates: {e.reason}")
        remote_ver = self._parse_version(remote_src)
        if not remote_ver:
            raise RuntimeError("Could not read APP_VERSION from the latest Python file.")
        self.log(f"Installed Python v{APP_VERSION}; GitHub Python v{remote_ver}.")
        if self._version_tuple(remote_ver) <= self._version_tuple(APP_VERSION):
            self.log("Already up to date.", "OK")
            self.alert("Check for updates", f"You are on the latest Python version v{APP_VERSION}.", "info")
            return
        self.root.after(0, lambda: self._prompt_apply_source_update(remote_src, remote_ver))

    def _check_exe_release_update(self):
        """Check latest GitHub Release for an EXE asset.
        If the user approves, download the EXE, close this process, replace the
        current executable, and relaunch the new version.
        """
        req = urllib.request.Request(UPDATE_RELEASES_API, method="GET")
        req.add_header("User-Agent", "GitHub-PR-Agent")
        req.add_header("Accept", "application/vnd.github+json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                release = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                self.log("No GitHub Release found yet. Opening releases page for manual check.", "WARN")
                self.root.after(0, lambda: webbrowser.open(UPDATE_RELEASES_URL))
                return
            raise RuntimeError(f"Release update check failed HTTP {e.code}.")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Network error checking releases: {e.reason}")

        tag = str(release.get("tag_name", "")).lstrip("vV")
        html_url = release.get("html_url") or UPDATE_RELEASES_URL
        assets = release.get("assets") or []
        exe_assets = [a for a in assets if str(a.get("name", "")).lower().endswith(".exe")]
        self.log(f"Installed EXE v{APP_VERSION}; latest release v{tag or 'unknown'}.")

        if tag and self._version_tuple(tag) <= self._version_tuple(APP_VERSION):
            self.log("Already up to date.", "OK")
            self.alert("Check for updates", f"You are on the latest EXE version v{APP_VERSION}.", "info")
            return

        if not exe_assets:
            def prompt_no_asset():
                if messagebox.askyesno(
                    "Update available",
                    f"A newer release is available: v{tag or 'unknown'}.\n\nNo .exe asset was detected. Open the release page?",
                ):
                    webbrowser.open(html_url)
            self.root.after(0, prompt_no_asset)
            return

        asset = exe_assets[0]
        asset_name = asset.get("name", "GitHub_PR_Agent.exe")
        asset_url = asset.get("browser_download_url")
        if not asset_url:
            raise RuntimeError("The latest release has an EXE asset, but no download URL was provided by GitHub.")

        def prompt_update():
            if messagebox.askyesno(
                "Update available",
                f"A newer EXE release is available.\n\nInstalled: v{APP_VERSION}\nLatest: v{tag or 'unknown'}\nAsset: {asset_name}\n\nDownload it, close this app, replace the current EXE, and reopen the new version?",
            ):
                self._async(lambda: self._download_and_apply_exe_update(asset_url, asset_name, tag or "unknown"), "Apply EXE update")
        self.root.after(0, prompt_update)

    def _download_and_apply_exe_update(self, asset_url: str, asset_name: str, remote_ver: str):
        if not getattr(sys, "frozen", False):
            raise RuntimeError("EXE update can only run from the packaged executable.")

        current_exe = Path(sys.executable).resolve()
        update_dir = CONFIG_DIR / "updates"
        update_dir.mkdir(parents=True, exist_ok=True)
        downloaded_exe = update_dir / asset_name

        self.log(f"Downloading EXE update: {asset_name}")
        req = urllib.request.Request(asset_url, method="GET")
        req.add_header("User-Agent", "GitHub-PR-Agent")
        with urllib.request.urlopen(req, timeout=120) as resp:
            downloaded_exe.write_bytes(resp.read())

        if not downloaded_exe.exists() or downloaded_exe.stat().st_size < 100_000:
            raise RuntimeError("Downloaded update does not look like a valid EXE. Update was not applied.")

        backup_exe = current_exe.with_suffix(f".exe.bak-v{APP_VERSION}")
        updater = update_dir / "apply_github_pr_agent_update.cmd"
        pid = os.getpid()

        script = f'''@echo off
setlocal
set "SRC={downloaded_exe}"
set "DST={current_exe}"
set "BAK={backup_exe}"
set "PID={pid}"
echo Updating GitHub PR Agent...
:waitloop
tasklist /FI "PID eq %PID%" 2>NUL | find "%PID%" >NUL
if not errorlevel 1 (
  timeout /t 1 /nobreak >NUL
  goto waitloop
)
if exist "%DST%" copy /Y "%DST%" "%BAK%" >NUL
copy /Y "%SRC%" "%DST%" >NUL
if errorlevel 1 (
  echo Update failed. Backup remains at "%BAK%".
  pause
  exit /b 1
)
start "" "%DST%"
del "%SRC%" >NUL 2>NUL
del "%~f0" >NUL 2>NUL
'''
        updater.write_text(script, encoding="utf-8")
        self.log(f"Downloaded v{remote_ver}. Applying update and restarting...", "OK")
        self.save_config()
        subprocess.Popen(["cmd.exe", "/c", str(updater)], close_fds=True, creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
        self.root.after(300, self.root.destroy)

    def _prompt_apply_source_update(self, remote_src: str, remote_ver: str):
        if not messagebox.askyesno(
            "Update available",
            f"A newer Python version is available.\n\nInstalled: v{APP_VERSION}\nLatest: v{remote_ver}\n\nReplace this local Python file and restart?",
        ):
            return
        self._async(lambda: self._apply_source_update(remote_src, remote_ver), "Apply update")

    def _resolve_self_path(self) -> Path:
        candidates = []
        for getter in (
            lambda: Path(__file__).resolve(),
            lambda: Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None,
            lambda: app_base_dir() / UPDATE_SCRIPT_NAME,
        ):
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

    def _apply_source_update(self, remote_src: str, remote_ver: str):
        if getattr(sys, "frozen", False):
            raise RuntimeError("EXE builds are updated from GitHub Releases, not by replacing the Python source file.")
        target = self._resolve_self_path()
        if not target.exists():
            raise RuntimeError(f"Could not locate the running Python file: {target}")
        backup = target.with_suffix(f".py.bak-v{APP_VERSION}")
        try:
            shutil.copy2(target, backup)
            self.log(f"Backed up current file -> {backup.name}", "OK")
        except Exception as e:
            backup = None
            self.log(f"Backup failed, continuing: {e}", "WARN")
        target.write_text(remote_src, encoding="utf-8")
        self.log(f"Updated {target.name} to v{remote_ver}.", "OK")
        self.root.after(0, lambda: self._restart_into_new_version(target, remote_ver, backup.name if backup else "none"))

    def _restart_into_new_version(self, target: Path, remote_ver: str, backup_name: str):
        self.save_config()
        py = sys.executable or shutil.which("pythonw") or shutil.which("python") or "python"
        try:
            flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            subprocess.Popen([py, str(target)], cwd=str(target.parent), creationflags=flags, close_fds=True)
        except Exception as e:
            self.alert(
                "Update installed",
                f"Updated to v{remote_ver} with backup {backup_name}, but auto-restart failed: {e}\n\nClose and reopen the app manually.",
                "warn",
            )
            return
        self.root.after(400, self.root.destroy)

    def _on_close(self):
        self.save_config()
        self.root.destroy()


def main():
    root = tk.Tk()
    GitHubPRAgent(root)
    root.mainloop()


if __name__ == "__main__":
    main()
