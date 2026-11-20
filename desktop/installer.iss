; Compile after desktop/build_windows.py. The data directory is NEVER installed
; into, overwritten, or deleted by the installer/uninstaller.
#ifndef AppVersion
  #define AppVersion "0.2.0"
#endif
#ifndef AppSourceDir
  #define AppSourceDir "..\dist\windows\CreatorHub"
#endif
#define StagingDir AddBackslash(SourcePath) + "..\build\windows"
#define IconPath StagingDir + "\CreatorHub.ico"
#define Small100 StagingDir + "\installer-small-100.bmp"
#define Small150 StagingDir + "\installer-small-150.bmp"
#define Small200 StagingDir + "\installer-small-200.bmp"
#define Banner100 StagingDir + "\installer-banner-100.bmp"
#define Banner150 StagingDir + "\installer-banner-150.bmp"
#define Banner200 StagingDir + "\installer-banner-200.bmp"

[Setup]
AppId={{93FD4B37-6436-4CE0-8249-BC487CC5A062}
AppName=CreatorHub
AppVersion={#AppVersion}
AppVerName=CreatorHub v{#AppVersion}
AppPublisher=CreatorHub
AppPublisherURL=https://github.com/3441293738/creatorhub
AppSupportURL=https://github.com/3441293738/creatorhub/issues
AppUpdatesURL=https://github.com/3441293738/creatorhub/releases/latest
DefaultDirName={localappdata}\Programs\CreatorHub
DefaultGroupName=CreatorHub
DisableWelcomePage=no
DisableDirPage=no
DisableProgramGroupPage=yes
AppendDefaultDirName=yes
UsePreviousAppDir=yes
DirExistsWarning=no
EnableDirDoesntExistWarning=no
AllowUNCPath=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist\installer
OutputBaseFilename=CreatorHub-Setup-{#AppVersion}-windows-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
WizardSizePercent=118,118
#if FileExists(IconPath)
SetupIconFile={#IconPath}
#endif
#if FileExists(Small100) && FileExists(Small150) && FileExists(Small200)
WizardSmallImageFile={#Small100},{#Small150},{#Small200}
#endif
#if FileExists(Banner100) && FileExists(Banner150) && FileExists(Banner200)
WizardImageFile={#Banner100},{#Banner150},{#Banner200}
#endif
UninstallDisplayIcon={app}\CreatorHub.exe
UninstallDisplayName=CreatorHub
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
LaunchCreatorHub=立即启动 CreatorHub
InstallingWebView2=正在配置 Microsoft WebView2 运行环境，请保持网络连接…
WebView2StartFailed=WebView2 安装程序启动失败，请重新运行安装包。
WebView2Required=需要安装 Microsoft WebView2 运行环境才能继续。请检查网络连接后重试，原有 CreatorHub 数据保持不变。
UninstallKeepData=卸载将保留账号资料、配置、下载内容和备份。%n%n用户数据目录：%1%n%n卸载前请先退出 CreatorHub。如不再需要这些数据，可在卸载完成后自行备份并清理该目录。
WelcomeSubtitleFresh=即将在您的电脑上安装 CreatorHub v{#AppVersion} 本地多平台内容工作台。%n%n• 支持自由选择程序安装目录（如 D 盘或其他位置）%n• 账号登录态、配置、数据库与下载内容独立保存在用户数据目录%n• %1%n%n点击“下一步”选择安装位置。
WelcomeSubtitleUpgrade=检测到本机已安装 CreatorHub，即将升级或重新配置为 v{#AppVersion}。%n%n• 当前已安装位置：%1%n• 可直接覆盖升级，也可在下一步更换到其他磁盘目录%n• 原有账号登录态、配置、下载素材与备份将完整保留%n• %2%n%n点击“下一步”确认或修改安装位置。
WebView2StatusReady=运行环境检测：Microsoft WebView2 已就绪
WebView2StatusMissing=运行环境检测：缺少 Microsoft WebView2，安装时将自动联网补齐
SelectDirSubtitle=可保持默认位置，或点击“浏览”选择其他磁盘目录（如 D:\CreatorHub）。
ResetDefaultDir=恢复默认位置
SwitchSecondaryDir=切换到 %1
DiskSpaceSufficient=目标磁盘 (%1) 可用空间：%2  ·  空间充足
DiskSpaceLow=目标磁盘 (%1) 可用空间：%2  ·  建议至少预留 600 MB 可用空间
DiskSpaceUnknown=请选择本地可读写的固定磁盘目录
DataIsolationHint=说明：此处仅存放程序运行文件。账号资料、配置与下载内容独立保存在 %1，更换安装目录或升级不会丢失数据。
CleanOldDirOption=安装完成后自动清理原安装目录中的旧程序文件（不影响账号与用户数据）
DriveRootConfirm=不能直接将程序散落安装在磁盘根目录。%n%n是否自动使用子目录：%n%1 ？
ForeignDirConfirm=所选文件夹“%1”已包含其他文件。%n%n为避免程序文件混放并保障后续在线增量更新正常，建议安装到独立子目录：%n%2%n%n是否使用该子目录继续？
DataDirConflict=所选安装目录与 CreatorHub 用户数据目录存在重叠或包含关系：%n%1%n%n为保护账号数据在升级与卸载时不被影响，请选择独立的程序安装目录（例如 D:\CreatorHub 或点击“恢复默认位置”）。
DirNotWritable=当前用户对所选位置没有写入权限：%n%1%n%nCreatorHub 采用免管理员安装以支持平滑在线更新，请避免选择受系统保护的目录（如 C:\Program Files 或 C:\Windows），改选其他磁盘目录（如 D:\CreatorHub）或点击“恢复默认位置”。
AppRunningPrompt=检测到 CreatorHub（启动中心或后台服务）正在运行。%n%n请先在 CreatorHub 启动中心点击“停止并退出”（或从任务栏右下角托盘图标退出），然后点击“重试”继续。
ReadySectionMode=安装方式：
ReadyModeFresh=全新安装
ReadyModeUpgrade=原位覆盖升级（保留账号与用户数据）
ReadyModeRelocate=更换目录安装（原位置：%1）
ReadyCleanOldYes=完成后自动清理原目录中的旧程序文件
ReadyCleanOldNo=保留原目录文件不作改动
ReadySectionAppDir=程序安装位置：
ReadySectionDataDir=用户数据位置（独立保留）：
ReadySectionRuntime=运行环境状态：
ReadySectionShortcuts=快捷方式：
ReadyShortcutStartMenu=开始菜单快捷方式（CreatorHub）
ReadyShortcutDesktop=桌面快捷方式
FinishedSummary=CreatorHub v{#AppVersion} 已安装完成。%n%n程序安装目录：%1%n用户数据目录：%2%n%n后续可在启动中心内直接检查并一键更新版本。点击“完成”退出安装向导。

[Files]
Source: "..\build\windows\MicrosoftEdgeWebview2Setup.exe"; Flags: dontcopy
Source: "{#AppSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; Full-package upgrades also retire the old media stack. These three paths
; belong exclusively to the application, never to user-data or account profiles.
[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal\cv2"
Type: filesandordirs; Name: "{app}\_internal\numpy"
Type: filesandordirs; Name: "{app}\_internal\numpy.libs"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

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
const
  UninstallRegKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{93FD4B37-6436-4CE0-8249-BC487CC5A062}_is1';
  DRIVE_FIXED = 3;
  MinRecommendedBytes = 629145600; { 600 MiB }

var
  PreviousInstallDir: String;
  SecondaryDriveDir: String;
  ResetDirButton: TNewButton;
  AltDriveButton: TNewButton;
  DriveSpaceLabel: TNewStaticText;
  DataHintLabel: TNewStaticText;
  CleanOldDirCheck: TNewCheckBox;

function GetDriveTypeW(lpRootPathName: String): Cardinal;
  external 'GetDriveTypeW@kernel32.dll stdcall';

function NormalizeDir(const Value: String): String;
begin
  Result := Trim(Value);
  if (Length(Result) >= 3) and (Copy(Result, 2, 1) = ':') then
    Result := ExpandFileName(Result);
  if Result <> '' then
    Result := RemoveBackslashUnlessRoot(Result);
end;

function SameDir(const LeftPath, RightPath: String): Boolean;
begin
  Result := (LeftPath <> '') and (RightPath <> '') and
    (CompareText(NormalizeDir(LeftPath), NormalizeDir(RightPath)) = 0);
end;

function IsSubPathOrEqual(const ChildPath, ParentPath: String): Boolean;
var
  NormChild, NormParent, Prefix: String;
begin
  NormChild := NormalizeDir(ChildPath);
  NormParent := NormalizeDir(ParentPath);
  if (NormChild = '') or (NormParent = '') then
  begin
    Result := False;
    Exit;
  end;
  if CompareText(NormChild, NormParent) = 0 then
  begin
    Result := True;
    Exit;
  end;
  Prefix := AddBackslash(NormParent);
  Result := CompareText(Copy(NormChild, 1, Length(Prefix)), Prefix) = 0;
end;

function UserDataDir(): String;
begin
  Result := ExpandConstant('{localappdata}\CreatorHub\user-data');
end;

function DefaultInstallDir(): String;
begin
  Result := ExpandConstant('{localappdata}\Programs\CreatorHub');
end;

function DetectPreviousInstallDir(): String;
var
  Location: String;
begin
  Result := '';
  if RegQueryStringValue(HKCU, UninstallRegKey, 'Inno Setup: App Path', Location) or
     RegQueryStringValue(HKCU, UninstallRegKey, 'InstallLocation', Location) then
  begin
    Location := NormalizeDir(Location);
    if (Location <> '') and FileExists(AddBackslash(Location) + 'CreatorHub.exe') then
      Result := Location;
  end;
end;

function NearestExistingDir(const PathValue: String): String;
var
  Current, Parent: String;
begin
  Current := NormalizeDir(PathValue);
  while (Current <> '') and not DirExists(Current) do
  begin
    Parent := ExtractFileDir(Current);
    if (Parent = '') or (CompareText(Parent, Current) = 0) then
    begin
      Current := '';
      Break;
    end;
    Current := Parent;
  end;
  Result := Current;
end;

function CanWriteTargetDir(const TargetDir: String): Boolean;
var
  BaseDir, ProbeDir, ProbeFile: String;
  Suffix: String;
begin
  Result := False;
  Suffix := GetDateTimeString('hhnnss', #0, #0) + '-' + IntToStr(Random(1000000));
  if DirExists(TargetDir) then
  begin
    ProbeFile := AddBackslash(TargetDir) + '.creatorhub-probe-' + Suffix + '.tmp';
    if SaveStringToFile(ProbeFile, 'ok', False) then
    begin
      DeleteFile(ProbeFile);
      Result := True;
    end;
    Exit;
  end;
  BaseDir := NearestExistingDir(TargetDir);
  if BaseDir = '' then
    Exit;
  ProbeDir := AddBackslash(BaseDir) + '.creatorhub-probe-' + Suffix;
  if CreateDir(ProbeDir) then
  begin
    ProbeFile := AddBackslash(ProbeDir) + 'probe.tmp';
    if SaveStringToFile(ProbeFile, 'ok', False) then
    begin
      DeleteFile(ProbeFile);
      Result := True;
    end;
    RemoveDir(ProbeDir);
  end;
end;

function FindSecondaryDriveInstallDir(): String;
var
  SystemDrive, CandidateRoot, CandidateDir: String;
  DriveLetter: Integer;
begin
  Result := '';
  SystemDrive := Uppercase(ExtractFileDrive(ExpandConstant('{win}')));
  for DriveLetter := Ord('D') to Ord('Z') do
  begin
    CandidateRoot := Chr(DriveLetter) + ':\';
    if Uppercase(Chr(DriveLetter) + ':') = SystemDrive then
      Continue;
    CandidateDir := CandidateRoot + 'CreatorHub';
    if (GetDriveTypeW(CandidateRoot) = DRIVE_FIXED) and DirExists(CandidateRoot) and
       CanWriteTargetDir(CandidateDir) then
    begin
      Result := CandidateDir;
      Exit;
    end;
  end;
end;

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

function IsLockFileHeld(const LockPath: String): Boolean;
var
  Stream: TFileStream;
begin
  Result := False;
  if not FileExists(LockPath) then
    Exit;
  try
    Stream := TFileStream.Create(LockPath, fmOpenRead or fmShareExclusive);
    Stream.Free;
  except
    Result := True;
  end;
end;

function IsCreatorHubRunning(): Boolean;
var
  RuntimeDir: String;
begin
  RuntimeDir := AddBackslash(UserDataDir()) + 'runtime\';
  Result := IsLockFileHeld(RuntimeDir + 'desktop.lock') or
            IsLockFileHeld(RuntimeDir + 'service.lock');
end;

function EnsureCreatorHubStopped(): Boolean;
begin
  while IsCreatorHubRunning() do
  begin
    if MsgBox(CustomMessage('AppRunningPrompt'), mbConfirmation, MB_RETRYCANCEL) <> IDRETRY then
    begin
      Result := False;
      Exit;
    end;
  end;
  Result := True;
end;

function FormatBytes(const Bytes: Int64): String;
var
  Whole, Tenth: Int64;
begin
  if Bytes >= 1073741824 then
  begin
    Whole := Bytes div 1073741824;
    Tenth := ((Bytes mod 1073741824) * 10) div 1073741824;
    Result := Format('%d.%d GB', [Whole, Tenth]);
  end
  else
  begin
    Whole := Bytes div 1048576;
    Result := Format('%d MB', [Whole]);
  end;
end;

function IsDriveRoot(const PathValue: String): Boolean;
var
  Norm, Drive: String;
begin
  Norm := NormalizeDir(PathValue);
  Drive := ExtractFileDrive(Norm);
  Result := (Drive <> '') and (CompareText(Norm, AddBackslash(Drive)) = 0);
end;

function IsExistingCreatorHubDir(const DirPath: String): Boolean;
begin
  Result := FileExists(AddBackslash(DirPath) + 'CreatorHub.exe') or
            DirExists(AddBackslash(DirPath) + '_internal');
end;

function HasForeignEntries(const DirPath: String): Boolean;
var
  FindRec: TFindRec;
begin
  Result := False;
  if not DirExists(DirPath) or IsExistingCreatorHubDir(DirPath) then
    Exit;
  if FindFirst(AddBackslash(DirPath) + '*', FindRec) then
  begin
    try
      repeat
        if (FindRec.Name <> '.') and (FindRec.Name <> '..') then
        begin
          Result := True;
          Break;
        end;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

function IsRelocatingInstall(): Boolean;
begin
  Result := (PreviousInstallDir <> '') and not SameDir(WizardDirValue, PreviousInstallDir);
end;

procedure UpdateDirectoryControls();
var
  CurrentDir, ExistingBase, DriveName: String;
  FreeBytes, TotalBytes: Int64;
begin
  CurrentDir := Trim(WizardForm.DirEdit.Text);
  ResetDirButton.Enabled := not SameDir(CurrentDir, DefaultInstallDir());
  if AltDriveButton.Visible then
    AltDriveButton.Enabled := not SameDir(CurrentDir, SecondaryDriveDir);

  CleanOldDirCheck.Visible := (PreviousInstallDir <> '') and
    (CurrentDir <> '') and not SameDir(CurrentDir, PreviousInstallDir);

  ExistingBase := NearestExistingDir(CurrentDir);
  DriveName := Uppercase(ExtractFileDrive(CurrentDir));
  if (ExistingBase <> '') and GetSpaceOnDisk64(ExistingBase, FreeBytes, TotalBytes) then
  begin
    if FreeBytes >= MinRecommendedBytes then
    begin
      DriveSpaceLabel.Caption := FmtMessage(
        CustomMessage('DiskSpaceSufficient'), [DriveName, FormatBytes(FreeBytes)]);
      DriveSpaceLabel.Font.Color := $004E8A0D;
    end
    else
    begin
      DriveSpaceLabel.Caption := FmtMessage(
        CustomMessage('DiskSpaceLow'), [DriveName, FormatBytes(FreeBytes)]);
      DriveSpaceLabel.Font.Color := $000953B4;
    end;
  end
  else
  begin
    DriveSpaceLabel.Caption := CustomMessage('DiskSpaceUnknown');
    DriveSpaceLabel.Font.Color := $00665852;
  end;
end;

procedure DirEditChange(Sender: TObject);
begin
  UpdateDirectoryControls();
end;

procedure ResetDefaultDirClick(Sender: TObject);
begin
  WizardForm.DirEdit.Text := DefaultInstallDir();
  UpdateDirectoryControls();
end;

procedure SwitchSecondaryDirClick(Sender: TObject);
begin
  if SecondaryDriveDir <> '' then
  begin
    WizardForm.DirEdit.Text := SecondaryDriveDir;
    UpdateDirectoryControls();
  end;
end;

procedure RefreshWelcomeCopy();
var
  RuntimeLine: String;
begin
  if HasWebView2() then
    RuntimeLine := CustomMessage('WebView2StatusReady')
  else
    RuntimeLine := CustomMessage('WebView2StatusMissing');

  if PreviousInstallDir <> '' then
    WizardForm.WelcomeLabel2.Caption := FmtMessage(
      CustomMessage('WelcomeSubtitleUpgrade'), [PreviousInstallDir, RuntimeLine])
  else
    WizardForm.WelcomeLabel2.Caption := FmtMessage(
      CustomMessage('WelcomeSubtitleFresh'), [RuntimeLine]);
end;

procedure InitializeWizard();
var
  ContentWidth, LeftEdge, RowTop: Integer;
begin
  PreviousInstallDir := DetectPreviousInstallDir();
  SecondaryDriveDir := FindSecondaryDriveInstallDir();

  WizardForm.SelectDirBrowseLabel.Caption := CustomMessage('SelectDirSubtitle');

  LeftEdge := WizardForm.DirEdit.Left;
  ContentWidth := (WizardForm.DirBrowseButton.Left + WizardForm.DirBrowseButton.Width) - LeftEdge;
  RowTop := WizardForm.DirEdit.Top + WizardForm.DirEdit.Height + ScaleY(10);

  ResetDirButton := TNewButton.Create(WizardForm);
  ResetDirButton.Parent := WizardForm.SelectDirPage;
  ResetDirButton.Left := LeftEdge;
  ResetDirButton.Top := RowTop;
  ResetDirButton.Width := ScaleX(116);
  ResetDirButton.Height := WizardForm.DirBrowseButton.Height;
  ResetDirButton.Caption := CustomMessage('ResetDefaultDir');
  ResetDirButton.OnClick := @ResetDefaultDirClick;

  AltDriveButton := TNewButton.Create(WizardForm);
  AltDriveButton.Parent := WizardForm.SelectDirPage;
  AltDriveButton.Left := ResetDirButton.Left + ResetDirButton.Width + ScaleX(8);
  AltDriveButton.Top := RowTop;
  AltDriveButton.Width := ScaleX(172);
  AltDriveButton.Height := WizardForm.DirBrowseButton.Height;
  AltDriveButton.Visible := SecondaryDriveDir <> '';
  if SecondaryDriveDir <> '' then
    AltDriveButton.Caption := FmtMessage(CustomMessage('SwitchSecondaryDir'), [SecondaryDriveDir]);
  AltDriveButton.OnClick := @SwitchSecondaryDirClick;

  RowTop := ResetDirButton.Top + ResetDirButton.Height + ScaleY(12);
  DriveSpaceLabel := TNewStaticText.Create(WizardForm);
  DriveSpaceLabel.Parent := WizardForm.SelectDirPage;
  DriveSpaceLabel.Left := LeftEdge;
  DriveSpaceLabel.Top := RowTop;
  DriveSpaceLabel.Width := ContentWidth;
  DriveSpaceLabel.Height := ScaleY(20);
  DriveSpaceLabel.AutoSize := False;
  DriveSpaceLabel.WordWrap := False;
  DriveSpaceLabel.Anchors := [akLeft, akTop, akRight];

  RowTop := DriveSpaceLabel.Top + DriveSpaceLabel.Height + ScaleY(6);
  DataHintLabel := TNewStaticText.Create(WizardForm);
  DataHintLabel.Parent := WizardForm.SelectDirPage;
  DataHintLabel.Left := LeftEdge;
  DataHintLabel.Top := RowTop;
  DataHintLabel.Width := ContentWidth;
  DataHintLabel.Height := ScaleY(38);
  DataHintLabel.AutoSize := False;
  DataHintLabel.WordWrap := True;
  DataHintLabel.Font.Color := $00665852;
  DataHintLabel.Caption := FmtMessage(CustomMessage('DataIsolationHint'), [UserDataDir()]);
  DataHintLabel.Anchors := [akLeft, akTop, akRight];

  RowTop := DataHintLabel.Top + DataHintLabel.Height + ScaleY(6);
  CleanOldDirCheck := TNewCheckBox.Create(WizardForm);
  CleanOldDirCheck.Parent := WizardForm.SelectDirPage;
  CleanOldDirCheck.Left := LeftEdge;
  CleanOldDirCheck.Top := RowTop;
  CleanOldDirCheck.Width := ContentWidth;
  CleanOldDirCheck.Height := ScaleY(22);
  CleanOldDirCheck.Caption := CustomMessage('CleanOldDirOption');
  CleanOldDirCheck.Checked := True;
  CleanOldDirCheck.Visible := False;
  CleanOldDirCheck.Anchors := [akLeft, akTop, akRight];

  WizardForm.DirEdit.OnChange := @DirEditChange;
  RefreshWelcomeCopy();
  UpdateDirectoryControls();
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpWelcome then
    RefreshWelcomeCopy()
  else if CurPageID = wpSelectDir then
    UpdateDirectoryControls()
  else if CurPageID = wpFinished then
    WizardForm.FinishedLabel.Caption := FmtMessage(
      CustomMessage('FinishedSummary'), [ExpandConstant('{app}'), UserDataDir()]);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  SelectedDir, SuggestedDir, DataDir: String;
begin
  Result := True;
  if WizardSilent then
    Exit;

  if CurPageID = wpWelcome then
  begin
    Result := EnsureCreatorHubStopped();
    Exit;
  end;

  if CurPageID = wpSelectDir then
  begin
    SelectedDir := NormalizeDir(WizardForm.DirEdit.Text);
    if SelectedDir = '' then
      Exit;

    if IsDriveRoot(SelectedDir) then
    begin
      SuggestedDir := AddBackslash(SelectedDir) + 'CreatorHub';
      if MsgBox(FmtMessage(CustomMessage('DriveRootConfirm'), [SuggestedDir]),
        mbConfirmation, MB_YESNO) = IDYES then
      begin
        WizardForm.DirEdit.Text := SuggestedDir;
        SelectedDir := NormalizeDir(SuggestedDir);
        UpdateDirectoryControls();
      end
      else
      begin
        Result := False;
        Exit;
      end;
    end;

    DataDir := UserDataDir();
    if IsSubPathOrEqual(SelectedDir, DataDir) or IsSubPathOrEqual(DataDir, SelectedDir) then
    begin
      MsgBox(FmtMessage(CustomMessage('DataDirConflict'), [DataDir]), mbError, MB_OK);
      Result := False;
      Exit;
    end;

    if HasForeignEntries(SelectedDir) then
    begin
      SuggestedDir := AddBackslash(SelectedDir) + 'CreatorHub';
      if MsgBox(FmtMessage(CustomMessage('ForeignDirConfirm'), [SelectedDir, SuggestedDir]),
        mbConfirmation, MB_YESNO) = IDYES then
      begin
        WizardForm.DirEdit.Text := SuggestedDir;
        SelectedDir := NormalizeDir(SuggestedDir);
        UpdateDirectoryControls();
        if IsSubPathOrEqual(SelectedDir, DataDir) or IsSubPathOrEqual(DataDir, SelectedDir) then
        begin
          MsgBox(FmtMessage(CustomMessage('DataDirConflict'), [DataDir]), mbError, MB_OK);
          Result := False;
          Exit;
        end;
      end
      else
      begin
        Result := False;
        Exit;
      end;
    end;

    if not CanWriteTargetDir(SelectedDir) then
    begin
      MsgBox(FmtMessage(CustomMessage('DirNotWritable'), [SelectedDir]), mbError, MB_OK);
      Result := False;
      Exit;
    end;
  end;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo,
  MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
var
  Lines: String;
begin
  Lines := CustomMessage('ReadySectionMode') + NewLine;
  if PreviousInstallDir = '' then
    Lines := Lines + Space + CustomMessage('ReadyModeFresh') + NewLine
  else if SameDir(WizardDirValue, PreviousInstallDir) then
    Lines := Lines + Space + CustomMessage('ReadyModeUpgrade') + NewLine
  else
  begin
    Lines := Lines + Space + FmtMessage(CustomMessage('ReadyModeRelocate'), [PreviousInstallDir]) + NewLine;
    if CleanOldDirCheck.Checked then
      Lines := Lines + Space + CustomMessage('ReadyCleanOldYes') + NewLine
    else
      Lines := Lines + Space + CustomMessage('ReadyCleanOldNo') + NewLine;
  end;

  Lines := Lines + NewLine + CustomMessage('ReadySectionAppDir') + NewLine +
    Space + NormalizeDir(WizardDirValue) + NewLine;

  Lines := Lines + NewLine + CustomMessage('ReadySectionDataDir') + NewLine +
    Space + UserDataDir() + NewLine;

  Lines := Lines + NewLine + CustomMessage('ReadySectionRuntime') + NewLine;
  if HasWebView2() then
    Lines := Lines + Space + CustomMessage('WebView2StatusReady') + NewLine
  else
    Lines := Lines + Space + CustomMessage('WebView2StatusMissing') + NewLine;

  Lines := Lines + NewLine + CustomMessage('ReadySectionShortcuts') + NewLine +
    Space + CustomMessage('ReadyShortcutStartMenu');
  if WizardIsTaskSelected('desktopicon') then
    Lines := Lines + NewLine + Space + CustomMessage('ReadyShortcutDesktop');

  Result := Lines;
end;

procedure CleanupRelocatedOldInstall();
var
  OldDir: String;
  FindRec: TFindRec;
begin
  if WizardSilent or not IsRelocatingInstall() or not CleanOldDirCheck.Checked then
    Exit;
  OldDir := PreviousInstallDir;
  if (OldDir = '') or SameDir(OldDir, ExpandConstant('{app}')) or
     IsSubPathOrEqual(OldDir, UserDataDir()) or IsSubPathOrEqual(UserDataDir(), OldDir) then
    Exit;

  Log('Cleaning previous application files after relocation: ' + OldDir);
  DelTree(AddBackslash(OldDir) + '_internal', True, True, True);
  DeleteFile(AddBackslash(OldDir) + 'CreatorHub.exe');
  if FindFirst(AddBackslash(OldDir) + 'unins*.*', FindRec) then
  begin
    try
      repeat
        if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) = 0 then
          DeleteFile(AddBackslash(OldDir) + FindRec.Name);
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
  RemoveDir(OldDir);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    CleanupRelocatedOldInstall();
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  Attempt: Integer;
begin
  Result := '';
  if not WizardSilent and not EnsureCreatorHubStopped() then
  begin
    Result := CustomMessage('AppRunningPrompt');
    Exit;
  end;
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
  begin
    if not EnsureCreatorHubStopped() then
    begin
      Result := False;
      Exit;
    end;
    MsgBox(FmtMessage(CustomMessage('UninstallKeepData'), [ExpandConstant('{localappdata}\CreatorHub\user-data')]), mbInformation, MB_OK);
  end;
end;
