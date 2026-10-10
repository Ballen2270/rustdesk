' 窗口诊断 v3(显示控制台:脚本死掉时能直接看到报错)
Set sh = CreateObject("WScript.Shell")
ps1 = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\")) & "mhxy-diag3.ps1"
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -NoExit -File """ & ps1 & """ -Visible", 1, False
