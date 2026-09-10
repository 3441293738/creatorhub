; Compile after desktop/build_windows.py. The data directory is NEVER installed
; into, overwritten, or deleted by the installer/uninstaller.
#ifndef AppVersion
  #define AppVersion "0.2.2"
#endif
[Setup]
AppId={{93FD4B37-6436-4CE0-8249-BC487CC5A062}
AppName=CreatorHub
AppVersion={#AppVersion}
AppPublisher=CreatorHub
AppPublisherURL=https://github.com/3441293738/creatorhub
DefaultDirName={localappdata}\Programs\CreatorHub
DefaultGroupName=CreatorHub
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist\installer
OutputBaseFilename=CreatorHub-Setup-{#AppVersion}-windows-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\CreatorHub.exe
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Files]
Source: "..\dist\windows\CreatorHub\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: checkedonce

[Icons]
Name: "{group}\CreatorHub"; Filename: "{app}\CreatorHub.exe"
Name: "{autodesktop}\CreatorHub"; Filename: "{app}\CreatorHub.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\CreatorHub.exe"; Description: "Launch CreatorHub"; Flags: nowait postinstall skipifsilent

[Code]
function InitializeUninstall(): Boolean;
begin
  Result := True;
  if not UninstallSilent then
    MsgBox('Account data, configuration, downloads and backups will be kept in your LocalAppData\CreatorHub\user-data folder. Stop CreatorHub before uninstalling. Delete that folder manually only if you no longer need the data.', mbInformation, MB_OK);
end;
