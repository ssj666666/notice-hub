@echo off
rem Stop a running notice-hub server
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*run.py*' -and $_.CommandLine -like '*notice-hub*' };" ^
  "if ($procs) { $procs | ForEach-Object { Write-Host ('stopping PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force } } else { Write-Host 'notice-hub is not running.' }"
echo.
timeout /t 3 >nul
endlocal
