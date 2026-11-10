$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

Set-Location $ProjectRoot
if (-not (Test-Path -LiteralPath $VenvPython)) {
    py -m venv .venv
}

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -e ".[dev]"
& $VenvPython -m brand_agent init

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "Dashboard: scripts\start_dashboard.cmd"
Write-Host "One research cycle: scripts\run_once.cmd"

