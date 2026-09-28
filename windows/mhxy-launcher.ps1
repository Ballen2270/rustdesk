# ============================================================================
#  梦幻西游手游 PC 版 五开助手 v2.1(修复版)
#  ---------------------------------------------------------------------------
#  相对 v1 的修复(依据目标机器诊断日志 mhxy-debug.log 确认的根因):
#   1. 槽位绑定改为「进程血缘」:启动器 PID → 子进程(MyGame)PID → 该进程的主窗口。
#      不再按「窗口出现顺序」分配 —— 旧逻辑会被同一进程的登录弹窗(MPAY_LOGIN)、
#      阴影窗(DuiShadowWnd)骗走槽位,导致实例2/3的真窗口被忽略、停在居中默认位置。
#   2. 每个进程只认一个主窗口:标题含游戏关键字(如「梦幻西游」)优先,
#      否则取最大的非弹窗窗口;弹窗/阴影窗(MPAY_*、DuiShadowWnd 等)完全不参与分配,
#      也不再被永久拉黑(槽位空出后按血缘允许重新绑定)。
#   3. 主窗口句柄变化(重建)时重新摆放;每次摆放后回读验证并重试。
#   4. 详细日志 mhxy-launcher.log 保留(追加写入),便于后续调试。
#  v2.1 修复(依据本机 mhxy-launcher.log 确认的问题):
#   1. 记录布局保存「还原矩形」:最小化窗口记录其还原位置(旧版存成 (-32000,-32000) 160x28
#      的垃圾布局,导致后续整理/启动全部按无效尺寸摆放、游戏窗口拒绝移动)。
#   2. 整理窗口/增量启动按「当前位置就近匹配」槽位(旧版按 PID 顺序硬对,
#      窗口重开或手动挪动后会被洗牌)。
#   3. 布局校验:保存的矩形异常(过小/越界)自动回退网格摆放并警告,旧垃圾配置不再静默破坏流程。
#   4. SetWindowPos 失败记录错误码(区分"调用被拒"与"游戏抢回");验证回读兼容最小化窗口。
#  v2.2 修复(依据桌面运行日志确认的问题):
#   1. 管理员权限自提升:游戏 exe(MyLauncher/MyGame)带 requireAdministrator 清单,
#      游戏窗口以高完整性运行;普通权限脚本 SetWindowPos 会被 UIPI 拒绝(返回失败),
#      导致窗口全部停在默认位置。现在 launch/arrange 模式自动以管理员身份重启自身
#      (启动时弹一次 UAC,之后启动游戏不再逐个弹窗),即可正常移动窗口。
#   2. 补齐 MHWin32.GetLastError 声明(此前调用未声明的方法导致每次摆放都抛异常)。
#   3. 汇总按「实际位置与目标比对」判定就位(旧版只要窗口存在就报"正常")。
#  v2.3 修复(依据 pc1 2026-09-27 日志与多虚拟屏实测):
#   1. 位置记忆只有一个时,skill位置摆放报「找不到属性Count」:Get-SavedLayoutNames 返回单元素/空
#      集合会被 PowerShell 解包成标量,$names.Count 在 StrictMode 2.0 下抛错。调用处一律 @() 包裹
#      (Get-MatchedWindows 的调用处同样加固)。
#   2. 开虚拟屏后「整理窗口」不能回归单屏:VirtualScreen 变成多屏总包围盒(如 5760x1080),
#      触发 scaleOnDifferentScreen 把 1920 布局横向×3 拉伸。现改为「参照屏」逻辑:保存时的
#      屏幕矩形仍存在就按原坐标摆放;参照屏消失才缩放。
#  用法: 同 v1(一键五开/记录布局/整理窗口/查看窗口/设置客户端路径/自检)
# ============================================================================
param(
    [ValidateSet('launch', 'record', 'arrange', 'save', 'use', 'list', 'setpath', 'selftest')]
    [string]$Mode = 'launch',
    [int]$Count = 0,
    [switch]$Force,
    [string]$ExePath = '',
    [string]$LayoutName = '',   # save/use: 命名布局名称(mhxy-layouts\<名称>.json)
    [switch]$Elevated,    # 内部:自提升后的标记(不手动使用)
    [switch]$NoElevate,   # 调试:跳过自提升
    [switch]$TestElevate, # 调试:只打印自提升命令不执行
    [switch]$Gui          # 图形界面模式(由 .vbs 入口传入:隐藏窗口 + 对话框交互)
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
if ($Mode -eq 'setpath') {
    [Console]::OutputEncoding = [System.Text.Encoding]::Default
} else {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
}

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $ScriptDir 'mhxy-layout.json'
$LayoutsDir = Join-Path $ScriptDir 'mhxy-layouts'   # save/use: 命名布局目录
$LogPath    = Join-Path $ScriptDir 'mhxy-launcher.log'

# ---------------------------------------------------------------------------
#  管理员权限自提升:游戏 exe 带 requireAdministrator,窗口为高完整性,
#  普通权限无法 SetWindowPos(UIPI 拒绝)。launch/arrange/use 需要移动窗口 → 自提升。
# ---------------------------------------------------------------------------
$script:IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $script:IsAdmin -and -not $Elevated -and -not $NoElevate -and ($Mode -eq 'launch' -or $Mode -eq 'arrange' -or $Mode -eq 'use')) {
    $relaunchArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', ('"' + $MyInvocation.MyCommand.Path + '"'), '-Mode', $Mode, '-Elevated', '-Gui')
    if ($Count -gt 0) { $relaunchArgs += '-Count'; $relaunchArgs += $Count }
    if ($Force) { $relaunchArgs += '-Force' }
    if ($ExePath) { $relaunchArgs += '-ExePath'; $relaunchArgs += ('"' + $ExePath + '"') }
    if ($LayoutName) { $relaunchArgs += '-LayoutName'; $relaunchArgs += ('"' + $LayoutName + '"') }
    if ($TestElevate) {
        if ($Gui) { Show-Msg ('将以管理员身份重启: powershell.exe ' + ($relaunchArgs -join ' ')) '五开助手' }
        else { Write-Host ('[TestElevate] 将以管理员身份重启: powershell.exe ' + ($relaunchArgs -join ' ')) }
        exit 0
    }
    if (-not $Gui) { Write-Host '游戏窗口需要管理员权限才能移动,正在请求提升权限(请在 UAC 弹窗点击"是")...' -ForegroundColor Yellow }
    try {
        Start-Process -FilePath 'powershell.exe' -ArgumentList $relaunchArgs -Verb RunAs -WorkingDirectory $ScriptDir | Out-Null
        exit 0
    } catch {
        if ($Gui) { Show-Msg '未获得管理员权限(可能取消了 UAC 确认)。移动游戏窗口需要管理员权限,已退出。' '五开助手' 'Error' }
        else { Write-Host '未获得管理员权限(可能取消了 UAC 确认),移动游戏窗口需要管理员权限,已退出。' -ForegroundColor Red }
        exit 1
    }
}

$script:SW = $null
try {
    $script:SW = New-Object System.IO.StreamWriter((New-Object System.IO.FileStream($LogPath, [System.IO.FileMode]::Append, [System.IO.FileAccess]::Write, [System.IO.FileShare]::ReadWrite)), (New-Object System.Text.UTF8Encoding($false)))
} catch {
    # 默认日志被残留进程占用时,改用独立日志文件继续工作
    $fallback = Join-Path $ScriptDir ('mhxy-launcher-' + $PID + '.log')
    try {
        $script:SW = New-Object System.IO.StreamWriter((New-Object System.IO.FileStream($fallback, [System.IO.FileMode]::Append, [System.IO.FileAccess]::Write, [System.IO.FileShare]::ReadWrite)), (New-Object System.Text.UTF8Encoding($false)))
        $script:SW.WriteLine('')
        $script:SW.WriteLine('[注意] 默认日志被占用,本次日志: ' + $fallback)
    } catch { $script:SW = $null }
}
$script:SW.AutoFlush = $true
function Say {
    param([string]$Msg, [string]$Color = '')
    # Gui 模式(隐藏运行)下不写控制台:隐藏控制台的 Write-Host 会挂起,信息只进日志/弹窗
    if (-not $Gui) {
        if ($Color) { Write-Host $Msg -ForegroundColor $Color } else { Write-Host $Msg }
    }
    if ($script:SW) { $script:SW.WriteLine(('[{0:HH:mm:ss.fff}] {1}' -f (Get-Date), $Msg)) }
}
function Log {
    param([string]$Msg)
    if ($script:SW) { $script:SW.WriteLine(('[{0:HH:mm:ss.fff}] {1}' -f (Get-Date), $Msg)) }
}
function Log-Sep {
    if (-not $script:SW) { return }
    $script:SW.WriteLine('')
    $script:SW.WriteLine('=' * 100)
    $script:SW.WriteLine(('==== 运行开始 {0}  模式={1}  版本=v2.3 ====' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Mode))
    $script:SW.WriteLine('=' * 100)
}

