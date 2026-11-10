$ErrorActionPreference = "Stop"
$TaskName = "Frontier Brand Agent Dashboard"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
Write-Host "Removed '$TaskName'." -ForegroundColor Green

