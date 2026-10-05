[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '1.4.1'
)

$ErrorActionPreference = 'Stop'
$AppName = 'VideoCaptioner'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$PyInstaller = Join-Path $ProjectRoot '.venv\Scripts\pyinstaller.exe'
$WindowsBin = Join-Path $ProjectRoot 'resource\bin\windows'
$Ffmpeg = Join-Path $WindowsBin 'ffmpeg.exe'
$Ffprobe = Join-Path $WindowsBin 'ffprobe.exe'
$BuildRoot = Join-Path $ProjectRoot 'build\windows'
$PyInstallerDist = Join-Path $ProjectRoot 'dist\windows\pyinstaller'
$ReleaseRoot = Join-Path $ProjectRoot 'dist\windows\release'
$AppDirectory = Join-Path $PyInstallerDist $AppName
$ArchiveName = "$AppName-Windows-x64-v$Version.zip"
$ArchivePath = Join-Path $ReleaseRoot $ArchiveName
$ChecksumPath = "$ArchivePath.sha256"
$env:PATH = "$WindowsBin;$env:PATH"

foreach ($RequiredFile in @($Python, $PyInstaller, $Ffmpeg, $Ffprobe)) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "Missing required file: $RequiredFile"
    }
}

$PythonBits = & $Python -c "import struct; print(struct.calcsize('P') * 8)"
if ($LASTEXITCODE -ne 0 -or $PythonBits.Trim() -ne '64') {
    throw 'The Windows release must be built with 64-bit Python.'
}

foreach ($Directory in @($BuildRoot, $PyInstallerDist)) {
    if (Test-Path -LiteralPath $Directory) {
        Remove-Item -LiteralPath $Directory -Recurse -Force
    }
}
foreach ($File in @($ArchivePath, $ChecksumPath)) {
    if (Test-Path -LiteralPath $File) {
        Remove-Item -LiteralPath $File -Force
    }
}
New-Item -ItemType Directory -Path $ReleaseRoot -Force | Out-Null

$PyInstallerArguments = @(
    '--noconfirm'
    '--clean'
    '--onedir'
    '--windowed'
    '--runtime-hook', (Join-Path $PSScriptRoot 'windows_runtime_hook.py')
    '--copy-metadata', 'torchcodec'
    '--recursive-copy-metadata', 'whisperx'
    '--collect-all', 'pyannote.audio'
    '--name', $AppName
    '--distpath', $PyInstallerDist
    '--workpath', $BuildRoot
    '--specpath', $BuildRoot
    '--icon', (Join-Path $ProjectRoot 'resource\assets\logo.png')
    '--add-data', "$(Join-Path $ProjectRoot 'resource');resource"
    '--add-data', "$(Join-Path $ProjectRoot 'app\core\utils\acceleration.py');app\core\utils"
    '--add-data', "$(Join-Path $ProjectRoot 'app\core\bk_asr\whisperx_runner.py');app\core\bk_asr"
    '--hidden-import', 'whisperx'
    '--hidden-import', 'torch'
    '--hidden-import', 'torchaudio'
    '--hidden-import', 'torchvision'
    '--hidden-import', 'pyannote.audio'
    '--hidden-import', 'faster_whisper'
    '--hidden-import', 'ctranslate2'
    '--collect-all', 'whisperx'
    '--collect-all', 'faster_whisper'
    (Join-Path $ProjectRoot 'main.py')
)

& $PyInstaller @PyInstallerArguments
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}

$Executable = Join-Path $AppDirectory "$AppName.exe"
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw "PyInstaller did not create $Executable"
}

$ReadmePath = Join-Path $AppDirectory 'README-Windows.txt'
@"
VideoCaptioner for Windows x64
Version: $Version

Run VideoCaptioner.exe. The package includes FFmpeg and ffprobe, but it does
not include speech-recognition models or user configuration. WhisperX models
are downloaded when first used and are stored under:

%LOCALAPPDATA%\VideoCaptioner\models

The packaged application uses CPU inference by default when compatible CUDA
hardware and dependencies are unavailable.
"@ | Set-Content -LiteralPath $ReadmePath -Encoding utf8

Compress-Archive -LiteralPath $AppDirectory -DestinationPath $ArchivePath -CompressionLevel Optimal
$Hash = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText(
    $ChecksumPath,
    "$Hash  $ArchiveName`n",
    [Text.Encoding]::ASCII
)

Write-Output "Built $ArchivePath"
Write-Output "Wrote $ChecksumPath"
