@echo off
REM Launch the GitHub PR Agent with a visible console (for debugging).
cd /d "%~dp0"
python "%~dp0GitHub_PR_Agent.py"
echo.
echo (agent exited) & pause
