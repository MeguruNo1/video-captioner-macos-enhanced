[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '1.4.1'
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ReleaseDir = Join-Path $ProjectRoot 'dist\windows\release'
$Installer = Join-Path $ReleaseDir "VideoCaptioner-Windows-x64-v$Version-Setup.exe"
$InstallDir = Join-Path $env:RUNNER_TEMP 'VideoCaptioner-installed'
$Evidence = [ordered]@{ version = $Version; source_commit = $env:GITHUB_SHA; checks = @() }
$Process = $null

foreach ($Checksum in Get-ChildItem $ReleaseDir -Filter '*.sha256') {
    $Parts = (Get-Content $Checksum.FullName -Raw).Trim() -split '  ', 2
    $Actual = (Get-FileHash (Join-Path $ReleaseDir $Parts[1]) -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Actual -ne $Parts[0]) { throw "Checksum mismatch: $($Parts[1])" }
}
$Evidence.checks += 'Portable ZIP and installer SHA-256 verified'

$Install = Start-Process $Installer -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/DIR=$InstallDir", "/LOG=$ReleaseDir/install.log") -Wait -PassThru
if ($Install.ExitCode -ne 0) { throw "Installer failed: $($Install.ExitCode)" }
$Evidence.checks += 'Silent per-user installation succeeded'

try {
    $Bin = Join-Path $InstallDir '_internal\resource\bin\windows'
    & (Join-Path $Bin 'ffmpeg.exe') -version
    if ($LASTEXITCODE -ne 0) { throw 'Installed FFmpeg failed' }
    & (Join-Path $Bin 'ffprobe.exe') -version
    if ($LASTEXITCODE -ne 0) { throw 'Installed ffprobe failed' }
    $Evidence.checks += 'Installed FFmpeg and ffprobe run successfully'
    $Process = Start-Process (Join-Path $InstallDir 'VideoCaptioner.exe') -PassThru
    $Deadline = (Get-Date).AddSeconds(120)
    do {
        Start-Sleep -Seconds 2
        $Process.Refresh()
        if ($Process.HasExited) { throw "Installed application exited before showing its window: $($Process.ExitCode)" }
    } while ($Process.MainWindowHandle -eq 0 -and (Get-Date) -lt $Deadline)
    if ($Process.MainWindowHandle -eq 0 -or -not $Process.Responding) { throw 'Installed application did not show a responsive main window' }
    $Evidence.window_title = $Process.MainWindowTitle
    $Evidence.checks += 'Installed EXE shows a responsive native Windows main window'
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $Bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
    $Bitmap = [System.Drawing.Bitmap]::new($Bounds.Width, $Bounds.Height)
    $Graphics = [System.Drawing.Graphics]::FromImage($Bitmap)
    try {
        $Graphics.CopyFromScreen($Bounds.Location, [System.Drawing.Point]::Empty, $Bounds.Size)
        $Bitmap.Save((Join-Path $ReleaseDir 'windows-installed.png'), [System.Drawing.Imaging.ImageFormat]::Png)
    } finally {
        $Graphics.Dispose()
        $Bitmap.Dispose()
    }
} finally {
    if ($null -ne $Process -and -not $Process.HasExited) { Stop-Process -Id $Process.Id -Force }
}

$Uninstall = Start-Process (Join-Path $InstallDir 'unins000.exe') -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=$ReleaseDir/uninstall.log") -Wait -PassThru
if ($Uninstall.ExitCode -ne 0) { throw "Uninstaller failed: $($Uninstall.ExitCode)" }
if (Test-Path (Join-Path $InstallDir 'VideoCaptioner.exe')) { throw 'Uninstaller left the application executable behind' }
$Evidence.checks += 'Silent uninstallation removed the installed application'
$Evidence.unverified = @('Real-media transcription and alignment', 'CUDA acceleration', 'Code signing')
$Evidence | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $ReleaseDir 'verification.json') -Encoding utf8
