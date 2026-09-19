param(
    [switch]$AutoStart
)

# ============================================================
#  Creates desktop shortcut(s) for notice-hub.
#  Deliberately ASCII-only: the Chinese shortcut name is built
#  from char codes so it works under any PowerShell version,
#  code page, or BOM situation.
#
#  Run with:
#    powershell -ExecutionPolicy Bypass -File scripts\create-shortcut.ps1
#  Add -AutoStart to also put a shortcut in the Startup folder.
# ============================================================

$ErrorActionPreference = 'Stop'

# '通知中枢.lnk'
$shortcutNm = ([char]0x901A) + ([char]0x77E5) + ([char]0x4E2D) + ([char]0x67A2) + '.lnk'

$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Split-Path -Parent $scriptDir
# 桌面快捷方式指向「App 窗口」启动器（原生窗口，无浏览器地址栏）
$vbsTarget  = Join-Path $scriptDir 'start-app-silent.vbs'
$iconPath   = Join-Path $projectDir 'assets\notice-hub.ico'

if (-not (Test-Path $vbsTarget)) { throw "Cannot find launcher: $vbsTarget" }
if (-not (Test-Path $iconPath))  { throw "Cannot find icon: $iconPath  (run: py -3.12 tools\make_icons.py)" }

$wscript = Join-Path $env:SystemRoot 'System32\wscript.exe'
$shell   = New-Object -ComObject WScript.Shell

function New-HubShortcut {
    param([string]$Directory)
    $path = Join-Path $Directory $shortcutNm
    $lnk  = $shell.CreateShortcut($path)
    $lnk.TargetPath       = $wscript
    $lnk.Arguments        = '"' + $vbsTarget + '"'
    $lnk.WorkingDirectory = $projectDir
    $lnk.IconLocation     = $iconPath
    $lnk.Description      = 'notice-hub'
    $lnk.Save()
    return $path
}

$desktop = [Environment]::GetFolderPath('Desktop')
$made = New-HubShortcut -Directory $desktop
Write-Host "[OK] Desktop shortcut created:"
Write-Host "     $made"

if ($AutoStart) {
    $startup = [Environment]::GetFolderPath('Startup')
    $made2 = New-HubShortcut -Directory $startup
    Write-Host "[OK] Startup shortcut created (server will boot on login):"
    Write-Host "     $made2"
    Write-Host "     Delete that file to disable auto-start."
}

Write-Host ""
Write-Host "Double-click it to launch. The dashboard opens automatically."
Write-Host "To stop the server: scripts\stop.bat"
