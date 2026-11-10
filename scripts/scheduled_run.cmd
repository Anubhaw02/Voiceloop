@echo off
setlocal
cd /d "%~dp0.."
if not exist "logs" mkdir "logs"
".venv\Scripts\python.exe" -m brand_agent run >> "logs\scheduler.log" 2>&1

