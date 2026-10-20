# Log Grabber collector - merged offline/live Windows diagnostic collector.
# Facts only -> diagnostics.json + copied evtx/hives/dumps for AI interpretation.
#
#   powershell -ExecutionPolicy Bypass -File .\collector.ps1 -WindowsRoot D:
#   powershell -ExecutionPolicy Bypass -File .\collector.ps1 -WindowsRoot C: -NoUi
#
# Offline volume (e.g. D: from a bench OS): logs/hives/dumps are copied directly.
# Live volume ($win == SystemDrive): logs are exported with wevtutil (files are
# locked), registry is read from mounted hives, hives saved with reg save.
param(
    [string]$WindowsRoot = '',
    [string]$OutDir = '',
    [switch]$NoUi
)

$ErrorActionPreference = 'Continue'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not $WindowsRoot) {
    if (Test-Path 'D:\Windows\System32\winevt\Logs\System.evtx') { $WindowsRoot = 'D:' }
    elseif (Test-Path 'C:\Windows\System32\winevt\Logs\System.evtx') { $WindowsRoot = 'C:' }
    else { throw 'No Windows volume found. Pass -WindowsRoot D:' }
}
$win = $WindowsRoot.TrimEnd('\')
$isLive = ($win -ieq $env:SystemDrive.TrimEnd('\'))

if (-not $OutDir) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $OutDir = Join-Path $Here "cases\$stamp"
}

$bootLog = Join-Path $Here 'cases\last-run.log'
New-Item -ItemType Directory -Force -Path (Join-Path $Here 'cases') | Out-Null
"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') start win=$win live=$isLive out=$OutDir admin check..." | Out-File $bootLog -Encoding utf8

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    "not admin - launching elevated cmd /k" | Out-File $bootLog -Append -Encoding utf8
    $uiArg = $(if ($NoUi) { ' -NoUi' } else { '' })
    $inner = "title Log Grabber collect & cd /d `"$Here`" & powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -WindowsRoot `"$win`" -OutDir `"$OutDir`"$uiArg & echo. & pause"
    Start-Process -FilePath "$env:SystemRoot\System32\cmd.exe" -Verb RunAs -WindowStyle Normal -ArgumentList @('/k', $inner)
    Write-Host "UAC sent. After Yes, an Administrator window will run the collector."
    Write-Host "Output: $OutDir"
    exit 0
}
"admin=yes pid=$PID" | Out-File $bootLog -Append -Encoding utf8

try {
    Add-Type -Name Win -Namespace Native -MemberDefinition @"
        [System.Runtime.InteropServices.DllImport("kernel32.dll")]
        public static extern System.IntPtr GetConsoleWindow();
        [System.Runtime.InteropServices.DllImport("user32.dll")]
        public static extern bool ShowWindow(System.IntPtr hWnd, int nCmdShow);
        [System.Runtime.InteropServices.DllImport("user32.dll")]
        public static extern bool SetForegroundWindow(System.IntPtr hWnd);
"@
    # -ErrorAction cannot trail a here-string closer; closer must be at column 0.
    $hwnd = [Native.Win]::GetConsoleWindow()
    if ($hwnd -ne [IntPtr]::Zero) {
        [Native.Win]::ShowWindow($hwnd, 5) | Out-Null
        [Native.Win]::SetForegroundWindow($hwnd) | Out-Null
    }
} catch { }

function Out-Log([string]$Msg) {
    $line = '[{0:HH:mm:ss}] {1}' -f (Get-Date), $Msg
    Write-Host $line
    $logFile = Join-Path $OutDir 'collect.log'
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

function Truncate([string]$s, [int]$n = 400) {
    if (-not $s) { return '' }
    $t = (($s -replace '[\r\n]+', ' ') -replace '\s+', ' ').Trim()
    if ($t.Length -gt $n) { return $t.Substring(0, $n) }
    return $t
}

function Event-DataMap($Event) {
    $map = [ordered]@{}
    try {
        $xml = [xml]$Event.ToXml()
        foreach ($d in @($xml.Event.EventData.Data)) {
            if ($d.Name) { $map[$d.Name] = [string]$d.'#text' }
        }
    } catch { }
    return $map
}

function Collect-Events {
    param($Path, $Filter, $Max = 40)
    $out = @{ samples = @(); returned = 0; capped_at = $Max; truncated = $false; error = $null }
    if (-not (Test-Path $Path)) { $out.error = 'missing'; return $out }
    try {
        $ev = @(Get-WinEvent -Path $Path -FilterXPath $Filter -MaxEvents $Max -ErrorAction Stop)
        $out.returned = $ev.Count
        $out.truncated = ($ev.Count -ge $Max)
        foreach ($e in $ev) {
            $data = Event-DataMap $e
            $out.samples += [ordered]@{
                time     = $e.TimeCreated.ToString('yyyy-MM-ddTHH:mm:ss')
                id       = $e.Id
                level    = $e.Level
                provider = [string]$e.ProviderName
                message  = Truncate $e.Message
                data     = $data
            }
        }
    } catch {
        $out.error = $_.Exception.Message
    }
    return $out
}

$script:ProgressForm = $null
$script:ProgressBar = $null
$script:ProgressLabel = $null
$script:ProgressLog = $null
$script:ProgressTotal = 14

function Write-ProgressFile([int]$Step, [string]$Text, [string]$Status = 'running') {
    $p = Join-Path $OutDir 'progress.txt'
    @(
        "status=$Status"
        "step=$Step"
        "total=$script:ProgressTotal"
        "text=$Text"
        "utc=$((Get-Date).ToString('yyyy-MM-ddTHH:mm:ss'))"
        "out=$OutDir"
    ) | Set-Content -Path $p -Encoding ASCII
}

function Pump-Ui {
    try { [System.Windows.Forms.Application]::DoEvents() } catch { }
}

function Init-ProgressUi {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $form = New-Object System.Windows.Forms.Form
    $form.Text = 'Log Grabber - collecting diagnostics'
    $form.Size = New-Object System.Drawing.Size(560, 340)
    $form.StartPosition = 'CenterScreen'
    $form.TopMost = $true
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $form.BackColor = [System.Drawing.Color]::FromArgb(16, 42, 67)
    $form.ForeColor = [System.Drawing.Color]::White

    $title = New-Object System.Windows.Forms.Label
    $title.Text = 'Collecting Windows logs'
    $title.Font = New-Object System.Drawing.Font('Segoe UI', 12, [System.Drawing.FontStyle]::Bold)
    $title.AutoSize = $true
    $title.Location = New-Object System.Drawing.Point(16, 14)
    $form.Controls.Add($title)

    $lbl = New-Object System.Windows.Forms.Label
    $lbl.Text = 'Starting...'
    $lbl.Font = New-Object System.Drawing.Font('Segoe UI', 9)
    $lbl.Size = New-Object System.Drawing.Size(520, 22)
    $lbl.Location = New-Object System.Drawing.Point(16, 48)
    $form.Controls.Add($lbl)

    $bar = New-Object System.Windows.Forms.ProgressBar
    $bar.Location = New-Object System.Drawing.Point(16, 76)
    $bar.Size = New-Object System.Drawing.Size(512, 22)
    $bar.Minimum = 0
    $bar.Maximum = $script:ProgressTotal
    $bar.Value = 0
    $bar.Style = 'Continuous'
    $form.Controls.Add($bar)

    $log = New-Object System.Windows.Forms.ListBox
    $log.Location = New-Object System.Drawing.Point(16, 110)
    $log.Size = New-Object System.Drawing.Size(512, 170)
    $log.Font = New-Object System.Drawing.Font('Consolas', 8)
    $log.BackColor = [System.Drawing.Color]::FromArgb(36, 59, 83)
    $log.ForeColor = [System.Drawing.Color]::White
    $form.Controls.Add($log)

    $script:ProgressForm = $form
    $script:ProgressBar = $bar
    $script:ProgressLabel = $lbl
    $script:ProgressLog = $log
    $form.Show()
    $form.Activate()
    Pump-Ui
}

function Set-Progress([int]$Step, [string]$Text) {
    Write-Progress -Activity 'Log Grabber collect' -Status $Text -PercentComplete ([int](100 * $Step / [Math]::Max($script:ProgressTotal, 1)))
    Write-ProgressFile $Step $Text 'running'
    Out-Log $Text
    if ($script:ProgressForm) {
        $script:ProgressBar.Value = [Math]::Min($Step, $script:ProgressBar.Maximum)
        $script:ProgressLabel.Text = "{0}/{1}  {2}" -f $Step, $script:ProgressTotal, $Text
        $script:ProgressLog.Items.Add(('[{0:HH:mm:ss}] {1}' -f (Get-Date), $Text)) | Out-Null
        $script:ProgressLog.TopIndex = $script:ProgressLog.Items.Count - 1
        Pump-Ui
    }
}

function Finish-ProgressUi([string]$Text, [bool]$Ok = $true) {
    Write-ProgressFile $script:ProgressTotal $Text $(if ($Ok) { 'done' } else { 'failed' })
    if ($script:ProgressForm) {
        $script:ProgressBar.Value = $script:ProgressBar.Maximum
        $script:ProgressLabel.Text = $Text
        $script:ProgressForm.Text = $(if ($Ok) { 'Log Grabber - finished' } else { 'Log Grabber - FAILED' })
        $btn = New-Object System.Windows.Forms.Button
        $btn.Text = 'Close'
        $btn.Size = New-Object System.Drawing.Size(90, 28)
        $btn.Location = New-Object System.Drawing.Point(438, 80)
        $btn.Add_Click({ $script:ProgressForm.Close() })
        $script:ProgressForm.Controls.Add($btn)
        Pump-Ui
        while ($script:ProgressForm.Visible) {
            Start-Sleep -Milliseconds 80
            Pump-Ui
        }
    }
}

try {

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
if (-not $NoUi) {
    try { Init-ProgressUi } catch { Out-Log "progress UI failed: $($_.Exception.Message) - continuing without window" }
}
Set-Progress 1 "Starting as $([Security.Principal.WindowsIdentity]::GetCurrent().Name)"
Set-Progress 1 "windowsRoot=$win live=$isLive"

$evtxDir = Join-Path $OutDir 'evtx'
New-Item -ItemType Directory -Force -Path $evtxDir | Out-Null

Set-Progress 2 'Collecting event logs (EVTX)'
if ($isLive) {
    # Live log files are locked by the Event Log service - export channels with
    # wevtutil instead of copying. No /lf flag: it fails on some builds.
    $wantedChannels = @(
        'System', 'Application',
        'Microsoft-Windows-Kernel-WHEA/Errors',
        'Microsoft-Windows-Kernel-WHEA/Operational',
        'Microsoft-Windows-Kernel-Dump/Operational',
        'Microsoft-Windows-Storage-Storport/Operational',
        'Microsoft-Windows-Storage-Storport/Health',
        'Microsoft-Windows-Ntfs/Operational',
        'Microsoft-Windows-CodeIntegrity/Operational',
        'Microsoft-Windows-Kernel-PnP/Configuration',
        'Microsoft-Windows-Kernel-Boot/Operational',
        'Microsoft-Windows-WindowsUpdateClient/Operational',
        'Microsoft-Windows-Kernel-Power/Thermal-Operational',
        'Microsoft-Windows-Kernel-Processor-Power/Diagnostic',
        'Microsoft-Windows-Kernel-LiveDump/Operational',
        'HardwareEvents',
        'Microsoft-Windows-Diagnostics-Performance/Operational'
    )
    foreach ($chan in $wantedChannels) {
        $safeName = ($chan -replace '/', '%4') + '.evtx'
        $dst = Join-Path $evtxDir $safeName
        try { $wevOut = & wevtutil.exe epl "$chan" "$dst" /ow:true 2>&1 }
        catch { $wevOut = $_.Exception.Message }
        if (Test-Path $dst) { Out-Log "exported $chan" }
        else { Out-Log "missing $chan :: $(Truncate ([string]$wevOut) 120)" }
        Pump-Ui
    }
} else {
    $src = Join-Path $win 'Windows\System32\winevt\Logs'
    $wanted = @(
        'System.evtx', 'Application.evtx',
        'Microsoft-Windows-Kernel-WHEA%4Errors.evtx',
        'Microsoft-Windows-Kernel-WHEA%4Operational.evtx',
        'Microsoft-Windows-Kernel-Dump%4Operational.evtx',
        'Microsoft-Windows-Storage-Storport%4Operational.evtx',
        'Microsoft-Windows-Storage-Storport%4Health.evtx',
        'Microsoft-Windows-Ntfs%4Operational.evtx',
        'Microsoft-Windows-CodeIntegrity%4Operational.evtx',
        'Microsoft-Windows-Kernel-PnP%4Configuration.evtx',
        'Microsoft-Windows-Kernel-Boot%4Operational.evtx',
        'Microsoft-Windows-WindowsUpdateClient%4Operational.evtx',
        'Microsoft-Windows-Kernel-Power%4Thermal-Operational.evtx',
        'Microsoft-Windows-Kernel-Processor-Power%4Diagnostic.evtx',
        'Microsoft-Windows-Kernel-LiveDump%4Operational.evtx',
        'HardwareEvents.evtx',
        'Microsoft-Windows-Diagnostics-Performance%4Operational.evtx'
    )
    foreach ($name in $wanted) {
        $p = Join-Path $src $name
        if (Test-Path $p) {
            try { Copy-Item $p $evtxDir -Force -ErrorAction Stop; Out-Log "copied $name" }
            catch { Out-Log "FAIL copy $name :: $($_.Exception.Message)" }
        } else { Out-Log "missing $name" }
        Pump-Ui
    }
}

Set-Progress 3 'Copying WER reports'
$werSrc = Join-Path $win 'ProgramData\Microsoft\Windows\WER\ReportArchive'
if (Test-Path $werSrc) {
    try { robocopy $werSrc (Join-Path $OutDir 'wer') /E /R:0 /W:0 /NFL /NDL /NJH /NJS | Out-Null
          Out-Log 'copied WER ReportArchive' }
    catch { Out-Log "FAIL WER ReportArchive :: $($_.Exception.Message)" }
}
$werSrc2 = Join-Path $win 'ProgramData\Microsoft\Windows\WER\ReportQueue'
if (Test-Path $werSrc2) {
    try { robocopy $werSrc2 (Join-Path $OutDir 'wer_queue') /E /R:0 /W:0 /NFL /NDL /NJH /NJS | Out-Null
          Out-Log 'copied WER ReportQueue' }
    catch { Out-Log "FAIL WER ReportQueue :: $($_.Exception.Message)" }
}

Set-Progress 4 'Reading hardware (CPU, RAM, disks, GPU)'
# Every WMI/CIM query is optional - a broken machine may have a broken WMI.
try { $cs = Get-CimInstance Win32_ComputerSystem -ErrorAction Stop }
catch { $cs = $null; Out-Log "WMI ComputerSystem failed: $($_.Exception.Message)" }
try { $bios = Get-CimInstance Win32_BIOS -ErrorAction Stop }
catch { $bios = $null; Out-Log "WMI BIOS failed: $($_.Exception.Message)" }
try { $osLive = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop }
catch { $osLive = $null; Out-Log "WMI OperatingSystem failed: $($_.Exception.Message)" }
try { $cpu = Get-CimInstance Win32_Processor -ErrorAction Stop | Select-Object -First 1 }
catch { $cpu = $null; Out-Log "WMI Processor failed: $($_.Exception.Message)" }
try {
    $ramMods = @(Get-CimInstance Win32_PhysicalMemory -ErrorAction Stop | ForEach-Object {
        [ordered]@{
            bank         = [string]$_.BankLabel
            manufacturer = [string]$_.Manufacturer.Trim()
            part         = [string]$_.PartNumber.Trim()
            serial       = [string]$_.SerialNumber
            bytes        = [int64]$_.Capacity
            speed_mhz    = [int]$_.Speed
        }
    })
} catch { $ramMods = @(); Out-Log "WMI PhysicalMemory failed: $($_.Exception.Message)" }
$ramVendors = @($ramMods.manufacturer | Where-Object { $_ } | Sort-Object -Unique)
$ramParts = @($ramMods.part | Where-Object { $_ } | Sort-Object -Unique)

try {
    $gpus = @(Get-CimInstance Win32_VideoController -ErrorAction Stop | ForEach-Object {
        [ordered]@{
            name    = [string]$_.Name
            driver  = [string]$_.DriverVersion
            status  = [string]$_.Status
            pnp     = [string]$_.PNPDeviceID
        }
    })
} catch { $gpus = @(); Out-Log "WMI VideoController failed: $($_.Exception.Message)" }

try {
    $disks = @(Get-PhysicalDisk -ErrorAction Stop | ForEach-Object {
        [ordered]@{
            friendly     = [string]$_.FriendlyName
            media        = [string]$_.MediaType
            health       = [string]$_.HealthStatus
            bus          = [string]$_.BusType
            size_bytes   = [int64]$_.Size
            serial       = [string]$_.SerialNumber
            firmware     = [string]$_.FirmwareVersion
        }
    })
} catch { $disks = @(); Out-Log "Get-PhysicalDisk failed: $($_.Exception.Message)" }

Set-Progress 5 'Reading SMART / reliability counters'

# NVMe SMART/Health log (page 0x02) via IOCTL_STORAGE_QUERY_PROPERTY
# protocol-specific query - the same mechanism smartmontools uses. Works on
# client Windows where reliability-counter fields are null and the
# Storport/Health channel has no events. Returns the raw 512-byte log.
$nvmeQueryOk = $false
try {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class NvmeQuery {
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern IntPtr CreateFileW(string lpFileName, uint dwDesiredAccess, uint dwShareMode,
        IntPtr lpSecurityAttributes, uint dwCreationDisposition, uint dwFlagsAndAttributes,
        IntPtr hTemplateFile);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool DeviceIoControl(IntPtr hDevice, uint dwIoControlCode,
        byte[] lpInBuffer, uint nInBufferSize, byte[] lpOutBuffer, uint nOutBufferSize,
        out uint lpBytesReturned, IntPtr lpOverlapped);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);

    public static byte[] HealthLog(int driveNumber) {
        IntPtr h = CreateFileW("\\\\.\\PhysicalDrive" + driveNumber,
            0x80000000u /* GENERIC_READ */, 3u /* SHARE_READ|WRITE */,
            IntPtr.Zero, 3u /* OPEN_EXISTING */, 0u, IntPtr.Zero);
        if (h == new IntPtr(-1)) return null;
        try {
            // STORAGE_PROPERTY_QUERY(8) + STORAGE_PROTOCOL_SPECIFIC_DATA(40)
            byte[] inp = new byte[48];
            BitConverter.GetBytes(49).CopyTo(inp, 0);   // StorageAdapterProtocolSpecificProperty
            BitConverter.GetBytes(0).CopyTo(inp, 4);    // PropertyStandardQuery
            BitConverter.GetBytes(3).CopyTo(inp, 8);    // ProtocolType = Nvme
            BitConverter.GetBytes(2).CopyTo(inp, 12);   // DataType = NVMeDataTypeLogPage
            BitConverter.GetBytes(2).CopyTo(inp, 16);   // RequestValue = log page 2 (SMART/health)
            BitConverter.GetBytes(40).CopyTo(inp, 24);  // ProtocolDataOffset
            BitConverter.GetBytes(512).CopyTo(inp, 28); // ProtocolDataLength
            byte[] outp = new byte[48 + 512];
            uint ret;
            if (!DeviceIoControl(h, 0x2D1400u /* IOCTL_STORAGE_QUERY_PROPERTY */,
                                 inp, 48, outp, (uint)outp.Length, out ret, IntPtr.Zero)) return null;
            if (BitConverter.ToInt32(outp, 28) < 512) return null; // ProtocolDataLength too short
            byte[] log = new byte[512];
            Array.Copy(outp, 48, log, 0, 512);
            return log;
        } finally { CloseHandle(h); }
    }
}
'@
    $nvmeQueryOk = $true
} catch { Out-Log "Add-Type NvmeQuery failed: $($_.Exception.Message)" }

$smart = @()
try { $physDisks = @(Get-PhysicalDisk -ErrorAction Stop) }
catch { $physDisks = @(); Out-Log "Get-PhysicalDisk (SMART) failed: $($_.Exception.Message)" }
foreach ($pd in $physDisks) {
    try {
        $c = Get-StorageReliabilityCounter -PhysicalDisk $pd -ErrorAction Stop
        $nvmeLog = $null
        if ($nvmeQueryOk -and [string]$pd.BusType -eq 'NVMe' -and $null -ne $pd.DeviceId) {
            try {
                $log = [NvmeQuery]::HealthLog([int]$pd.DeviceId)
                if ($log -and (@($log | Where-Object { $_ -ne 0 })).Count -gt 0) {
                    $nvmeLog = -join ($log | ForEach-Object { $_.ToString('x2') })
                }
            } catch { Out-Log "NVMe health query drive $($pd.DeviceId): $($_.Exception.Message)" }
        }
        $smart += [ordered]@{
            friendly                 = [string]$pd.FriendlyName
            serial                   = [string]$pd.SerialNumber
            temperature              = $c.Temperature
            wear                     = $c.Wear
            power_on_hours           = $c.PowerOnHours
            read_errors_corrected    = $c.ReadErrorsCorrected
            read_errors_total        = $c.ReadErrorsTotal
            read_errors_uncorrected  = $c.ReadErrorsUncorrected
            write_errors_corrected   = $c.WriteErrorsCorrected
            write_errors_uncorrected = $c.WriteErrorsUncorrected
            read_latency_max_ms      = $c.ReadLatencyMax
            write_latency_max_ms     = $c.WriteLatencyMax
            flush_latency_max_ms     = $c.FlushLatencyMax
            nvme_log                 = $nvmeLog
        }
    } catch {
        $smart += [ordered]@{ friendly = [string]$pd.FriendlyName; serial = [string]$pd.SerialNumber; error = $_.Exception.Message }
    }
}

# ATA SMART failure prediction - only present where the storage stack
# exposes it (SATA drives behind atapi/storahci; absent on pure-NVMe boxes).
try {
    foreach ($fp in @(Get-CimInstance -Namespace root/wmi -ClassName MSStorageDriver_FailurePredictStatus -ErrorAction Stop)) {
        $inst = [string]$fp.InstanceName
        foreach ($s in $smart) {
            $ser = ([string]$s.serial -replace '[_\.]', '')
            if ($ser.Length -gt 4 -and $inst -like "*$ser*") {
                $s.predict_failure = [bool]$fp.PredictFailure
            }
        }
    }
} catch { }

try {
    $battery = @(Get-CimInstance Win32_Battery -ErrorAction Stop | ForEach-Object {
        [ordered]@{
            name             = [string]$_.Name
            status           = [string]$_.Status
            estimated_pct    = $_.EstimatedChargeRemaining
            design_capacity  = $_.DesignCapacity
            full_charge      = $_.FullChargeCapacity
        }
    })
} catch { $battery = @(); Out-Log "WMI Battery failed: $($_.Exception.Message)" }

Set-Progress 6 'Reading registry + copying hives'
$customer = [ordered]@{ computer_name = $null; product_name = $null; current_build = $null; ubr = $null; install_date = $null }
$crash = [ordered]@{}

if ($isLive) {
    # Live OS: hives are already mounted under HKLM.
    $customer.computer_name = (Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\ComputerName\ComputerName' -ErrorAction SilentlyContinue).ComputerName
    $cc = Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\CrashControl' -ErrorAction SilentlyContinue
    if ($cc) {
        $crash = [ordered]@{
            AutoReboot       = [int]$cc.AutoReboot
            CrashDumpEnabled = [int]$cc.CrashDumpEnabled
            MinidumpDir      = [string]$cc.MinidumpDir
            DumpFile         = [string]$cc.DumpFile
            DumpFilters      = [string]$cc.DumpFilters
            LogEvent         = [int]$cc.LogEvent
        }
    }
    $cv = Get-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction SilentlyContinue
    if ($cv) {
        $customer.product_name = [string]$cv.ProductName
        $customer.current_build = [string]$cv.CurrentBuild
        $customer.ubr = [string]$cv.UBR
        $customer.display_version = [string]$cv.DisplayVersion
        $customer.build_lab = [string]$cv.BuildLabEx
        if ($cv.InstallDate) {
            $customer.install_date = ([DateTime]'1970-01-01Z').AddSeconds([int]$cv.InstallDate).ToString('yyyy-MM-dd')
        }
    }
    Out-Log 'read live registry'

    # Live hives are locked - export with reg save (needs admin backup rights).
    # DEFAULT mounts under HKU\.DEFAULT on a live box (HKLM\DEFAULT does not
    # exist), so it gets its own source path.
    $hivesDir = Join-Path $OutDir 'hives'
    New-Item -ItemType Directory -Force -Path $hivesDir | Out-Null
    $liveHives = [ordered]@{
        'SYSTEM'   = 'HKLM\SYSTEM'
        'SOFTWARE' = 'HKLM\SOFTWARE'
        'SAM'      = 'HKLM\SAM'
        'SECURITY' = 'HKLM\SECURITY'
        'DEFAULT'  = 'HKU\.DEFAULT'
    }
    foreach ($hive in $liveHives.Keys) {
        $dst = Join-Path $hivesDir $hive
        try { $null = & reg.exe save $liveHives[$hive] "$dst" /y 2>&1 }
        catch { Out-Log "FAIL hive $hive :: $($_.Exception.Message)" }
        if (Test-Path $dst) { Out-Log "saved hive $hive" }
        else { Out-Log "FAIL hive $hive" }
        Pump-Ui
    }
} else {
    # Offline volume: mount read points with reg load, copy raw hive files.
    $sysHive = Join-Path $win 'Windows\System32\config\SYSTEM'
    $swHive = Join-Path $win 'Windows\System32\config\SOFTWARE'
    try { $null = reg unload HKLM\OffSys 2>$null } catch { }
    try { $null = reg unload HKLM\OffSoft 2>$null } catch { }
    if (Test-Path $sysHive) {
        try { $null = reg load HKLM\OffSys $sysHive 2>&1 }
        catch { Out-Log "reg load SYSTEM failed: $($_.Exception.Message)" }
        try {
            $customer.computer_name = (Get-ItemProperty -Path 'HKLM:\OffSys\ControlSet001\Control\ComputerName\ComputerName' -ErrorAction SilentlyContinue).ComputerName
            $cc = Get-ItemProperty -Path 'HKLM:\OffSys\ControlSet001\Control\CrashControl' -ErrorAction SilentlyContinue
            if ($cc) {
                $crash = [ordered]@{
                    AutoReboot       = [int]$cc.AutoReboot
                    CrashDumpEnabled = [int]$cc.CrashDumpEnabled
                    MinidumpDir      = [string]$cc.MinidumpDir
                    DumpFile         = [string]$cc.DumpFile
                    DumpFilters      = [string]$cc.DumpFilters
                    LogEvent         = [int]$cc.LogEvent
                }
            }
            Out-Log 'loaded SYSTEM hive'
        } finally { try { $null = reg unload HKLM\OffSys 2>$null } catch { } }
    }
    if (Test-Path $swHive) {
        try { $null = reg load HKLM\OffSoft $swHive 2>&1 }
        catch { Out-Log "reg load SOFTWARE failed: $($_.Exception.Message)" }
        try {
            $cv = Get-ItemProperty -Path 'HKLM:\OffSoft\Microsoft\Windows NT\CurrentVersion' -ErrorAction SilentlyContinue
            if ($cv) {
                $customer.product_name = [string]$cv.ProductName
                $customer.current_build = [string]$cv.CurrentBuild
                $customer.ubr = [string]$cv.UBR
                $customer.display_version = [string]$cv.DisplayVersion
                $customer.build_lab = [string]$cv.BuildLabEx
                if ($cv.InstallDate) {
                    $customer.install_date = ([DateTime]'1970-01-01Z').AddSeconds([int]$cv.InstallDate).ToString('yyyy-MM-dd')
                }
            }
            Out-Log 'loaded SOFTWARE hive'
        } finally { try { $null = reg unload HKLM\OffSoft 2>$null } catch { } }
    }

    # Copy the raw config hives (unlocked on an offline volume).
    $hivesDir = Join-Path $OutDir 'hives'
    New-Item -ItemType Directory -Force -Path $hivesDir | Out-Null
    $cfgDir = Join-Path $win 'Windows\System32\config'
    foreach ($hive in 'SYSTEM', 'SOFTWARE', 'SAM', 'SECURITY', 'DEFAULT') {
        $src = Join-Path $cfgDir $hive
        if (Test-Path $src) {
            try { Copy-Item $src $hivesDir -Force -ErrorAction Stop; Out-Log "copied hive $hive" }
            catch { Out-Log "FAIL hive $hive :: $($_.Exception.Message)" }
        } else { Out-Log "missing hive $hive" }
        Pump-Ui
    }
}

$sys = Join-Path $evtxDir 'System.evtx'
$app = Join-Path $evtxDir 'Application.evtx'
$stor = Join-Path $evtxDir 'Microsoft-Windows-Storage-Storport%4Operational.evtx'
$storHealth = Join-Path $evtxDir 'Microsoft-Windows-Storage-Storport%4Health.evtx'
$wheaE = Join-Path $evtxDir 'Microsoft-Windows-Kernel-WHEA%4Errors.evtx'
$therm = Join-Path $evtxDir 'Microsoft-Windows-Kernel-Power%4Thermal-Operational.evtx'
$kdump = Join-Path $evtxDir 'Microsoft-Windows-Kernel-Dump%4Operational.evtx'
$ci = Join-Path $evtxDir 'Microsoft-Windows-CodeIntegrity%4Operational.evtx'
$ntfs = Join-Path $evtxDir 'Microsoft-Windows-Ntfs%4Operational.evtx'
$pnp = Join-Path $evtxDir 'Microsoft-Windows-Kernel-PnP%4Configuration.evtx'
$kboot = Join-Path $evtxDir 'Microsoft-Windows-Kernel-Boot%4Operational.evtx'
$wu = Join-Path $evtxDir 'Microsoft-Windows-WindowsUpdateClient%4Operational.evtx'

Set-Progress 7 'Decoding crash / power events (Event 41, 6008, 1074)'
$ev41 = Collect-Events $sys '*[System[EventID=41]]' 50
$ev1001 = Collect-Events $sys '*[System[(EventID=1001)]]' 30
$ev6008 = Collect-Events $sys '*[System[EventID=6008]]' 40
$ev1074 = Collect-Events $sys '*[System[EventID=1074]]' 40
$ev161 = Collect-Events $sys '*[System[(EventID=161)]]' 30
Set-Progress 8 'Decoding thermal events'
$ev86 = Collect-Events $sys '*[System[(EventID=86)]]' 50
$ev109 = Collect-Events $sys '*[System[(EventID=109)]]' 40
$ev37 = Collect-Events $sys '*[System[(EventID=37) and Provider[@Name="Microsoft-Windows-Kernel-Processor-Power"]]]' 40
Set-Progress 9 'Decoding storage / Storport / NTFS'
$evDisk = Collect-Events $sys '*[System[(EventID=7 or EventID=11 or EventID=51 or EventID=153) and Provider[@Name="disk"]]]' 40
$ev219 = Collect-Events $sys '*[System[(EventID=219)]]' 20
$stor549 = Collect-Events $stor '*[System[(EventID=500 or EventID=501 or EventID=549 or EventID=550 or EventID=557 or EventID=561)]]' 80
# Storport/Health channel: periodic SMART snapshots - 510 SMART attributes,
# 511 ATA device statistics, 512 NVMe health log, 537-548 miniport health.
$storHealthEv = Collect-Events $storHealth '*' 60
$ntfsEv = Collect-Events $ntfs '*[System[(Level=1 or Level=2 or Level=3)]]' 30
Set-Progress 10 'Decoding WHEA, Code Integrity, Application, WU'
$whea = Collect-Events $wheaE '*' 40
$thermalOp = Collect-Events $therm '*' 40
$kdumpEv = Collect-Events $kdump '*' 30
$ciEv = Collect-Events $ci '*[System[(Level=1 or Level=2 or Level=3)]]' 40
$pnpEv = Collect-Events $pnp '*[System[(Level=1 or Level=2)]]' 30
$appErr = Collect-Events $app '*[System[(Level=1 or Level=2)]]' 40
$wuErr = Collect-Events $wu '*[System[(Level=1 or Level=2)]]' 20
$kbootEv = Collect-Events $kboot '*' 20

# enrich Event 41
$bugcheckNames = @{
    0 = 'none (hard power / hang)'
    10 = 'IRQL_NOT_LESS_OR_EQUAL'
    25 = 'BAD_POOL_HEADER'
    26 = 'MEMORY_MANAGEMENT'
    30 = 'KMODE_EXCEPTION_NOT_HANDLED'
    36 = 'NTFS_FILE_SYSTEM'
    59 = 'SYSTEM_SERVICE_EXCEPTION'
    80 = 'PAGE_FAULT_IN_NONPAGED_AREA'
    116 = 'BAD_SYSTEM_CONFIG_INFO'
    119 = 'KERNEL_STACK_INPAGE_ERROR'
    122 = 'KERNEL_DATA_INPAGE_ERROR'
    123 = 'INACCESSIBLE_BOOT_DEVICE'
    127 = 'UNEXPECTED_KERNEL_MODE_TRAP'
    128 = 'NMI_HARDWARE_FAILURE'
    133 = 'SETUP_FAILURE'
    156 = 'MACHINE_CHECK_EXCEPTION'
    159 = 'DRIVER_POWER_STATE_FAILURE'
    190 = 'ATTEMPTED_WRITE_TO_READONLY_MEMORY'
    194 = 'BAD_POOL_CALLER'
    197 = 'DRIVER_CORRUPTED_EXPOOL'
    209 = 'DRIVER_IRQL_NOT_LESS_OR_EQUAL'
    222 = 'POOL_CORRUPTION_IN_FILE_AREA'
    237 = 'UNMOUNTABLE_BOOT_VOLUME'
    239 = 'CRITICAL_PROCESS_DIED'
    244 = 'CRITICAL_OBJECT_TERMINATION'
    252 = 'ATTEMPTED_EXECUTE_OF_NOEXECUTE_MEMORY'
    254 = 'BUGCODE_USB_DRIVER'
    257 = 'CLOCK_WATCHDOG_TIMEOUT'
    269 = 'WDF_VIOLATION'
    270 = 'VIDEO_MEMORY_MANAGEMENT_INTERNAL'
    278 = 'VIDEO_TDR_FAILURE'
    279 = 'VIDEO_TDR_TIMEOUT_DETECTED'
    292 = 'WHEA_UNCORRECTABLE_ERROR'
    307 = 'DPC_WATCHDOG_TIMEOUT'
    313 = 'KERNEL_SECURITY_CHECK_FAILURE'
    314 = 'KERNEL_MODE_HEAP_CORRUPTION'
    324 = 'BUGCODE_USB3_DRIVER'
    340 = 'UNEXPECTED_STORE_EXCEPTION'
    351 = 'LOST_DELAYED_WRITE_TIMEOUT'
    455 = 'STORE_DATA_STRUCTURE_CORRUPTION'
}
foreach ($s in $ev41.samples) {
    $code = 0
    [void][int]::TryParse([string]$s.data.BugcheckCode, [ref]$code)
    $s.bugcheck_code = $code
    $s.bugcheck_hex = ('0x{0:X}' -f $code)
    $s.bugcheck_name = $bugcheckNames[$code]
    if (-not $s.bugcheck_name) { $s.bugcheck_name = 'unknown' }
}

Set-Progress 11 'Parsing WER, CBS, copying crash dumps'
# WER
$werList = @()
foreach ($wd in @((Join-Path $OutDir 'wer'), (Join-Path $OutDir 'wer_queue'))) {
    if (Test-Path $wd) {
        foreach ($rep in Get-ChildItem $wd -Recurse -Filter 'Report.wer' -ErrorAction SilentlyContinue) {
            $lines = Get-Content $rep.FullName -Encoding Unicode -ErrorAction SilentlyContinue
            $kv = [ordered]@{ folder = $rep.Directory.Name; path = $rep.FullName }
            foreach ($line in $lines) {
                if ($line -match '^(EventType|OriginalFilename|AppName|AppPath|FriendlyEventName|NsAppName)=(.*)$') {
                    $kv[$Matches[1]] = $Matches[2]
                }
            }
            $werList += $kv
            if ($werList.Count -ge 25) { break }
        }
    }
    if ($werList.Count -ge 25) { break }
}

# CBS last errors (live CBS.log can be locked - fall back to a copy)
$cbsHits = @()
$cbs = Join-Path $win 'Windows\Logs\CBS\CBS.log'
if (Test-Path $cbs) {
    $cbsPath = $cbs
    try {
        $null = Select-String -Path $cbs -Pattern 'x' -List -ErrorAction Stop
    } catch {
        $cbsPath = Join-Path $OutDir 'CBS.snapshot.log'
        try { Copy-Item $cbs $cbsPath -Force -ErrorAction Stop } catch { $cbsPath = $null }
    }
    if ($cbsPath) {
        try {
            $hits = Select-String -Path $cbsPath -Pattern 'Error\s+|0x800f|CBS_E_|corrupt' -CaseSensitive:$false -ErrorAction SilentlyContinue | Select-Object -Last 25
            foreach ($h in $hits) {
                if ($h.Line -notmatch 'KB3025096') { $cbsHits += Truncate $h.Line 300 }
            }
        } catch { }
    }
}

# Ship the actual .dmp files - the only shot at them. Cap at the 10 most
# recent; MEMORY.DMP stays excluded (can be GBs) but its size is recorded.
# Every dump's first 8KB (the DUMP_HEADER: bugcheck code + parameters) is
# captured as hex so the report can read the crash even when the file is
# not copied or is later unavailable.
function Read-FileHeadHex([string]$path, [int]$n = 8192) {
    try {
        $fs = [System.IO.File]::OpenRead($path)
        $buf = New-Object byte[] $n
        $got = $fs.Read($buf, 0, $n)
        $fs.Close()
        if ($got -gt 0) { return -join ($buf[0..($got - 1)] | ForEach-Object { $_.ToString('x2') }) }
    } catch {
        Out-Log "FAIL dump header read $path :: $($_.Exception.Message)"
    }
    return $null
}
$mdir = Join-Path $win 'Windows\Minidump'
$mdumpOut = Join-Path $OutDir 'minidumps'
$memDmp = Get-Item (Join-Path $win 'Windows\MEMORY.DMP') -ErrorAction SilentlyContinue
$dumps = [ordered]@{
    minidump_dir_exists = [bool](Test-Path $mdir)
    memory_dmp_exists   = [bool]$memDmp
    memory_dmp_bytes    = if ($memDmp) { [int64]$memDmp.Length } else { $null }
    memory_dmp_header   = if ($memDmp) { Read-FileHeadHex $memDmp.FullName } else { $null }
    minidumps           = @()
}
if (Test-Path $mdir) {
    New-Item -ItemType Directory -Force -Path $mdumpOut | Out-Null
    $dumps.minidumps = @(Get-ChildItem $mdir -Filter '*.dmp' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 10 | ForEach-Object {
            $hdr = Read-FileHeadHex $_.FullName
            $copied = $false
            try { Copy-Item $_.FullName $mdumpOut -Force -ErrorAction Stop; $copied = $true } catch { }
            [ordered]@{ name = $_.Name; bytes = $_.Length; mtime = $_.LastWriteTime.ToString('yyyy-MM-ddTHH:mm:ss'); copied = $copied; header = $hdr }
        })
    $copiedCount = @($dumps.minidumps | Where-Object { $_.copied }).Count
    Out-Log "minidumps: $($dumps.minidumps.Count) found, $copiedCount copied"
}

function Count-Of($pack) {
    if ($pack.error -eq 'missing') { return 0 }
    return [int]$pack.returned
}

$efCount = @($ev41.samples | Where-Object { $_.bugcheck_code -eq 239 }).Count
$zero41 = @($ev41.samples | Where-Object { $_.bugcheck_code -eq 0 }).Count

$diag = [ordered]@{
    schema_version = '1.0'
    collected_at   = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss')
    collector      = 'collector.ps1 (LogGrabber)'
    windows_root   = $win
    live = [ordered]@{
        computer_name = [string]$cs.Name
        manufacturer  = [string]$cs.Manufacturer
        model         = [string]$cs.Model
        serial        = [string]$bios.SerialNumber
        bios          = [string]$bios.SMBIOSBIOSVersion
        bios_date     = if ($bios.ReleaseDate -is [datetime]) { $bios.ReleaseDate.ToString('yyyy-MM-dd') } elseif ($bios.ReleaseDate) { [string]$bios.ReleaseDate } else { $null }
        os            = [string]$osLive.Caption
        os_version    = [string]$osLive.Version
        system_drive  = [string]$osLive.SystemDrive
        cpu           = [string]$cpu.Name
        cpu_max_mhz   = [int]$cpu.MaxClockSpeed
        cores         = [int]$cpu.NumberOfCores
        logical       = [int]$cpu.NumberOfLogicalProcessors
    }
    customer_os    = $customer
    memory         = [ordered]@{
        total_bytes     = [int64]$cs.TotalPhysicalMemory
        module_count    = $ramMods.Count
        mixed_vendors   = ($ramVendors.Count -gt 1)
        mixed_parts     = ($ramParts.Count -gt 1)
        vendors         = $ramVendors
        parts           = $ramParts
        modules         = $ramMods
    }
    graphics       = $gpus
    storage        = [ordered]@{ disks = $disks; smart = $smart }
    battery        = $battery
    crash_control  = $crash
    dumps          = $dumps
    counts         = [ordered]@{
        kernel_power_41          = Count-Of $ev41
        kernel_power_41_0xEF     = $efCount
        kernel_power_41_code0    = $zero41
        event_1001               = Count-Of $ev1001
        event_6008               = Count-Of $ev6008
        event_1074               = Count-Of $ev1074
        volmgr_161               = Count-Of $ev161
        thermal_86               = Count-Of $ev86
        kernel_power_109         = Count-Of $ev109
        processor_power_37       = Count-Of $ev37
        disk_7_11_51_153         = Count-Of $evDisk
        storport_549_557         = Count-Of $stor549
        storport_health          = Count-Of $storHealthEv
        whea_errors              = Count-Of $whea
        code_integrity           = Count-Of $ciEv
        ntfs_warn_error          = Count-Of $ntfsEv
        pnp_errors               = Count-Of $pnpEv
        application_errors       = Count-Of $appErr
        windows_update_errors    = Count-Of $wuErr
        wer_reports              = $werList.Count
    }
    events         = [ordered]@{
        kernel_power_41     = $ev41
        event_1001          = $ev1001
        event_6008          = $ev6008
        event_1074          = $ev1074
        volmgr_161          = $ev161
        thermal_86          = $ev86
        kernel_power_109    = $ev109
        processor_power_37  = $ev37
        disk                = $evDisk
        driver_219          = $ev219
        storport            = $stor549
        storage_health      = $storHealthEv
        whea_errors         = $whea
        thermal_operational = $thermalOp
        kernel_dump         = $kdumpEv
        code_integrity      = $ciEv
        ntfs                = $ntfsEv
        pnp                 = $pnpEv
        application         = $appErr
        windows_update      = $wuErr
        kernel_boot         = $kbootEv
    }
    wer            = $werList
    cbs_error_lines = $cbsHits
    flags          = [ordered]@{
        live_os_collect      = [bool]$isLive
        mixed_ram            = [bool]($ramVendors.Count -gt 1)
        no_minidumps         = -not $dumps.minidump_dir_exists
        auto_reboot_off      = ($crash.AutoReboot -eq 0)
        dumpfve_filter       = ([string]$crash.DumpFilters -match 'dumpfve')
        storport_stalls      = ((Count-Of $stor549) -gt 0)
        thermal_trips        = ((Count-Of $ev86) -gt 0)
        whea_errors          = ((Count-Of $whea) -gt 0)
        critical_process_died = ($efCount -gt 0)
        unexpected_shutdowns = ((Count-Of $ev41) -gt 0 -or (Count-Of $ev6008) -gt 0)
    }
}

Set-Progress 12 'Writing diagnostics.json'
$jsonPath = Join-Path $OutDir 'diagnostics.json'
($diag | ConvertTo-Json -Depth 10 -Compress:$false) | Out-File -FilePath $jsonPath -Encoding utf8
Out-Log "wrote $jsonPath"

'DONE' | Out-File (Join-Path $OutDir 'DONE.txt') -Encoding ascii
Out-Log 'DONE'
Set-Progress 14 "Finished - $OutDir"
Finish-ProgressUi "Finished. Close this window." $true

} catch {
    $err = $_.Exception.Message
    Write-Host "COLLECTOR FAILED: $err"
    try {
        New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
        "FAILED $err" | Out-File (Join-Path $OutDir 'FAILED.txt') -Encoding utf8
        $_ | Out-File (Join-Path $OutDir 'FAILED.txt') -Append -Encoding utf8
        Write-ProgressFile 0 $err 'failed'
    } catch { }
    try { Finish-ProgressUi "FAILED: $err" $false } catch { }
    Write-Host ""
    Write-Host "Press Enter to close."
    [void][Console]::ReadLine()
}
