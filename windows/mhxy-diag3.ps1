#  mhxy-diag3.ps1 - window diagnosis v3 (ASCII-only source on purpose)
#  Why v3: v1/v2 on the target host wrote only the header then died silently
#  (hidden console). v3 strategy:
#    - ASCII-only script body: no encoding/BOM variables; Chinese strings are
#      read at runtime from mhxy-layout.json / window titles, never hardcoded
#    - Start-Transcript captures the hidden console's error stream
#    - stage markers flushed via [IO.File]::AppendAllText after every step,
#      so the log shows exactly which statement killed the run
#    - Stage 1 (Get-Process + Win32_Process command lines) runs FIRST: even if
#      everything after dies, the most valuable data is already on disk
#  Usage: double-click the .vbs launcher. Output: diag3.log + diag3-transcript.txt
param([switch]$Visible)
#  Hidden mode uses SilentlyContinue: a hidden console can HANG while printing an
#  error record (known trap, see mhxy-launcher.ps1). Visible mode keeps errors on screen.
$ErrorActionPreference = if ($Visible) { 'Continue' } else { 'SilentlyContinue' }
$dir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$log  = Join-Path $dir 'diag3.log'
$tr   = Join-Path $dir 'diag3-transcript.txt'
$cfgp = Join-Path $dir 'mhxy-layout.json'
$enc  = New-Object System.Text.UTF8Encoding($false)

function Mark {
    param([string]$s)
    try { [System.IO.File]::AppendAllText($log, (('[{0:HH:mm:ss.fff}] {1}' -f (Get-Date), $s) + "`r`n"), $enc) } catch { }
}

Mark 'START v3'
try { Start-Transcript -Path $tr -Force | Out-Null; Mark 'transcript on' } catch { Mark ('transcript failed: ' + $_.Exception.Message) }
Mark ('PS=' + $PSVersionTable.PSVersion + ' user=' + [Environment]::UserName + ' session=' + [System.Diagnostics.Process]::GetCurrentProcess().SessionId)
$elev = $false
try { $elev = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) } catch { }
Mark ('elevated=' + $elev)

# ---------------------------------------------------------------
#  Stage 1: process-level truth (independent of user32)
# ---------------------------------------------------------------
Mark 'STAGE1 begin'
try {
    $gp = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 })
    Mark ('STAGE1 processes with a main window: ' + $gp.Count)
    foreach ($p in $gp) { Mark ('  GP PID=' + $p.Id + ' name=' + $p.ProcessName + ' title=' + $p.MainWindowTitle) }
} catch { Mark ('STAGE1 getprocess err: ' + $_.Exception.Message) }
try {
    $wpr = Get-CimInstance Win32_Process -Filter "Name LIKE 'My%' OR Name LIKE '%TabCtrl%'" -ErrorAction SilentlyContinue
    Mark ('STAGE1 My*/TabCtrl processes: ' + @($wpr).Count)
    foreach ($w in $wpr) {
        Mark ('  WP PID=' + $w.ProcessId + ' name=' + $w.Name + ' parent=' + $w.ParentProcessId + ' cmdline=' + $w.CommandLine)
    }
} catch { Mark ('STAGE1 cim err: ' + $_.Exception.Message) }
Mark 'STAGE1 done'

# ---------------------------------------------------------------
#  Stage 2: config (keywords come from json, not from this file)
# ---------------------------------------------------------------
Mark 'STAGE2 begin'
$matchProcesses = @('MyGame_x64r', 'MyLauncher_x64r', 'MyPCLauncher_x64r', 'MyPreloader_x64r', 'MyTabCtrl_x64r')
$matchKeywords  = @()
if (Test-Path -LiteralPath $cfgp) {
    try {
        $c = Get-Content -LiteralPath $cfgp -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($c.matchProcesses) { $matchProcesses = @($c.matchProcesses) }
        if ($c.matchTitleKeywords) { $matchKeywords = @($c.matchTitleKeywords) }
    } catch { Mark ('STAGE2 config err: ' + $_.Exception.Message) }
} else { Mark 'STAGE2 no mhxy-layout.json, using built-in defaults' }
Mark ('STAGE2 matchProcesses=' + ($matchProcesses -join ','))
Mark ('STAGE2 keywords from json: ' + $matchKeywords.Count)
Mark 'STAGE2 done'

# ---------------------------------------------------------------
#  Stage 3: user32 enumeration, all inside C#
# ---------------------------------------------------------------
Mark 'STAGE3 add-type begin'
try { Add-Type -AssemblyName System.Windows.Forms } catch { Mark ('STAGE3 forms err: ' + $_.Exception.Message) }
$atErr = $null
try {
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;
using System.Collections.Generic;
public class DiagW3 {
    public delegate bool P(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] public static extern bool EnumWindows(P f, IntPtr p);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] public static extern bool GetWindowPlacement(IntPtr h, ref WP wp);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L; public int T; public int R; public int B; }
    [StructLayout(LayoutKind.Sequential)] public struct WP {
        public int len; public int flags; public int showCmd;
        public System.Drawing.Point min; public System.Drawing.Point max; public RECT rc;
    }
    public static List<string> Dump() {
        List<string> o = new List<string>();
        EnumWindows(delegate(IntPtr h, IntPtr l) {
            try {
                uint pid; GetWindowThreadProcessId(h, out pid);
                StringBuilder t = new StringBuilder(256); GetWindowText(h, t, 256);
                StringBuilder c = new StringBuilder(256); GetClassName(h, c, 256);
                WP wp = new WP(); wp.len = Marshal.SizeOf(typeof(WP));
                GetWindowPlacement(h, ref wp);
                o.Add(pid + "\t" + (IsWindowVisible(h) ? 1 : 0) + "\t" + (IsIconic(h) ? 1 : 0)
                    + "\t" + (wp.rc.R - wp.rc.L) + "\t" + (wp.rc.B - wp.rc.T)
                    + "\t" + c.ToString() + "\t" + t.ToString());
            } catch { }
            return true;
        }, IntPtr.Zero);
        return o;
    }
}
"@ -ReferencedAssemblies System.Drawing
} catch { $atErr = $_.Exception.Message }
if ($atErr) { Mark ('STAGE3 add-type FAILED: ' + $atErr) } else { Mark 'STAGE3 add-type ok' }

