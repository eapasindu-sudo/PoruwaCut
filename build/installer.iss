; PoruwaCut Windows installer (Inno Setup 6.3+)
;   iscc /DMyVersion=0.2.0 build\installer.iss
; Per-user install (no admin prompt) so auto-updates can run silently.

#ifndef MyVersion
  #define MyVersion "0.0.0"
#endif

[Setup]
AppId={{8F3C2A51-6B7E-4D2A-9C1F-5E0A7B4D2C91}
AppName=PoruwaCut
AppVersion={#MyVersion}
AppVerName=PoruwaCut {#MyVersion}
AppPublisher=PoruwaCut
DefaultDirName={localappdata}\Programs\PoruwaCut
DisableDirPage=auto
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\Output
OutputBaseFilename=PoruwaCut_Setup_{#MyVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\engine\PoruwaCutEngine.exe
UninstallDisplayName=PoruwaCut {#MyVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no

[Messages]
FinishedLabel=PoruwaCut installed.%n%nOpen Premiere Pro and go to:%nWindow > UXP Plugins > PoruwaCut Multicam%n%n(If Premiere was open, restart it.)

[InstallDelete]
; remove the previous engine build completely so old libraries never mix with new ones
Type: filesandordirs; Name: "{app}\engine"

[Files]
Source: "..\dist\PoruwaCutEngine\*"; DestDir: "{app}\engine"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "ffmpeg\ffmpeg.exe"; DestDir: "{app}\engine\ffmpeg"; Flags: ignoreversion
Source: "ffmpeg\ffprobe.exe"; DestDir: "{app}\engine\ffmpeg"; Flags: ignoreversion
Source: "ffmpeg\LICENSE.txt"; DestDir: "{app}\engine\ffmpeg"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\dist\PoruwaCut.ccx"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\PoruwaCut\PoruwaCut Engine"; Filename: "{app}\engine\PoruwaCutEngine.exe"
Name: "{userprograms}\PoruwaCut\PoruwaCut in browser"; Filename: "http://127.0.0.1:7788"
Name: "{userprograms}\PoruwaCut\Reinstall Premiere plugin (ccx)"; Filename: "{app}\PoruwaCut.ccx"
Name: "{userprograms}\PoruwaCut\Read me"; Filename: "{app}\README.md"
Name: "{userstartup}\PoruwaCut Engine"; Filename: "{app}\engine\PoruwaCutEngine.exe"

[Run]
; 1) Premiere plugin through Adobe's own installer agent (silent, also on updates)
Filename: "{code:UpiaPath}"; Parameters: "/install ""{app}\PoruwaCut.ccx"""; StatusMsg: "Installing the Premiere Pro plugin..."; Flags: runhidden waituntilterminated; Check: UpiaExists
; 2) start the engine (also after a silent auto-update)
Filename: "{app}\engine\PoruwaCutEngine.exe"; Flags: nowait
; 3) optional fallback: hand the .ccx to Creative Cloud / ZXP-UXP Installer
Filename: "{app}\PoruwaCut.ccx"; Description: "Open the plugin file (.ccx) - only if PoruwaCut is missing in Premiere"; Flags: shellexec postinstall skipifsilent unchecked

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM PoruwaCutEngine.exe"; Flags: runhidden; RunOnceId: "KillEngine"
Filename: "{code:UpiaPath}"; Parameters: "/remove ""PoruwaCut"""; Flags: runhidden; Check: UpiaExists; RunOnceId: "RemovePlugin"

[Code]
function UpiaPath(Param: String): String;
begin
  Result := ExpandConstant('{commoncf}\Adobe\Adobe Desktop Common\RemoteComponents\UPI\UnifiedPluginInstallerAgent\UnifiedPluginInstallerAgent.exe');
end;

function UpiaExists: Boolean;
begin
  Result := FileExists(UpiaPath(''));
  if not Result then
    Log('UPIA not found - the user installs PoruwaCut.ccx by double-click or ZXP/UXP Installer');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  rc: Integer;
begin
  { stop a running engine so its files can be replaced }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM PoruwaCutEngine.exe', '', SW_HIDE, ewWaitUntilTerminated, rc);
  Sleep(800);
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and (not UpiaExists) and (not WizardSilent) then
    MsgBox('Creative Cloud plugin installer was not found.' + #13#10 +
           'Install the Premiere plugin by double-clicking:' + #13#10 +
           ExpandConstant('{app}\PoruwaCut.ccx') + #13#10 +
           '(or drag it onto the aescripts ZXP/UXP Installer).', mbInformation, MB_OK);
end;
