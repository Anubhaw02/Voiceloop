$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$TaskName = "Frontier Brand Agent Dashboard"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Run scripts\setup.cmd before registering the dashboard task."
}

$Action = New-ScheduledTaskAction `
    -Execute $VenvPython `
    -Argument "-m brand_agent serve" `
    -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited
$TaskSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $TaskSettings `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Write-Host "Registered and started '$TaskName' on http://127.0.0.1:8765." -ForegroundColor Green