$raw = @()
if (-not $atErr) {
    Mark 'STAGE3 enum begin'
    try { $raw = @([DiagW3]::Dump()) } catch { Mark ('STAGE3 enum FAILED: ' + $_.Exception.Message) }
    Mark ('STAGE3 enum done total=' + $raw.Count)
}

# ---------------------------------------------------------------
#  Stage 4: parse + reproduce the launcher's three gates
# ---------------------------------------------------------------
Mark 'STAGE4 begin'
$procName = @{}
foreach ($p in Get-Process -ErrorAction SilentlyContinue) {
    if (-not $procName.ContainsKey($p.Id)) { $procName[$p.Id] = $p.ProcessName }
}
$wins = @()
foreach ($line in $raw) {
    $f = $line -split "`t", 7
    if ($f.Count -lt 7) { continue }
    if ($f[1] -ne '1') { continue }
    $nm = ''
    if ($procName.ContainsKey([int]$f[0])) { $nm = $procName[[int]$f[0]] }
    $wins += [pscustomobject]@{ PID = [int]$f[0]; Proc = $nm; Title = $f[6]; Class = $f[5]; RW = [int]$f[3]; RH = [int]$f[4]; Iconic = ($f[2] -eq '1') }
}
Mark ('STAGE4 visible windows: ' + $wins.Count + ' (invisible skipped: ' + ($raw.Count - $wins.Count) + ')')

$dlgPattern = 'MPAY|DuiShadowWnd|WebView2|GDI\+|IME|MSCTF'
foreach ($w in $wins) {
    $procHit = ($matchProcesses -contains $w.Proc)
    $susp    = ($w.Proc -match 'My|TabCtrl|Launcher|Preloader')
    $kwHit   = $false
    foreach ($k in $matchKeywords) { if ($k -and $w.Title.IndexOf($k, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) { $kwHit = $true; break } }
    $dlg = $false; $why = ''
    if ($w.RW -lt 120 -or $w.RH -lt 80) { $dlg = $true; $why = 'too small' }
    elseif ($w.Class -match $dlgPattern) { $dlg = $true; $why = 'dialog-class' }
    elseif ($w.Title -match '^MPAY_') { $dlg = $true; $why = 'MPAY_ prefix' }
    $w | Add-Member -NotePropertyName ProcHit -NotePropertyValue $procHit -Force
    $w | Add-Member -NotePropertyName KwHit -NotePropertyValue $kwHit -Force
    $w | Add-Member -NotePropertyName Dlg -NotePropertyValue $dlg -Force
    $w | Add-Member -NotePropertyName DlgWhy -NotePropertyValue $why -Force
    $w | Add-Member -NotePropertyName Susp -NotePropertyValue $susp -Force
}
foreach ($w in $wins) {
    $marks = @()
    if ($w.ProcHit) { $marks += 'proc-hit' }
    if ($w.KwHit) { $marks += 'title-hit' }
    if ($w.Dlg) { $marks += ('dlg-blocked:' + $w.DlgWhy) }
    if ($w.Iconic) { $marks += 'minimized' }
    $m = if ($marks.Count) { (' <' + ($marks -join ',')) + '>' } else { '' }
    Mark ('PID=' + $w.PID + '(' + $w.Proc + ') class=[' + $w.Class + '] title=' + $w.Title + ' restored=' + $w.RW + 'x' + $w.RH + $m)
}
$kwWins   = @($wins | Where-Object { $_.KwHit })
$procWins = @($wins | Where-Object { $_.ProcHit })
$passAll  = @($wins | Where-Object { $_.ProcHit -and -not $_.Dlg })
$suspWins = @($wins | Where-Object { $_.Susp })
Mark ('STAGE4 verdict: visible=' + $wins.Count + ' title-hit=' + $kwWins.Count + ' proc-hit=' + $procWins.Count + ' pass-all=' + $passAll.Count + ' game-suspect=' + $suspWins.Count)
if ($wins.Count -eq 0) { Mark 'STAGE4 ANOMALY: zero visible windows, see transcript; check STAGE1 GP lines for the truth' }
if ($suspWins.Count -gt 0) {
    Mark 'STAGE4 game-suspect windows (proc name looks game-related regardless of config):'
    foreach ($w in $suspWins) { Mark ('  SUSPECT PID=' + $w.PID + '(' + $w.Proc + ') class=[' + $w.Class + '] title=' + $w.Title) }
}
Mark 'STAGE4 done'
Mark 'DONE v3'
try { Stop-Transcript | Out-Null } catch { }

$summary = 'visible=' + $wins.Count + ' proc-hit=' + $procWins.Count + ' pass-all=' + $passAll.Count + ' game-suspect=' + $suspWins.Count + "`r`n" +
    'full data in diag3.log / diag3-transcript.txt'
try {
    [System.Windows.Forms.MessageBox]::Show($summary, 'diag3', [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null
} catch { Mark (' messagebox failed: ' + $_.Exception.Message) }
