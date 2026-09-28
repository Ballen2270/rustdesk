' 设置客户端路径(隐藏窗口启动)
Set sh = CreateObject("WScript.Shell")
ps1 = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\")) & "mhxy-launcher.ps1"
If WScript.Arguments.Count > 0 Then
    sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps1 & """ -Mode setpath -Gui -ExePath """ & WScript.Arguments(0) & """", 0, False
Else
    sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps1 & """ -Mode setpath -Gui", 0, False
End If
