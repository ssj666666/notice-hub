' ============================================================
'  notice-hub desktop app - silent launcher
'  Runs start-app.bat with no console window; the app window
'  itself is what you see.
' ============================================================
Option Explicit

Dim fso, shell, scriptDir, projectDir
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
projectDir = fso.GetParentFolderName(scriptDir)

shell.CurrentDirectory = projectDir
shell.Run """" & scriptDir & "\start-app.bat""", 0, False
