' skill位置摆放: 选一个已保存的命名布局,把所有窗口摆过去
' (复用"整理窗口"的就近匹配+回读验证;移动窗口需管理员,会弹 UAC)
Set sh = CreateObject("WScript.Shell")
ps1 = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\")) & "mhxy-launcher.ps1"
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps1 & """ -Mode use -Gui", 0, False
