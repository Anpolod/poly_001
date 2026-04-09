## Creates a Windows Scheduled Task that starts the collector at user logon
## and restarts it if it crashes. Run once as Administrator:
##   powershell -ExecutionPolicy Bypass -File setup_autostart.ps1

$TaskName = "PolymarketCollector"
$Python = "C:\Users\siriu\AppData\Local\Programs\Python\Python312\python.exe"
$WorkDir = "D:\dev\projects\polymarket-sports"
$Script = "main.py"
$LogFile = "$WorkDir\logs\stdout.log"

# Remove existing task if present
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument $Script `
    -WorkingDirectory $WorkDir

$Trigger = New-ScheduledTaskTrigger -AtLogOn

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Polymarket sports data collector (Phase 1)" `
    -RunLevel Limited

Write-Host "Task '$TaskName' created. It will start at next logon and restart on crash (up to 3x)."
Write-Host "Manual control:"
Write-Host "  Start:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "  Stop:   Stop-ScheduledTask -TaskName $TaskName"
Write-Host "  Status: Get-ScheduledTaskInfo -TaskName $TaskName"
Write-Host "  Remove: Unregister-ScheduledTask -TaskName $TaskName"