# ---------------------------------------------------------------------------
#  图形界面辅助(Gui 模式:隐藏窗口下用对话框代替控制台交互)
# ---------------------------------------------------------------------------
function Show-Msg {
    param([string]$Text, [string]$Title = '五开助手', [string]$Type = 'Info')
    $icon = switch ($Type) {
        'Error' { [System.Windows.Forms.MessageBoxIcon]::Error }
        'Warn'  { [System.Windows.Forms.MessageBoxIcon]::Warning }
        default { [System.Windows.Forms.MessageBoxIcon]::Information }
    }
    [void][System.Windows.Forms.MessageBox]::Show($Text, $Title, [System.Windows.Forms.MessageBoxButtons]::OK, $icon)
}
function Confirm-YesNo {
    param([string]$Text, [string]$Title = '五开助手')
    $r = [System.Windows.Forms.MessageBox]::Show($Text, $Title, [System.Windows.Forms.MessageBoxButtons]::YesNo, [System.Windows.Forms.MessageBoxIcon]::Question)
    return ($r -eq [System.Windows.Forms.DialogResult]::Yes)
}
function Show-Input {
    param([string]$Prompt, [string]$Title = '五开助手', [string]$Default = '')
    $form = New-Object System.Windows.Forms.Form
    $form.Text = $Title
    $form.Size = New-Object System.Drawing.Size(560, 170)
    $form.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
    $form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedDialog
    $form.MaximizeBox = $false; $form.MinimizeBox = $false
    $lbl = New-Object System.Windows.Forms.Label
    $lbl.Text = $Prompt; $lbl.Location = New-Object System.Drawing.Point(12, 12); $lbl.Size = New-Object System.Drawing.Size(520, 40)
    $txt = New-Object System.Windows.Forms.TextBox
    $txt.Text = $Default; $txt.Location = New-Object System.Drawing.Point(12, 58); $txt.Size = New-Object System.Drawing.Size(520, 24)
    $ok = New-Object System.Windows.Forms.Button; $ok.Text = '确定'; $ok.Location = New-Object System.Drawing.Point(320, 92); $ok.Size = New-Object System.Drawing.Size(100, 30); $ok.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $cancel = New-Object System.Windows.Forms.Button; $cancel.Text = '取消'; $cancel.Location = New-Object System.Drawing.Point(432, 92); $cancel.Size = New-Object System.Drawing.Size(100, 30); $cancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $form.Controls.AddRange([System.Windows.Forms.Control[]]@($lbl, $txt, $ok, $cancel))
    $form.AcceptButton = $ok; $form.CancelButton = $cancel
    if ($form.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { return $txt.Text } else { return $null }
}

Add-Type -AssemblyName System.Windows.Forms | Out-Null
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public class MHWin32 {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassName(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsZoomed(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    [DllImport("user32.dll")] public static extern bool GetWindowPlacement(IntPtr hWnd, ref WINDOWPLACEMENT lpwndpl);
    [DllImport("user32.dll", SetLastError = true)] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
    [DllImport("kernel32.dll")] public static extern uint GetLastError();
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
    [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr hWnd, int nIndex);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
    [StructLayout(LayoutKind.Sequential)] public struct WINDOWPLACEMENT {
        public int length;
        public int flags;
        public int showCmd;
        public System.Drawing.Point ptMinPosition;
        public System.Drawing.Point ptMaxPosition;
        public RECT rcNormalPosition;
    }
}
"@ -ReferencedAssemblies System.Drawing
try { [void][MHWin32]::SetProcessDPIAware() } catch { }
# Gui 模式:主动隐藏本进程的控制台窗口(即使提权启动时 -WindowStyle Hidden 被忽略)
try {
    if ($Gui) { [void][MHWin32]::ShowWindow([MHWin32]::GetConsoleWindow(), 0) }
} catch { }

# ---------------------------------------------------------------------------
#  进程映射(名称 + 父子关系)
# ---------------------------------------------------------------------------
$script:ProcName   = @{}
$script:ProcParent = @{}
function Update-ProcMaps {
    try {
        $names = @{}
        foreach ($p in Get-Process -ErrorAction SilentlyContinue) {
            if (-not $names.ContainsKey($p.Id)) { $names[$p.Id] = $p.ProcessName }
        }
        $script:ProcName = $names
    } catch { Log ('Get-Process 失败: ' + $_.Exception.Message) }
    try {
        $parents = @{}
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ForEach-Object {
            $parents[[int]$_.ProcessId] = [int]$_.ParentProcessId
        }
        $script:ProcParent = $parents
    } catch { Log ('CIM 查询失败: ' + $_.Exception.Message) }
}
function Get-ProcName {
    param([int]$pid_)
    if ($script:ProcName.ContainsKey($pid_)) { return $script:ProcName[$pid_] }
    return ''
}
function Get-ProcParent {
    param([int]$pid_)
    if ($script:ProcParent.ContainsKey($pid_)) { return $script:ProcParent[$pid_] }
    return 0
}
# 向上追溯父链,返回命中的槽位索引(pid 属于哪个已启动/已有的实例);找不到返回 -1
function Find-AncestorSlot {
    param([int]$pid_, [hashtable]$slotPids)
    $cur = $pid_
    for ($i = 0; $i -lt 6 -and $cur -ne 0; $i++) {
        if ($slotPids.ContainsKey($cur)) { return $slotPids[$cur] }
        $cur = Get-ProcParent $cur
    }
    return -1
}
function Get-ParentChain {
    param([int]$pid_, [hashtable]$slotPids)
    $parts = @()
    $cur = $pid_
    for ($i = 0; $i -lt 6 -and $cur -ne 0; $i++) {
        $nm = Get-ProcName $cur
        $mark = ''
        if ($slotPids -and $slotPids.ContainsKey($cur)) { $mark = '(槽位' + ($slotPids[$cur] + 1) + ')' }
        $parts += ($cur.ToString() + '(' + $nm + ')' + $mark)
        $cur = Get-ProcParent $cur
        if ($cur -eq 0) { break }
    }
    return ($parts -join ' <- ')
}

# ---------------------------------------------------------------------------
#  窗口枚举与分类
# ---------------------------------------------------------------------------
$script:EnumList = New-Object System.Collections.Generic.List[object]
function Get-MatchedWindows {
    param([string[]]$MatchProcesses)
    $script:EnumList = New-Object System.Collections.Generic.List[object]
    $cb = [MHWin32+EnumWindowsProc]{
        param($hWnd, $lParam)
        try {
            if ([MHWin32]::IsWindowVisible($hWnd)) {
                $procId = [uint32]0
                [void][MHWin32]::GetWindowThreadProcessId($hWnd, [ref]$procId)
                $name = Get-ProcName ([int]$procId)
                if ($name -ne '' -and ($script:EnumMatch -contains $name)) {
                    $sbT = New-Object System.Text.StringBuilder 256
                    [void][MHWin32]::GetWindowText($hWnd, $sbT, 256)
                    $sbC = New-Object System.Text.StringBuilder 256
                    [void][MHWin32]::GetClassName($hWnd, $sbC, 256)
                    $r = New-Object MHWin32+RECT
                    [void][MHWin32]::GetWindowRect($hWnd, [ref]$r)
                    $style = [MHWin32]::GetWindowLong($hWnd, -16)
                    $rr = $r
                    if ([MHWin32]::IsIconic($hWnd)) {
                        $pl = New-Object MHWin32+WINDOWPLACEMENT
                        $pl.length = [Runtime.InteropServices.Marshal]::SizeOf([type][MHWin32+WINDOWPLACEMENT])
                        if ([MHWin32]::GetWindowPlacement($hWnd, [ref]$pl)) { $rr = $pl.rcNormalPosition }
                    }
                    $script:EnumList.Add([pscustomobject]@{
                        Handle = $hWnd; PID = [int]$procId; Proc = $name
                        Title = $sbT.ToString(); Class = $sbC.ToString()
                        X = $r.Left; Y = $r.Top; W = $r.Right - $r.Left; H = $r.Bottom - $r.Top
                        RX = $rr.Left; RY = $rr.Top; RW = $rr.Right - $rr.Left; RH = $rr.Bottom - $rr.Top
                        Resizable = (($style -band 0x40000) -ne 0)
                        Iconic = [MHWin32]::IsIconic($hWnd)
                    })
                }
            }
        } catch { }
        return $true
    }
    $script:EnumMatch = $MatchProcesses
    [void][MHWin32]::EnumWindows($cb, [IntPtr]::Zero)
    $result = @()
    foreach ($w in $script:EnumList) {
        $w | Add-Member -NotePropertyName ParentPid -NotePropertyValue (Get-ProcParent $w.PID) -Force
        $result += $w
    }
    return $result
}
# 弹窗/阴影窗/工具窗判定(类名/尺寸/标题)
$script:DlgClassPatterns = 'MPAY|DuiShadowWnd|WebView2|GDI\+|IME|MSCTF'
function Test-DialogWindow {
    param($w)
    if ($w.RW -lt 120 -or $w.RH -lt 80) { return $true }   # 用还原尺寸判断(最小化窗口也能正确识别)
    if ($w.Class -match $script:DlgClassPatterns) { return $true }
    if ($w.Title -match '^MPAY_') { return $true }
    return $false
}
# 每个进程挑一个主窗口:标题含关键字优先,否则取最大的非弹窗窗口
function Get-MainWindows {
    param($wins, [string[]]$TitleKeywords)
    $byPid = @{}
    foreach ($w in $wins) {
        if (-not $byPid.ContainsKey($w.PID)) { $byPid[$w.PID] = New-Object System.Collections.Generic.List[object] }
        $byPid[$w.PID].Add($w)
    }
    $mains = @()
    foreach ($pid_ in @($byPid.Keys)) {
        $cands = @($byPid[$pid_] | Where-Object { -not (Test-DialogWindow $_) })
        if ($cands.Count -eq 0) { continue }
        $kw = @($cands | Where-Object {
            $hit = $false
            foreach ($k in $TitleKeywords) {
                if ($_.Title.IndexOf($k, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) { $hit = $true; break }
            }
            $hit
        })
        $main = if ($kw.Count -gt 0) { @($kw | Sort-Object @{ e = { $_.RW * $_.RH } } -Descending)[0] } else { @($cands | Sort-Object @{ e = { $_.RW * $_.RH } } -Descending)[0] }
        $mains += $main
    }
    return $mains
}
# 显示统一用还原矩形(最小化窗口显示其还原位置,而非 (-32000,-32000) 160x28)
function Format-Win {
    param($w)
    $mark = if ($w.Iconic) { ' [最小化]' } else { '' }
    return ('PID={0}({1}) 标题=[{2}] 类=[{3}] rect=({4},{5}) {6}x{7}{8}' -f $w.PID, $w.Proc, $w.Title, $w.Class, $w.RX, $w.RY, $w.RW, $w.RH, $mark)
}

# ---------------------------------------------------------------------------
#  配置
# ---------------------------------------------------------------------------
function Get-VirtualScreen {
    return [System.Windows.Forms.SystemInformation]::VirtualScreen
}
function Get-Config {
    if (Test-Path -LiteralPath $ConfigPath) {
        $c = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } else {
        $c = [pscustomobject]@{}
    }
    $defaults = [ordered]@{
        exePath                = ''
        workingDir             = ''
        launchDelaySeconds     = 3
        waitTimeoutSeconds     = 120
        sweepSeconds           = 300
        matchProcesses         = @('MyGame_x64r', 'MyLauncher_x64r', 'MyPCLauncher_x64r', 'MyPreloader_x64r')
        matchTitleKeywords     = @('梦幻西游')
        scaleOnDifferentScreen = $true
        count                  = 0
        virtualScreen          = $null
        windows                = @()
    }
    foreach ($k in $defaults.Keys) {
        if (-not $c.PSObject.Properties[$k]) { $c | Add-Member -NotePropertyName $k -NotePropertyValue $defaults[$k] }
    }
    $c.windows = @($c.windows)
    $c.matchProcesses = @($c.matchProcesses)
    $c.matchTitleKeywords = @($c.matchTitleKeywords)
    return $c
}
function Save-Config {
    param($cfg)
    $cfg | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $ConfigPath -Encoding UTF8
}
function Resolve-Exe {
    param($cfg)
    $candidates = @()
    if ($ExePath) { $candidates += $ExePath }
    if ($cfg.exePath) { $candidates += $cfg.exePath }
    $candidates += (Join-Path $ScriptDir '..\My\MyLauncher_x64r.exe')
    $candidates += (Join-Path $ScriptDir 'MyLauncher_x64r.exe')
    foreach ($p in $candidates) {
        if ($p -and (Test-Path -LiteralPath $p)) { return (Get-Item -LiteralPath $p).FullName }
    }
    throw "找不到客户端程序(MyLauncher_x64r.exe)! 请用「设置客户端路径.cmd」指定路径"
}
function Build-Rects {
    param($cfg, [int]$N, $vs)
    $rects = @()
    $saved = @($cfg.windows)
    $from = $cfg.virtualScreen
    $screenChanged = $false
    if ($from -and $from.w -and $from.h) {
        # 参照屏 = 与保存时 virtualScreen 矩形完全一致的当前显示器。
        # 只要它还在(哪怕另外开了虚拟屏),就按原坐标摆放 —— 多开/关虚拟屏不再触发缩放;
        # 只有参照屏消失(真实改分辨率/换屏)才回退为按整虚拟桌面比例缩放。
        $anchorAlive = $false
        foreach ($s in [System.Windows.Forms.Screen]::AllScreens) {
            $b = $s.Bounds
            if ([int]$b.X -eq [int]$from.x -and [int]$b.Y -eq [int]$from.y -and [int]$b.Width -eq [int]$from.w -and [int]$b.Height -eq [int]$from.h) { $anchorAlive = $true; break }
        }
        if (-not $anchorAlive -and ($from.w -ne $vs.Width -or $from.h -ne $vs.Height)) { $screenChanged = $true }
    }
    for ($i = 0; $i -lt $N; $i++) {
        $valid = $false
        if ($i -lt $saved.Count -and $saved[$i]) {
            $x = [int]$saved[$i].x; $y = [int]$saved[$i].y; $w = [int]$saved[$i].w; $h = [int]$saved[$i].h
            if ($w -ge 100 -and $h -ge 80 -and $x -gt -5000 -and $y -gt -5000) { $valid = $true }
            else { Say ("警告: 槽位{0} 保存的布局异常(({1},{2}) {3}x{4}),改用网格摆放" -f ($i + 1), $x, $y, $w, $h) 'Yellow' }
        }
        if ($valid) {
            if ($screenChanged -and $cfg.scaleOnDifferentScreen -and $from.w -gt 0 -and $from.h -gt 0) {
                $x = [int]($x * $vs.Width / $from.w); $y = [int]($y * $vs.Height / $from.h)
                $w = [int]($w * $vs.Width / $from.w); $h = [int]($h * $vs.Height / $from.h)
            }
            $rects += [pscustomobject]@{ x = $x; y = $y; w = $w; h = $h }
        } else {
            $cols = [Math]::Ceiling([Math]::Sqrt($N)); $rows = [Math]::Ceiling($N / $cols)
            $cw = [Math]::Floor($vs.Width / $cols); $ch = [Math]::Floor($vs.Height / $rows)
            $c = $i % $cols; $rr = [Math]::Floor($i / $cols)
            $rects += [pscustomobject]@{ x = $vs.X + $c * $cw; y = $vs.Y + $rr * $ch; w = $cw; h = $ch }
        }
    }
    if ($screenChanged) { Say ('注意: 屏幕尺寸与保存布局时不同({0}x{1} -> {2}x{3})' -f $from.w, $from.h, $vs.Width, $vs.Height) 'Yellow' }
    return $rects
}

# ---------------------------------------------------------------------------
#  摆放 + 回读验证 + 槽位就近匹配
# ---------------------------------------------------------------------------
# SetWindowPos 并记录失败错误码(区分"调用被拒"与"游戏抢回")
function Set-WindowRect {
    param([IntPtr]$hWnd, $x, $y, $w, $h, [string]$who)
    $ok = [MHWin32]::SetWindowPos($hWnd, [IntPtr]::Zero, $x, $y, $w, $h, 0x0014)
    if (-not $ok) { Log ("[摆放] {0} SetWindowPos 返回失败, 错误码={1}" -f $who, [MHWin32]::GetLastError()) }
    return $ok
}
function Apply-Rect {
    param([IntPtr]$hWnd, $Rect, $vs, [string]$who)
    if ($Rect.w -le 0 -or $Rect.h -le 0) { return }
    try {
        if ([MHWin32]::IsIconic($hWnd)) { [void][MHWin32]::ShowWindow($hWnd, 9) }
        $x = [int]$Rect.x; $y = [int]$Rect.y; $w = [int]$Rect.w; $h = [int]$Rect.h
        $vx = [int]$vs.X; $vy = [int]$vs.Y
        $vr = $vx + [int]$vs.Width; $vb = $vy + [int]$vs.Height
        if ($x -lt $vx) { $x = $vx }
        if ($y -lt $vy) { $y = $vy }
        if ($x + $w -gt $vr) { $x = [Math]::Max($vx, $vr - $w) }
        if ($y + $h -gt $vb) { $y = [Math]::Max($vy, $vb - $h) }
        Set-WindowRect $hWnd $x $y $w $h $who | Out-Null
        Say ("[摆放] $who -> ({0},{1}) {2}x{3}" -f $x, $y, $w, $h)
        return [pscustomobject]@{ x = $x; y = $y; w = $w; h = $h }
    } catch {
        Log ('[摆放失败] ' + $_.Exception.Message)
        return $null
    }
}
# 窗口当前(最小化窗口不在此判断)是否已在目标位置附近(±20px)
function Test-AtTarget {
    param($W, $Rect)
    if ($W.Iconic) { return $false }
    return ([Math]::Abs($W.RX - $Rect.x) -le 20 -and [Math]::Abs($W.RY - $Rect.y) -le 20 -and [Math]::Abs($W.RW - $Rect.w) -le 20 -and [Math]::Abs($W.RH - $Rect.h) -le 20)
}
function Verify-Rect {
    param([IntPtr]$hWnd, $Target, [string]$who)
    # 摆放后回读验证,偏差大则重试(最多3次);最小化窗口按还原矩形回读
    for ($try = 0; $try -lt 3; $try++) {
        Start-Sleep -Milliseconds 1200
        if (-not [MHWin32]::IsWindow($hWnd)) {
            Log ("[验证] $who 窗口已不存在")
            return $false
        }
        $r = New-Object MHWin32+RECT
        [void][MHWin32]::GetWindowRect($hWnd, [ref]$r)
        $ax = $r.Left; $ay = $r.Top; $aw = $r.Right - $r.Left; $ah = $r.Bottom - $r.Top
        if ([MHWin32]::IsIconic($hWnd)) {
            $pl = New-Object MHWin32+WINDOWPLACEMENT
            $pl.length = [Runtime.InteropServices.Marshal]::SizeOf([type][MHWin32+WINDOWPLACEMENT])
            if ([MHWin32]::GetWindowPlacement($hWnd, [ref]$pl)) {
                $ax = $pl.rcNormalPosition.Left; $ay = $pl.rcNormalPosition.Top
                $aw = $pl.rcNormalPosition.Right - $pl.rcNormalPosition.Left
                $ah = $pl.rcNormalPosition.Bottom - $pl.rcNormalPosition.Top
            }
        }
        $dx = [Math]::Abs($ax - $Target.x); $dy = [Math]::Abs($ay - $Target.y)
        $dw = [Math]::Abs($aw - $Target.w); $dh = [Math]::Abs($ah - $Target.h)
        $ok = ($dx -le 20 -and $dy -le 20 -and $dw -le 20 -and $dh -le 20)
        Log ("[验证] $who 目标=({0},{1}) {2}x{3} 实际=({4},{5}) {6}x{7} 偏差=({8},{9},{10},{11}) {12}" -f $Target.x, $Target.y, $Target.w, $Target.h, $ax, $ay, $aw, $ah, $dx, $dy, $dw, $dh, $(if ($ok) { 'OK' } else { '偏差' }))
        if ($ok) { return $true }
        if ($try -lt 2) {
            Set-WindowRect $hWnd $Target.x $Target.y $Target.w $Target.h $who | Out-Null
            Log ("[验证] $who 第{0}次重试" -f ($try + 1))
        }
    }
    Say ("[验证] $who 摆放后仍有偏差(游戏可能强制改变位置/大小)") 'Yellow'
    return $false
}
# 槽位就近匹配:窗口中心到槽位中心的平方距离,暴力枚举全排列取总距离最小
# (窗口≤6 → 排列≤720;从恒等排列开始枚举,平局优先恒等,窗口重叠时退化为原顺序)
function Resolve-SlotMatch {
    param($Windows, $Targets)
    $script:M_NR = @($Windows).Count   # 窗口数(行)
    $script:M_NC = @($Targets).Count   # 槽位数(列)
    if ($script:M_NR -eq 0 -or $script:M_NC -eq 0) { return @() }
    if ($script:M_NR -gt $script:M_NC) { throw '窗口数多于槽位数,无法匹配' }
    $script:M_Cost = New-Object 'double[,]' -ArgumentList @($script:M_NR, $script:M_NC)
    for ($i = 0; $i -lt $script:M_NR; $i++) {
        $wx = [double]$Windows[$i].RX + [double]$Windows[$i].RW / 2
        $wy = [double]$Windows[$i].RY + [double]$Windows[$i].RH / 2
        for ($j = 0; $j -lt $script:M_NC; $j++) {
            $tx = [double]$Targets[$j].x + [double]$Targets[$j].w / 2
            $ty = [double]$Targets[$j].y + [double]$Targets[$j].h / 2
            $dx = $wx - $tx; $dy = $wy - $ty
            $script:M_Cost[$i, $j] = $dx * $dx + $dy * $dy
        }
    }
    $script:M_BestPerm = $null; $script:M_BestCost = [double]::MaxValue
    $script:M_Perm = New-Object int[] $script:M_NR
    $script:M_Used = New-Object bool[] $script:M_NC
    function Visit($k, $total) {
        if ($k -ge $script:M_NR) {
            if ($total -lt $script:M_BestCost) { $script:M_BestCost = $total; $script:M_BestPerm = $script:M_Perm.Clone() }
            return
        }
        for ($j = 0; $j -lt $script:M_NC; $j++) {
            if ($script:M_Used[$j]) { continue }
            $nt = $total + $script:M_Cost[$k, $j]
            if ($nt -ge $script:M_BestCost) { continue }   # 剪枝
            $script:M_Used[$j] = $true; $script:M_Perm[$k] = $j
            Visit ($k + 1) $nt
            $script:M_Used[$j] = $false
        }
    }
    Visit 0 0.0
    return ,$script:M_BestPerm
}
# 绑定槽位主窗口并摆放(已有/血缘/补调/孤儿共用);窗口已在目标位置则跳过
function Bind-SlotMain {
    param($Slot, $M, $SlotPids, $Rects, $Vs, [string]$Tag = '')
    $Slot.Handle = $M.Handle; $Slot.PID = $M.PID; $Slot.Proc = $M.Proc; $Slot.Title = $M.Title; $Slot.Assigned = $true
    $SlotPids[$M.PID] = $Slot.Index
    $who = '槽位' + ($Slot.Index + 1) + $Tag
    $rect = $Rects[$Slot.Index]
    if (Test-AtTarget $M $rect) { Log ('[匹配] ' + $who + ' 已在目标位置,无需移动'); return }
    $t = Apply-Rect $M.Handle $rect $Vs $who
    if ($t) { [void](Verify-Rect $M.Handle $t $who) }
}

# ---------------------------------------------------------------------------
#  自检(不操作任何真实窗口)
# ---------------------------------------------------------------------------
function Invoke-SelfTest {
    Say '==== 自检 ====' 'Cyan'
    $pass = 0; $fail = 0
    function T { param([bool]$cond, [string]$name) if ($cond) { $script:p++; Say ('  [通过] ' + $name) } else { $script:f++; Say ('  [失败] ' + $name) 'Red' } }
    $script:p = 0; $script:f = 0
    # 1) 弹窗判定
    T (Test-DialogWindow ([pscustomobject]@{ W = 360; H = 400; RW = 360; RH = 400; Class = 'MPAY_LOGIN'; Title = '登录' })) 'MPAY_LOGIN 判定为弹窗'
    T (Test-DialogWindow ([pscustomobject]@{ W = 890; H = 702; RW = 890; RH = 702; Class = 'DuiShadowWnd'; Title = '' })) 'DuiShadowWnd 判定为弹窗'
    T (Test-DialogWindow ([pscustomobject]@{ W = 60; H = 60; RW = 60; RH = 60; Class = 'MPAY_LOADINF_FORM'; Title = 'MPAY_LOADING_FORM' })) '小窗口(60x60)判定为弹窗'
    T (-not (Test-DialogWindow ([pscustomobject]@{ W = 860; H = 672; RW = 860; RH = 672; Class = '8nNTEtZ1smtkYCcD3Ss1yvi7d'; Title = '梦幻西游：时空' }))) '主窗口(随机类名+游戏标题)判定为非弹窗'
    T (-not (Test-DialogWindow ([pscustomobject]@{ W = 860; H = 672; RW = 860; RH = 672; Class = 'Notepad'; Title = '无标题 - 记事本' }))) '普通窗口判定为非弹窗'
    # 2) 主窗口挑选:每进程一个
    $fake = @(
        [pscustomobject]@{ PID = 1; Handle = [IntPtr]11; Title = '梦幻西游：时空'; Class = 'Aa'; W = 860; H = 672; RW = 860; RH = 672 },
        [pscustomobject]@{ PID = 1; Handle = [IntPtr]12; Title = '登录'; Class = 'MPAY_LOGIN'; W = 360; H = 400; RW = 360; RH = 400 },
        [pscustomobject]@{ PID = 1; Handle = [IntPtr]13; Title = ''; Class = 'DuiShadowWnd'; W = 890; H = 702; RW = 890; RH = 702 },
        [pscustomobject]@{ PID = 2; Handle = [IntPtr]21; Title = '梦幻西游：时空'; Class = 'Bb'; W = 860; H = 672; RW = 860; RH = 672 },
        [pscustomobject]@{ PID = 3; Handle = [IntPtr]31; Title = ''; Class = 'Cc'; W = 860; H = 672; RW = 860; RH = 672 },
        [pscustomobject]@{ PID = 4; Handle = [IntPtr]41; Title = '梦幻西游：时空'; Class = 'Dd'; W = 160; H = 28; RW = 969; RH = 754 }  # 最小化主窗口(当前160x28, 还原969x754)
    )
    $mains = Get-MainWindows $fake @('梦幻西游')
    T ($mains.Count -eq 4) ('每进程一个主窗口(共4进程含最小化, 实际 ' + $mains.Count + ')')
    $mByPid = @{}
    foreach ($m in $mains) { $mByPid[$m.PID] = $m }
    T ($mByPid.ContainsKey(1) -and $mByPid[1].Handle -eq [IntPtr]11) '进程1选中游戏标题主窗口'
    T ($mByPid.ContainsKey(2) -and $mByPid[2].Handle -eq [IntPtr]21) '进程2选中游戏标题主窗口'
    T ($mByPid.ContainsKey(3) -and $mByPid[3].Handle -eq [IntPtr]31) '进程3无关键字时选中最大非弹窗窗口'
    T ($mByPid.ContainsKey(4) -and $mByPid[4].Handle -eq [IntPtr]41) '进程4(最小化)用还原尺寸判定为主窗口'
    # 3) 血缘查找
    $script:ProcParent = @{ 100 = 200; 200 = 0; 300 = 400; 400 = 0; 500 = 600; 600 = 700; 700 = 0 }
    $script:ProcName = @{ 100 = 'MyGame_x64r'; 200 = 'MyLauncher_x64r'; 300 = 'MyGame_x64r'; 400 = 'MyLauncher_x64r'; 500 = 'MyGame_x64r'; 600 = 'MyLauncher_x64r'; 700 = 'explorer' }
    $slotPids = @{ 200 = 0; 400 = 1; 600 = 2 }
    T ((Find-AncestorSlot 100 $slotPids) -eq 0) '血缘: MyGame←MyLauncher 命中槽位1'
    T ((Find-AncestorSlot 300 $slotPids) -eq 1) '血缘: 命中槽位2'
    T ((Find-AncestorSlot 500 $slotPids) -eq 2) '血缘: 命中槽位3'
    T ((Find-AncestorSlot 999 $slotPids) -eq -1) '无关进程返回 -1'
    # 4) 槽位就近匹配(窗口中心到槽位中心)
    $tgt = @(
        [pscustomobject]@{ x = 0;   y = 0;   w = 960; h = 540 },
        [pscustomobject]@{ x = 0;   y = 540; w = 960; h = 540 },
        [pscustomobject]@{ x = 960; y = 0;   w = 960; h = 540 },
        [pscustomobject]@{ x = 960; y = 540; w = 960; h = 540 }
    )
    $w0 = [pscustomobject]@{ RX = 0; RY = 0; RW = 960; RH = 540 }       # 左上
    $w1 = [pscustomobject]@{ RX = 0; RY = 540; RW = 960; RH = 540 }     # 左下
    $w2 = [pscustomobject]@{ RX = 960; RY = 0; RW = 960; RH = 540 }     # 右上
    $w3 = [pscustomobject]@{ RX = 600; RY = 400; RW = 960; RH = 540 }   # 右下被拖到靠近中央(600,400),中心(1080,670)最近BR槽
    $p = Resolve-SlotMatch @($w0, $w1, $w2, $w3) $tgt
    T (($p -join ',') -eq '0,1,2,3') '匹配: 右下窗口移到中央附近仍归右下槽,其余不动'
    $p = Resolve-SlotMatch @($w1, $w3, $w0) $tgt
    T (($p -join ',') -eq '1,3,0') '匹配: 窗口乱序(PID与空间顺序不一致)也各归其位'
    $p = Resolve-SlotMatch @($w0, $w1) $tgt
    T (($p -join ',') -eq '0,1') '匹配: 窗口少于槽位时按最近匹配,多余槽位留空'
    Say ('自检完成: 通过 ' + $script:p + ' / 失败 ' + $script:f) $(if ($script:f -eq 0) { 'Green' } else { 'Red' })
    if ($Gui) { if ($script:f -eq 0) { Show-Msg ('自检通过: ' + $script:p + ' 项全部正常') '自检' } else { Show-Msg ('自检有 ' + $script:f + ' 项失败,详见 mhxy-launcher.log') '自检' 'Warn' } }
}

# ---------------------------------------------------------------------------
#  模式: setpath
# ---------------------------------------------------------------------------
function Invoke-SetPath {
    $cfg = Get-Config
    $path = $ExePath
    while ($true) {
        if (-not $path) {
            if ($Gui) {
                $path = Show-Input '请输入梦幻西游客户端完整路径(或直接把 MyLauncher_x64r.exe 拖到「设置客户端路径」图标上)' '设置客户端路径' $cfg.exePath
                if ($null -eq $path) { Say '用户取消'; return }
            } else {
                $input_ = Read-Host '请输入梦幻西游客户端完整路径(可直接拖入exe文件路径,空回车=取消)'
                if ($null -eq $input_) { $input_ = '' }
                $path = $input_
            }
        }
        $path = $path.Trim().Trim('"')
        if (-not $path) {
            if ($Gui) { Show-Msg '未输入路径,已取消' '设置客户端路径' 'Warn' }
            Say '未输入路径,已取消' 'Yellow'
            return
        }
        if (Test-Path -LiteralPath $path) { break }
        if ($Gui) { Show-Msg ('路径不存在: ' + $path) '设置客户端路径' 'Warn' } else { Say ('路径不存在: ' + $path) 'Red' }
        $path = ''
    }
    $full = (Get-Item -LiteralPath $path).FullName
    if ($full -notmatch 'MyLauncher_x64r\.exe$') {
        Say ('警告: 目标文件是 ' + (Split-Path -Leaf $full) + ',不是启动器 MyLauncher_x64r.exe,通常应指向启动器') 'Yellow'
    }
    $cfg.exePath = $full
    Save-Config $cfg
    Say ('已保存客户端路径 -> ' + $full) 'Green'
    Say '以后直接双击"一键五开"即可' 'Green'
    if ($Gui) { Show-Msg ('已保存客户端路径 -> ' + $full) '设置客户端路径' }
}

# ---------------------------------------------------------------------------
#  模式: list
# ---------------------------------------------------------------------------
function Invoke-List {
    $cfg = Get-Config
    Update-ProcMaps
    $wins = @(Get-MatchedWindows $cfg.matchProcesses)
    $mains = @(Get-MainWindows $wins $cfg.matchTitleKeywords)
    $mainKeys = @{}
    foreach ($m in $mains) { $mainKeys[[string]$m.Handle] = $true }
    Say ('找到可见窗口 ' + $wins.Count + ' 个(主窗口 ' + $mains.Count + ' 个):') 'Cyan'
    $i = 0
    foreach ($w in ($wins | Sort-Object PID, @{ e = { $_.RW * $_.RH } } -Descending)) {
        $i++
        $mark = if ($mainKeys.ContainsKey([string]$w.Handle)) { '[主]' } else { '[弹窗]' }
        Say ('  {0,2}. {1} {2}' -f $i, $mark, (Format-Win $w))
        Say ('      血缘: ' + (Get-ParentChain $w.PID $null))
    }
    if ($Gui) { Show-Msg ('找到 {0} 个窗口(主窗口 {1} 个),详见 mhxy-launcher.log' -f $wins.Count, $mains.Count) '查看窗口' }
}

# ---------------------------------------------------------------------------
#  模式: record
# ---------------------------------------------------------------------------
function Invoke-Record {
    $cfg = Get-Config
    Update-ProcMaps
    $wins = @(Get-MatchedWindows $cfg.matchProcesses)
    $mains = @(Get-MainWindows $wins $cfg.matchTitleKeywords | Sort-Object PID)
    if ($mains.Count -eq 0) {
        Say '没有找到梦幻西游主窗口!' 'Red'
        Say '请先手动打开所有梦幻西游窗口,再运行"记录当前布局"' 'Yellow'
        return
    }
    Say ('检测到 {0} 个实例(按进程排序):' -f $mains.Count) 'Cyan'
    $i = 0
    foreach ($m in $mains) {
        $i++
        Say ('  [{0}] {1}' -f $i, (Format-Win $m))
    }
    $dialogs = @($wins | Where-Object { Test-DialogWindow $_ })
    if ($dialogs.Count -gt 0) { Log ('另有弹窗/小窗口 ' + $dialogs.Count + ' 个(不参与记录): ' + ((@($dialogs | ForEach-Object { $_.Title + '/' + $_.Class }) -join '; '))) }
    $minCount = @($mains | Where-Object { $_.Iconic }).Count
    if ($minCount -gt 0) { Say ('注意: {0} 个窗口处于最小化状态,将记录其还原位置(列表中标注[最小化])' -f $minCount) 'Yellow' }
    $ans = 'y'
    if (-not $Force) {
        if ($Gui) {
            if (-not (Confirm-YesNo ('确认按以上 {0} 个窗口保存布局?' -f $mains.Count))) { $ans = 'n' }
        } else {
            $ans = Read-Host ('确认按以上 {0} 个窗口保存布局? [Y/N] (回车=Y)' -f $mains.Count)
            if ($null -eq $ans) { $ans = '' }
            if ($ans -eq '') { $ans = 'y' }
        }
    }
    if ($ans -notmatch '^[yY]') { Say '已取消' 'Yellow'; return }
    $vs = Get-VirtualScreen
    $cfg.virtualScreen = [pscustomobject]@{ x = $vs.X; y = $vs.Y; w = $vs.Width; h = $vs.Height }
    $cfg.windows = @()
    # 保存还原矩形(RX/RY/RW/RH):最小化窗口也记录其还原位置,而非 (-32000,-32000) 160x28
    foreach ($m in $mains) { $cfg.windows += [pscustomobject]@{ x = $m.RX; y = $m.RY; w = $m.RW; h = $m.RH } }
    $cfg.count = $mains.Count
    $cfg.exePath = Resolve-Exe $cfg
    Save-Config $cfg
    Say ('已保存 {0} 个窗口的布局 -> {1}' -f $cfg.windows.Count, $ConfigPath) 'Green'
    Log ('布局明细: ' + (($cfg.windows | ForEach-Object { ('({0},{1}) {2}x{3}' -f $_.x, $_.y, $_.w, $_.h) }) -join '; '))
    if ($Gui) { Show-Msg ('已保存 {0} 个窗口的布局' -f $cfg.windows.Count) '记录当前布局' }
}

# ---------------------------------------------------------------------------
#  模式: arrange
# ---------------------------------------------------------------------------
function Invoke-Arrange {
    $cfg = Get-Config
    $saved = @($cfg.windows)
    if ($saved.Count -eq 0) { Say '还没有保存过布局! 请先运行"记录当前布局"' 'Red'; return }
    Update-ProcMaps
    $wins = @(Get-MatchedWindows $cfg.matchProcesses)
    $mains = @(Get-MainWindows $wins $cfg.matchTitleKeywords | Sort-Object PID)
    if ($mains.Count -eq 0) { Say '没有找到梦幻西游主窗口' 'Red'; return }
    $vs = Get-VirtualScreen
    # 窗口不多于保存布局时用全部保存槽位作目标(多余槽位留空),否则补网格槽位
    $rects = Build-Rects $cfg ([Math]::Max($mains.Count, $saved.Count)) $vs
    $perm = Resolve-SlotMatch $mains $rects
    Say ('整理 {0} 个主窗口(按当前位置就近匹配槽位):' -f $mains.Count) 'Cyan'
    for ($i = 0; $i -lt $mains.Count; $i++) {
        $si = $perm[$i]
        Log ("[匹配] 窗口#{0} PID={1} 当前=({2},{3}) {4}x{5} -> 槽位{6} 目标=({7},{8}) {9}x{10}" -f ($i + 1), $mains[$i].PID, $mains[$i].RX, $mains[$i].RY, $mains[$i].RW, $mains[$i].RH, ($si + 1), $rects[$si].x, $rects[$si].y, $rects[$si].w, $rects[$si].h)
        $who = '整理#' + ($i + 1)
        if (Test-AtTarget $mains[$i] $rects[$si]) { Log ('[匹配] ' + $who + ' 已在目标位置,无需移动'); continue }
        $t = Apply-Rect $mains[$i].Handle $rects[$si] $vs ($who + ' ' + $mains[$i].Title)
        if ($t) { [void](Verify-Rect $mains[$i].Handle $t $who) }
    }
    Say ('已整理 {0} 个窗口' -f $mains.Count) 'Green'
    if ($Gui) { Show-Msg ('已整理 {0} 个窗口' -f $mains.Count) '整理窗口' }
}

# ---------------------------------------------------------------------------
#  模式: save / use(skill 位置记忆 + skill 位置摆放)
#  命名布局只存几何(virtualScreen/count/windows),存 mhxy-layouts\<名称>.json;
#  exePath 等其余配置仍在 mhxy-layout.json,与「记录当前布局/整理窗口」互不影响。
# ---------------------------------------------------------------------------
function Get-SavedLayoutNames {
    if (-not (Test-Path -LiteralPath $LayoutsDir)) { return @() }
    $items = @(Get-ChildItem -LiteralPath $LayoutsDir -Filter '*.json' -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)
    return @($items | ForEach-Object { $_.BaseName })
}
function Invoke-Save {
    $cfg = Get-Config
    $name = $LayoutName
    if (-not $name) {
        if ($Gui) { $name = Show-Input '输入位置记忆名称(如: 自己玩 / skill)' 'skill位置记忆' }
        else { $name = Read-Host '输入位置记忆名称(如: 自己玩 / skill)' }
        if ($null -eq $name) { Say '已取消' 'Yellow'; return }
        $name = ('' + $name).Trim()
    }
    if (-not $name) { Say '名称为空,已取消' 'Yellow'; return }
    $safe = ($name -replace '[\\/:*?"<>|]', '_').TrimEnd(' .')
    if ($safe -ne $name) { Say ('注意: 名称含文件名非法字符,已改为 "' + $safe + '"') 'Yellow'; $name = $safe }
    if (-not $name) { Say '名称为空,已取消' 'Yellow'; return }
    if (-not (Test-Path -LiteralPath $LayoutsDir)) { New-Item -ItemType Directory -Path $LayoutsDir -Force | Out-Null }
    $layPath = Join-Path $LayoutsDir ($name + '.json')
    if ((Test-Path -LiteralPath $layPath) -and -not $Force) {
        if ($Gui) { $overwrite = Confirm-YesNo ('位置记忆 "' + $name + '" 已存在,覆盖?') }
        else { $overwrite = ((Read-Host '位置记忆已存在,覆盖? [Y/N]') -match '^[yY]') }
        if (-not $overwrite) { Say '已取消' 'Yellow'; return }
    }
    Update-ProcMaps
    $wins = @(Get-MatchedWindows $cfg.matchProcesses)
    $mains = @(Get-MainWindows $wins $cfg.matchTitleKeywords | Sort-Object PID)
    if ($mains.Count -eq 0) {
        Say '没有找到梦幻西游主窗口! 请先打开窗口摆好位置,再运行"skill位置记忆"' 'Red'
        if ($Gui) { Show-Msg '没有找到梦幻西游主窗口! 请先打开窗口摆好位置' 'skill位置记忆' 'Error' }
        return
    }
    Say ('检测到 {0} 个实例,将保存为位置记忆 "{1}":' -f $mains.Count, $name) 'Cyan'
    $i = 0
    foreach ($m in $mains) { $i++; Say ('  [{0}] {1}' -f $i, (Format-Win $m)) }
    $minCount = @($mains | Where-Object { $_.Iconic }).Count
    if ($minCount -gt 0) { Say ('注意: {0} 个窗口处于最小化状态,将记录其还原位置' -f $minCount) 'Yellow' }
    $ans = 'y'
    if (-not $Force) {
        if ($Gui) {
            if (-not (Confirm-YesNo ('按以上 {0} 个窗口保存位置记忆 "{1}"?' -f $mains.Count, $name))) { $ans = 'n' }
        } else {
            $ans = Read-Host ('确认保存位置记忆 "{0}"? [Y/N] (回车=Y)' -f $name)
            if ($null -eq $ans) { $ans = '' }
            if ($ans -eq '') { $ans = 'y' }
        }
    }
    if ($ans -notmatch '^[yY]') { Say '已取消' 'Yellow'; return }
    $vs = Get-VirtualScreen
    $lay = [pscustomobject]@{
        name          = $name
        recordedAt    = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
        virtualScreen = [pscustomobject]@{ x = $vs.X; y = $vs.Y; w = $vs.Width; h = $vs.Height }
        count         = $mains.Count
        windows       = @()
    }
    foreach ($m in $mains) { $lay.windows += [pscustomobject]@{ x = $m.RX; y = $m.RY; w = $m.RW; h = $m.RH } }
    $lay | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $layPath -Encoding UTF8
    Say ('已保存位置记忆 "{0}"({1} 个窗口) -> {2}' -f $name, $lay.windows.Count, $layPath) 'Green'
    Log ('[位置记忆] ' + $name + ': ' + (($lay.windows | ForEach-Object { ('({0},{1}) {2}x{3}' -f $_.x, $_.y, $_.w, $_.h) }) -join '; '))
    if ($Gui) { Show-Msg ('已保存位置记忆 "{0}"({1} 个窗口)' -f $name, $lay.windows.Count) 'skill位置记忆' }
}
function Invoke-Use {
    $names = @(Get-SavedLayoutNames)
    if ($names.Count -eq 0) {
        Say '还没有保存过任何位置记忆! 请先用"skill位置记忆"保存(如: 自己玩 / skill)' 'Red'
        if ($Gui) { Show-Msg '还没有保存过任何位置记忆! 请先运行"skill位置记忆"' 'skill位置摆放' 'Error' }
        return
    }
    $name = $LayoutName
    if (-not $name) {
        $hint = '切换到哪个位置记忆?(输入名称或序号)  已保存: ' + ($names -join '、')
        if ($Gui) { $picked = Show-Input $hint 'skill位置摆放' $names[0] }
        else { $picked = Read-Host $hint }
        if ($null -eq $picked) { Say '已取消' 'Yellow'; return }
        $picked = ('' + $picked).Trim()
        if ($picked -match '^\d+$' -and [int]$picked -ge 1 -and [int]$picked -le $names.Count) { $name = $names[[int]$picked - 1] }
        else { $name = $picked }
    }
    if ($names -notcontains $name) {
        Say ('没有找到位置记忆 "' + $name + '"! 已保存: ' + ($names -join '、')) 'Red'
        if ($Gui) { Show-Msg ('没有找到位置记忆 "' + $name + '"!' + [Environment]::NewLine + '已保存: ' + ($names -join '、')) 'skill位置摆放' 'Error' }
        return
    }
    $layPath = Join-Path $LayoutsDir ($name + '.json')
    $lay = Get-Content -LiteralPath $layPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $cfg = Get-Config
    # 只取命名布局的几何;scaleOnDifferentScreen 等行为仍取主配置 mhxy-layout.json
    $cfg.virtualScreen = $lay.virtualScreen
    $cfg.windows = @($lay.windows)
    $cfg.count = [int]$lay.count
    $saved = @($cfg.windows)
    Update-ProcMaps
    $wins = @(Get-MatchedWindows $cfg.matchProcesses)
    $mains = @(Get-MainWindows $wins $cfg.matchTitleKeywords | Sort-Object PID)
    if ($mains.Count -eq 0) {
        Say '没有找到梦幻西游主窗口' 'Red'
        if ($Gui) { Show-Msg '没有找到梦幻西游主窗口' 'skill位置摆放' 'Error' }
        return
    }
    $vs = Get-VirtualScreen
    $rects = Build-Rects $cfg ([Math]::Max($mains.Count, $saved.Count)) $vs
    $perm = Resolve-SlotMatch $mains $rects
    Say ('按位置记忆 "{0}" 摆放 {1} 个主窗口(按当前位置就近匹配):' -f $name, $mains.Count) 'Cyan'
    for ($i = 0; $i -lt $mains.Count; $i++) {
        $si = $perm[$i]
        Log ("[位置摆放:{0}] 窗口#{1} PID={2} 当前=({3},{4}) {5}x{6} -> 槽位{7} 目标=({8},{9}) {10}x{11}" -f $name, ($i + 1), $mains[$i].PID, $mains[$i].RX, $mains[$i].RY, $mains[$i].RW, $mains[$i].RH, ($si + 1), $rects[$si].x, $rects[$si].y, $rects[$si].w, $rects[$si].h)
        $who = '摆放' + $name + '#' + ($i + 1)
        if (Test-AtTarget $mains[$i] $rects[$si]) { Log ('[位置摆放:' + $name + '] ' + $who + ' 已在目标位置,无需移动'); continue }
        $t = Apply-Rect $mains[$i].Handle $rects[$si] $vs ($who + ' ' + $mains[$i].Title)
        if ($t) { [void](Verify-Rect $mains[$i].Handle $t $who) }
    }
    Say ('已按位置记忆 "{0}" 整理 {1} 个窗口' -f $name, $mains.Count) 'Green'
    if ($Gui) { Show-Msg ('已按位置记忆 "{0}" 整理 {1} 个窗口' -f $name, $mains.Count) 'skill位置摆放' }
}

# ---------------------------------------------------------------------------
#  模式: launch(v2 核心:进程血缘绑定)
# ---------------------------------------------------------------------------
function Invoke-Launch {
    $cfg = Get-Config
    $saved = @($cfg.windows)
    if ($saved.Count -eq 0) {
        Say '还没有保存过布局! 请先手动打开窗口,再运行"记录当前布局"' 'Red'
        return
    }
    $N = if ($Count -gt 0) { $Count } elseif ($cfg.count -gt 0) { [int]$cfg.count } else { $saved.Count }
    if ($N -le 0) { Say '目标窗口数无效' 'Red'; return }
    $exe = Resolve-Exe $cfg
    if ($ExePath -and $cfg.exePath -ne $exe) {
        $cfg.exePath = $exe; Save-Config $cfg
        Say ('已记住客户端路径: ' + $exe) 'Green'
    }
    $workDir = Split-Path -Parent $exe
    $vs = Get-VirtualScreen
    $rects = Build-Rects $cfg $N $vs
    Say ('客户端: ' + $exe) 'Gray'
    Say ('目标窗口数: ' + $N) 'Cyan'

    Update-ProcMaps
    $wins = @(Get-MatchedWindows $cfg.matchProcesses)
    $mains = @(Get-MainWindows $wins $cfg.matchTitleKeywords)
    # 已有实例数 = 有可见窗口的不同进程数(登录/游戏阶段都算)
    $procSet = @{}
    foreach ($w in $wins) { $procSet[$w.PID] = $true }
    $E = $procSet.Count
    if ($E -gt 0) {
        Say ("检测到已有 {0} 个梦幻西游进程(窗口)在运行" -f $E) 'Cyan'
        foreach ($w in $wins) { Log ('已有窗口: ' + (Format-Win $w) + ' 血缘: ' + (Get-ParentChain $w.PID $null)) }
    }
    if ($E -gt $N) { Say ("注意: 当前进程数({0})超过目标({1}),不启动新窗口,只整理前 {1} 个" -f $E, $N) 'Yellow' }

    # ---- 槽位 ----
    $slots = @()
    for ($i = 0; $i -lt $N; $i++) {
        $slots += [pscustomobject]@{
            Index = $i; LaunchedPid = 0; Handle = [IntPtr]::Zero; PID = 0
            Proc = ''; Title = ''; Assigned = $false
        }
    }
    $slotPids = @{}   # pid -> slot 索引(已有进程 + 已启动的启动器)

    # 已有窗口按当前位置就近匹配槽位(不按 PID 顺序,避免洗牌)
    $take = [Math]::Min($E, $N)
    $existingMains = @($mains | Sort-Object PID)
    if ($take -gt 0) {
        if ($E -gt $N) { $existingMains = @($existingMains | Select-Object -First $N) }
        $perm = Resolve-SlotMatch $existingMains $rects
        for ($i = 0; $i -lt $take; $i++) {
            $slotIdx = $perm[$i]
            Say ("[槽位{0}] 已有主窗口: {1}" -f ($slotIdx + 1), (Format-Win $existingMains[$i]))
            Bind-SlotMain $slots[$slotIdx] $existingMains[$i] $slotPids $rects $vs '-已有'
        }
    }

    # ---- 增量启动 + 血缘绑定(新实例填剩余槽位) ----
    $taken = @{}
    foreach ($v in $slotPids.Values) { $taken[$v] = $true }
    $freeSlots = @()
    for ($i = 0; $i -lt $N; $i++) { if (-not $taken.ContainsKey($i)) { $freeSlots += $i } }
    $toLaunch = $N - $take
    if ($toLaunch -gt 0) {
        Say ("需要启动 {0} 个新实例" -f $toLaunch) 'Cyan'
        for ($k = 0; $k -lt $toLaunch; $k++) {
            $slotIdx = $freeSlots[$k]
            Say ("启动第 {0}/{1} 个实例(槽位{2})..." -f ($k + 1), $toLaunch, ($slotIdx + 1)) 'Cyan'
            try {
                $proc = Start-Process -FilePath $exe -WorkingDirectory $workDir -PassThru
                $slots[$slotIdx].LaunchedPid = $proc.Id
                $slotPids[$proc.Id] = $slotIdx
                Log ("[LAUNCH] 槽位{0} 启动器 PID={1}" -f ($slotIdx + 1), $proc.Id)
            } catch {
                Say ("启动失败: {0}" -f $_.Exception.Message) 'Red'
            }
            # 等待:找到「血缘属于本槽位」的主窗口
            $deadline = (Get-Date).AddSeconds([double]$cfg.waitTimeoutSeconds)
            $assigned = $false
            while ((Get-Date) -lt $deadline) {
                Start-Sleep -Milliseconds 500
                Update-ProcMaps
                $cur = Get-MatchedWindows $cfg.matchProcesses
                foreach ($m in (Get-MainWindows $cur $cfg.matchTitleKeywords)) {
                    if ((Find-AncestorSlot $m.PID $slotPids) -eq $slotIdx) {
                        Say ("[槽位{0}] 主窗口已出现(血缘绑定): {1}" -f ($slotIdx + 1), (Format-Win $m))
                        Log ('      血缘: ' + (Get-ParentChain $m.PID $slotPids))
                        Bind-SlotMain $slots[$slotIdx] $m $slotPids $rects $vs
                        $assigned = $true
                        break
                    }
                }
                if ($assigned) { break }
            }
            if (-not $assigned) {
                Say ("警告: 槽位{0} 等待窗口超时({1}s),进入观察阶段按血缘自动补调" -f ($slotIdx + 1), $cfg.waitTimeoutSeconds) 'Yellow'
            }
            if ($k -lt $toLaunch - 1) { Start-Sleep -Seconds ([double]$cfg.launchDelaySeconds) }
        }
    } else {
        Say '当前窗口数已满足目标,无需启动新窗口' 'Green'
    }

    # ---- 观察/补调阶段:按血缘持续绑定主窗口,句柄变化时重新摆放 ----
    Say '观察窗口稳定并自动补调(登录窗口切换等变化会自动重新摆放)...' 'Cyan'
    $sweepDeadline = (Get-Date).AddSeconds([double]$cfg.sweepSeconds)
    $lastChange = Get-Date
    $lastStatus = Get-Date
    while ((Get-Date) -lt $sweepDeadline) {
        Start-Sleep -Milliseconds 1000
        Update-ProcMaps
        $cur = Get-MatchedWindows $cfg.matchProcesses
        $mains = Get-MainWindows $cur $cfg.matchTitleKeywords
        $allOk = $true
        $assignedHandles = @{}
        for ($i = 0; $i -lt $N; $i++) {
            $s = $slots[$i]
            # 找本槽位的主窗口:血缘命中(已启动按启动器链;已有进程按进程PID)
            $mine = $null
            foreach ($m in $mains) {
                if ((Find-AncestorSlot $m.PID $slotPids) -eq $i) { $mine = $m; break }
            }
            if ($mine) {
                $assignedHandles[[string]$mine.Handle] = $true
                if (-not $s.Assigned -or $mine.Handle -ne $s.Handle) {
                    Say ("[补调] 槽位{0} 主窗口{1}: {2}" -f ($i + 1), $(if ($s.Handle -ne [IntPtr]::Zero) { '变化' } else { '出现' }), (Format-Win $mine))
                    Log ('      血缘: ' + (Get-ParentChain $mine.PID $slotPids))
                    Bind-SlotMain $s $mine $slotPids $rects $vs '-补调'
                    $lastChange = Get-Date
                }
            } else {
                $allOk = $false
                if ($s.Assigned -and $s.Handle -ne [IntPtr]::Zero) {
                    Log ("[观察] 槽位{0} 主窗口暂时消失(等待重新出现)" -f ($i + 1))
                    $s.Handle = [IntPtr]::Zero
                    $lastChange = Get-Date
                }
            }
        }
        # 孤儿主窗口(血缘不属任何槽位)→ 分配给最早未分配的槽位(带警告)
        $orphans = @($mains | Where-Object { -not $assignedHandles.ContainsKey([string]$_.Handle) })
        foreach ($o in $orphans) {
            if ((Find-AncestorSlot $o.PID $slotPids) -ne -1) { continue }
            for ($i = 0; $i -lt $N; $i++) {
                if (-not $slots[$i].Assigned) {
                    Say ("[补调-孤儿] 槽位{0} 绑定无血缘主窗口: {1}(注意:血缘未命中)" -f ($i + 1), (Format-Win $o)) 'Yellow'
                    Bind-SlotMain $slots[$i] $o $slotPids $rects $vs '-孤儿'
                    $lastChange = Get-Date
                    break
                }
            }
        }
        # 状态行(每30秒)
        if (((Get-Date) - $lastStatus).TotalSeconds -ge 30) {
            $parts = @()
            for ($i = 0; $i -lt $N; $i++) {
                $s = $slots[$i]
                $st = if ($s.Assigned -and $s.Handle -ne [IntPtr]::Zero) { $s.Title } else { '等待中' }
                $parts += ('槽' + ($i + 1) + '=' + $st)
            }
            Log ('[STATUS] ' + ($parts -join '  '))
            $lastStatus = Get-Date
        }
        if ($allOk -and ((Get-Date) - $lastChange).TotalSeconds -gt 30) {
            Log '所有槽位主窗口就位且已稳定30秒,提前结束观察'
            break
        }
    }

    # ---- 最终汇总(回读实际位置,与目标比对判定就位) ----
    Say ('==== 汇总: 目标 {0} 个窗口 ====' -f $N) 'Cyan'
    $okCount = 0
    for ($i = 0; $i -lt $N; $i++) {
        $s = $slots[$i]
        if ($s.Assigned -and $s.Handle -ne [IntPtr]::Zero -and [MHWin32]::IsWindow($s.Handle)) {
            # 最小化窗口按还原矩形回读
            $r = New-Object MHWin32+RECT
            $pl = New-Object MHWin32+WINDOWPLACEMENT
            $pl.length = [Runtime.InteropServices.Marshal]::SizeOf([type][MHWin32+WINDOWPLACEMENT])
            if ([MHWin32]::IsIconic($s.Handle) -and [MHWin32]::GetWindowPlacement($s.Handle, [ref]$pl)) {
                $r = $pl.rcNormalPosition
            } else {
                [void][MHWin32]::GetWindowRect($s.Handle, [ref]$r)
            }
            $ax = $r.Left; $ay = $r.Top; $aw = $r.Right - $r.Left; $ah = $r.Bottom - $r.Top
            $dx = [Math]::Abs($ax - $rects[$i].x); $dy = [Math]::Abs($ay - $rects[$i].y)
            $dw = [Math]::Abs($aw - $rects[$i].w); $dh = [Math]::Abs($ah - $rects[$i].h)
            if ($dx -le 25 -and $dy -le 25 -and $dw -le 25 -and $dh -le 25) {
                $okCount++
                Say ('  [{0}/{1}] 就位  {2}  ({3},{4}) {5}x{6}' -f ($i + 1), $N, $s.Title, $ax, $ay, $aw, $ah)
            } else {
                Say ('  [{0}/{1}] 位置偏差  {2}  实际=({3},{4}) {5}x{6}  目标=({7},{8}) {9}x{10}' -f ($i + 1), $N, $s.Title, $ax, $ay, $aw, $ah, $rects[$i].x, $rects[$i].y, $rects[$i].w, $rects[$i].h) 'Yellow'
            }
        } else {
            Say ('  [{0}/{1}] 未就位  {2}' -f ($i + 1), $N, $s.Title)
        }
    }
    if ($okCount -lt $N) {
        Say ('有 {0} 个窗口未在目标位置。可等登录完成后重新运行"整理窗口",或手动摆好后运行"记录当前布局"更新布局' -f ($N - $okCount)) 'Yellow'
    } else {
        Say '全部窗口已就位!' 'Green'
    }
    if ($Gui) {
        if ($okCount -lt $N) { Show-Msg ('有 {0} 个窗口未在目标位置(详见 mhxy-launcher.log)' -f ($N - $okCount)) '一键五开' 'Warn' } else { Show-Msg '全部窗口已就位!' '一键五开' }
    }
    # 前台激活最后一个
    $last = $null
    for ($i = $N - 1; $i -ge 0; $i--) {
        if ($slots[$i].Assigned -and $slots[$i].Handle -ne [IntPtr]::Zero -and [MHWin32]::IsWindow($slots[$i].Handle)) { $last = $slots[$i].Handle; break }
    }
    if ($last -and $last -ne [IntPtr]::Zero) { [void][MHWin32]::SetForegroundWindow($last) }
}

# ---------------------------------------------------------------------------
#  入口
# ---------------------------------------------------------------------------
Log-Sep
Say '==================================================' 'Cyan'
Say '  梦幻西游手游 PC 版 五开助手 v2.3(多屏适配)' 'Cyan'
Say '==================================================' 'Cyan'
Log ('脚本目录: ' + $ScriptDir)
Log ('操作系统: ' + [System.Environment]::OSVersion.VersionString)

try {
    switch ($Mode) {
        'list'    { Invoke-List }
        'record'  { Invoke-Record }
        'arrange' { Invoke-Arrange }
        'save'    { Invoke-Save }
        'use'     { Invoke-Use }
        'launch'  { Invoke-Launch }
        'setpath' { Invoke-SetPath }
        'selftest' { Invoke-SelfTest }
    }
} catch {
    Log ('[致命错误] ' + $_.Exception.Message)
    Log ($_.ScriptStackTrace)
    if ($Gui) { Show-Msg ('发生错误: ' + $_.Exception.Message) '五开助手' 'Error' } else { Write-Host ('发生错误: ' + $_.Exception.Message) -ForegroundColor Red }
}
Say '完成' 'Gray'
try { if ($script:SW) { $script:SW.Flush(); $script:SW.Close() } } catch { }
