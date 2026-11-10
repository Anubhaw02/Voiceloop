$ErrorActionPreference = "Stop"
$TaskName = "Frontier Brand Agent Research"
& schtasks.exe /Delete /TN $TaskName /F
if ($LASTEXITCODE -ne 0) {
    throw "Windows Task Scheduler removal failed with exit code $LASTEXITCODE."
}
Write-Host "Removed '$TaskName'." -ForegroundColor Green

