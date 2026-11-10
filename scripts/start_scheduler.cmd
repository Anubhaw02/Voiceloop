@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Environment is not set up. Run scripts\setup.cmd first.
  exit /b 1
)
".venv\Scripts\python.exe" -m brand_agent scheduler

