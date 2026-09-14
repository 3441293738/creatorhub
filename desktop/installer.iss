; Compile after desktop/build_windows.py. The data directory is NEVER installed
; into, overwritten, or deleted by the installer/uninstaller.
#ifndef AppVersion
  #define AppVersion "0.2.0"
#endif
#ifndef AppSourceDir
  #define AppSourceDir "..\dist\windows\CreatorHub"
#endif
[Setup]
AppId={{93FD4B37-6436-4CE0-8249-BC487CC5A062}
AppName=CreatorHub
AppVersion={#AppVersion}
AppVerName=CreatorHub v{#AppVersion}
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
ShowLanguageDialog=no
UsePreviousLanguage=no

[Languages]
; Vendor the translation so local and CI builds never depend on compiler languages.
Name: "chinesesimplified"; MessagesFile: "languages\ChineseSimplified.isl"

[LangOptions]
DialogFontName=Microsoft YaHei UI
WelcomeFontName=Microsoft YaHei UI

[CustomMessages]
LaunchCreatorHub=启动 CreatorHub
InstallingWebView2=正在安装 Microsoft WebView2 运行环境，请保持网络连接…
WebView2StartFailed=WebView2 安装程序启动失败，请重新运行安装包。
WebView2Required=需要安装 Microsoft WebView2 运行环境才能继续。请检查网络连接后重试，原有 CreatorHub 数据保持不变。
UninstallKeepData=卸载将保留账号资料、配置、下载内容和备份。%n%n用户数据目录：%1%n%n卸载前请先退出 CreatorHub。如不再需要这些数据，可在卸载完成后自行备份并清理该目录。

[Files]
Source: "..\build\windows\MicrosoftEdgeWebview2Setup.exe"; Flags: dontcopy
Source: "{#AppSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: checkedonce

[Icons]
Name: "{group}\CreatorHub"; Filename: "{app}\CreatorHub.exe"
Name: "{autodesktop}\CreatorHub"; Filename: "{app}\CreatorHub.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\CreatorHub.exe"; Description: "{cm:LaunchCreatorHub}"; Flags: nowait postinstall skipifsilent

; Delta updates can add application-owned files not in the original install log.
; User data lives outside {app}; never delete the parent/user-data directories.
[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\CreatorHub.exe"

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
  WizardForm.StatusLabel.Caption := CustomMessage('InstallingWebView2');
  ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe');
  if not Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'),
    '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    Result := CustomMessage('WebView2StartFailed');
    Exit;
  end;
  Log(Format('WebView2 bootstrapper exit code: %d', [ResultCode]));
  for Attempt := 1 to 60 do
  begin
    if HasWebView2() then Exit;
    Sleep(1000);
  end;
  Result := CustomMessage('WebView2Required');
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  if not UninstallSilent then
    MsgBox(FmtMessage(CustomMessage('UninstallKeepData'), [ExpandConstant('{localappdata}\CreatorHub\user-data')]), mbInformation, MB_OK);
end;
