' 查看窗口(隐藏窗口启动)
Set sh = CreateObject("WScript.Shell")
ps1 = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\")) & "mhxy-launcher.ps1"
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps1 & """ -Mode list -Gui", 0, False
