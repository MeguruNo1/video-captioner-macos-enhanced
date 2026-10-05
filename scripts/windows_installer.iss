#ifndef AppVersion
  #error AppVersion must be specified by the build script
#endif
#ifndef PackageDir
  #error PackageDir must be specified by the build script
#endif
#ifndef ReleaseDir
  #error ReleaseDir must be specified by the build script
#endif
#ifndef ChineseMessages
  #error ChineseMessages must be specified by the build script
#endif

[Setup]
AppId={{9231BDE2-2715-46D9-A034-6E59B453BE18}
AppName=VideoCaptioner
AppVersion={#AppVersion}
AppPublisher=VideoCaptioner contributors
AppPublisherURL=https://github.com/MeguruNo1/video-captioner-macos-enhanced
DefaultDirName={localappdata}\Programs\VideoCaptioner
DefaultGroupName=VideoCaptioner
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#ReleaseDir}
OutputBaseFilename=VideoCaptioner-Windows-x64-v{#AppVersion}-Setup
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\VideoCaptioner.exe
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimp"; MessagesFile: "{#ChineseMessages}"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#PackageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\VideoCaptioner"; Filename: "{app}\VideoCaptioner.exe"
Name: "{autodesktop}\VideoCaptioner"; Filename: "{app}\VideoCaptioner.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\VideoCaptioner.exe"; Description: "{cm:LaunchProgram,VideoCaptioner}"; Flags: nowait postinstall skipifsilent
