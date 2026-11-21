Option Explicit
Dim fso, sh, here, target
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)
target = fso.BuildPath(here, "stop.ps1")
If Not fso.FileExists(target) Then
  MsgBox "Cannot find stop.ps1 next to this file.", 16, "Storybook"
  WScript.Quit 1
End If
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & target & """", 0, False
