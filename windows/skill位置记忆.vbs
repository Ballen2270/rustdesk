' skill位置记忆: 把当前各窗口的位置保存为命名布局(mhxy-layouts\<名称>.json)
' 与「记录当前布局」互不影响(那个仍写 mhxy-layout.json,供一键五开/整理窗口用)
Set sh = CreateObject("WScript.Shell")
ps1 = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\")) & "mhxy-launcher.ps1"
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps1 & """ -Mode save -Gui", 0, False
