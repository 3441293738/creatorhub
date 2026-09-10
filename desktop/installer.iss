; Compile after desktop/build_windows.py. The data directory is NEVER installed
; into, overwritten, or deleted by the installer/uninstaller.
#ifndef AppVersion
  #define AppVersion "0.1.0"
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
Source: "..\build\windows\MicrosoftEdgeWebview2Setup.exe"; Flags: dontcopy
Source: "..\dist\windows\CreatorHub\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: checkedonce

[Icons]
Name: "{group}\CreatorHub"; Filename: "{app}\CreatorHub.exe"
Name: "{autodesktop}\CreatorHub"; Filename: "{app}\CreatorHub.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\CreatorHub.exe"; Description: "Launch CreatorHub"; Flags: nowait postinstall skipifsilent

[Code]
function HasWebView2(): Boolean;
var
  Version: String;
  Key: String;
begin
  Key := 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  Result := RegQueryStringValue(HKLM32, Key, 'pv', Version) and
    (Version <> '') and (Version <> '0.0.0.0');
  if not Result then
    Result := RegQueryStringValue(HKCU, Key, 'pv', Version) and
      (Version <> '') and (Version <> '0.0.0.0');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  Attempt: Integer;
begin
  Result := '';
  if HasWebView2() then Exit;
  WizardForm.StatusLabel.Caption := 'Installing Microsoft WebView2 Runtime (internet required)...';
  ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe');
  if not Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'),
    '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    Result := 'WebView2 setup could not start. Retry the installation.';
    Exit;
  end;
  Log(Format('WebView2 bootstrapper exit code: %d', [ResultCode]));
  for Attempt := 1 to 60 do
  begin
    if HasWebView2() then Exit;
    Sleep(1000);
  end;
  Result := 'Microsoft WebView2 Runtime is required. Check your internet connection and retry installation. Your CreatorHub data is unchanged.';
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  if not UninstallSilent then
    MsgBox('Account data, configuration, downloads and backups will be kept in your LocalAppData\CreatorHub\user-data folder. Stop CreatorHub before uninstalling. Delete that folder manually only if you no longer need the data.', mbInformation, MB_OK);
end;
