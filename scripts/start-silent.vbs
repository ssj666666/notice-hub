' ============================================================
'  notice-hub silent launcher
'  Same as start.bat but with no console window.
'  To stop the server: run scripts\stop.bat
' ============================================================
Option Explicit

Dim fso, shell, scriptDir, projectDir
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
projectDir = fso.GetParentFolderName(scriptDir)

shell.CurrentDirectory = projectDir
shell.Run """" & scriptDir & "\start.bat""", 0, False
