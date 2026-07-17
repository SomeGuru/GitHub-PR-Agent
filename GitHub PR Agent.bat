@echo off
REM Launch the GitHub PR Agent GUI with no console window.
cd /d "%~dp0"
start "" pythonw "%~dp0GitHub_PR_Agent.py"
