$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Runner = Join-Path $ProjectRoot "scripts\scheduled_run.cmd"
$TaskName = "Frontier Brand Agent Research"

if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot ".venv\Scripts\python.exe"))) {
    throw "Run scripts\setup.cmd before registering the task."
}

$ActionArguments = "/d /c `"`"$Runner`"`""
$Action = New-ScheduledTaskAction `
    -Execute $env:ComSpec `
    -Argument $ActionArguments `
    -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger `
    -Once `
    -At ((Get-Date).AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Hours 12)
$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited
$TaskSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $TaskSettings `
    -Force | Out-Null
Write-Host "Registered '$TaskName' to run every 12 hours." -ForegroundColor Green

