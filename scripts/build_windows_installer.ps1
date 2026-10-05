[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '1.4.1',
    [string]$CompilerPath = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PackageDir = Join-Path $ProjectRoot 'dist\windows\pyinstaller\VideoCaptioner'
$ReleaseDir = Join-Path $ProjectRoot 'dist\windows\release'
$InstallerName = "VideoCaptioner-Windows-x64-v$Version-Setup.exe"
$InstallerPath = Join-Path $ReleaseDir $InstallerName
$LanguagePath = Join-Path $ProjectRoot 'build\windows\ChineseSimplified.isl'

foreach ($File in @($CompilerPath, (Join-Path $PackageDir 'VideoCaptioner.exe'))) {
    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) {
        throw "Missing required file: $File"
    }
}

New-Item -ItemType Directory -Path (Split-Path -Parent $LanguagePath) -Force | Out-Null
Invoke-WebRequest 'https://raw.githubusercontent.com/jrsoftware/issrc/is-6_6_1/Files/Languages/Unofficial/ChineseSimplified.isl' -OutFile $LanguagePath
& $CompilerPath "/DAppVersion=$Version" "/DPackageDir=$PackageDir" "/DReleaseDir=$ReleaseDir" "/DChineseMessages=$LanguagePath" (Join-Path $PSScriptRoot 'windows_installer.iss')
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }
if (-not (Test-Path -LiteralPath $InstallerPath -PathType Leaf)) { throw 'Installer output is missing' }
$Hash = (Get-FileHash -LiteralPath $InstallerPath -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText("$InstallerPath.sha256", "$Hash  $InstallerName`n", [Text.Encoding]::ASCII)
Write-Output "Built $InstallerPath"
