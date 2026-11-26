; PoruwaCut Windows installer (Inno Setup 6.3+)
;   iscc /DMyVersion=0.2.0 build\installer.iss
; Per-user install (no admin prompt) so auto-updates can run silently.

#ifndef MyVersion
  #define MyVersion "0.0.0"
#endif

[Setup]
AppId={{8F3C2A51-6B7E-4D2A-9C1F-5E0A7B4D2C91}
AppName=AutoWed
AppVersion={#MyVersion}
AppVerName=AutoWed {#MyVersion}
AppPublisher=Endora
DefaultDirName={localappdata}\Programs\PoruwaCut
DisableDirPage=auto
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\Output
OutputBaseFilename=AutoWed_Setup_{#MyVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\engine\PoruwaCutEngine.exe
UninstallDisplayName=AutoWed {#MyVersion} (Endora)
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no

[Messages]
FinishedLabel=AutoWed installed. Powered by Endora.%n%nPremiere Pro: Window > UXP Plugins > AutoWed%nDaVinci Resolve Studio: Workspace > Workflow Integrations > Endora Highlights%n%n(If Premiere or Resolve was open, restart it.)

[InstallDelete]
; remove the previous engine build completely so old libraries never mix with new ones
Type: filesandordirs; Name: "{app}\engine"
; shortcuts from the PoruwaCut-named versions (now AutoWed)
Type: filesandordirs; Name: "{userprograms}\PoruwaCut"
Type: files; Name: "{userstartup}\PoruwaCut Engine.lnk"

[Files]
Source: "..\dist\PoruwaCutEngine\*"; DestDir: "{app}\engine"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "ffmpeg\ffmpeg.exe"; DestDir: "{app}\engine\ffmpeg"; Flags: ignoreversion
Source: "ffmpeg\ffprobe.exe"; DestDir: "{app}\engine\ffmpeg"; Flags: ignoreversion
Source: "ffmpeg\LICENSE.txt"; DestDir: "{app}\engine\ffmpeg"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\dist\PoruwaCut.ccx"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\AutoWedMCP.exe"; DestDir: "{app}\mcp"; Flags: ignoreversion
Source: "..\dist\AutoWed.mcpb"; DestDir: "{app}\mcp"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\resolve-plugin\*"; DestDir: "{app}\resolve-plugin"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\AutoWed\AutoWed Engine"; Filename: "{app}\engine\PoruwaCutEngine.exe"
Name: "{userprograms}\AutoWed\AutoWed in browser"; Filename: "http://127.0.0.1:7788"
Name: "{userprograms}\AutoWed\Reinstall Premiere plugin (ccx)"; Filename: "{app}\PoruwaCut.ccx"
Name: "{userprograms}\AutoWed\Read me"; Filename: "{app}\README.md"
Name: "{userprograms}\AutoWed\Install AutoWed for Claude (extension)"; Filename: "{app}\mcp\AutoWed.mcpb"
Name: "{userstartup}\AutoWed Engine"; Filename: "{app}\engine\PoruwaCutEngine.exe"

[Run]
; 1) Premiere plugin through Adobe's own installer agent (silent, also on updates)
Filename: "{code:UpiaPath}"; Parameters: "/install ""{app}\PoruwaCut.ccx"""; StatusMsg: "Installing the Premiere Pro plugin..."; Flags: runhidden waituntilterminated; Check: UpiaExists
; 1b) Claude MCP bridge: add "autowed-premiere" to Claude desktop's config (if Claude desktop is installed)
Filename: "{app}\mcp\AutoWedMCP.exe"; Parameters: "--register"; Flags: runhidden waituntilterminated
; 2) start the engine (also after a silent auto-update)
Filename: "{app}\engine\PoruwaCutEngine.exe"; Flags: nowait
; 3) optional fallback: hand the .ccx to Creative Cloud / ZXP-UXP Installer
Filename: "{app}\PoruwaCut.ccx"; Description: "Open the plugin file (.ccx) - only if AutoWed is missing in Premiere"; Flags: shellexec postinstall skipifsilent unchecked

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM PoruwaCutEngine.exe"; Flags: runhidden; RunOnceId: "KillEngine"
Filename: "{app}\mcp\AutoWedMCP.exe"; Parameters: "--unregister"; Flags: runhidden; RunOnceId: "UnregMCP"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM AutoWedMCP.exe"; Flags: runhidden; RunOnceId: "KillMCP"
Filename: "{code:UpiaPath}"; Parameters: "/remove ""AutoWed"""; Flags: runhidden; Check: UpiaExists; RunOnceId: "RemovePlugin"
Filename: "{code:UpiaPath}"; Parameters: "/remove ""PoruwaCut"""; Flags: runhidden; Check: UpiaExists; RunOnceId: "RemovePluginOld"

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
  { Claude desktop keeps the MCP bridge running; stop it so the file can be replaced (Claude restarts it) }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM AutoWedMCP.exe', '', SW_HIDE, ewWaitUntilTerminated, rc);
  Sleep(800);
  Result := '';
end;

{ DaVinci Resolve (Studio): Endora Highlights panel -> Workspace > Workflow Integrations.
  Copied with code (not [Files]) so a locked / protected folder never stops an AutoWed update. }
procedure InstallResolvePlugin();
var
  Src, Dst, Node: String;
  Names: array of String;
  i: Integer;
begin
  if not DirExists(ExpandConstant('{commonappdata}\Blackmagic Design\DaVinci Resolve')) then
  begin
    Log('DaVinci Resolve not found - Endora Highlights panel not installed');
    exit;
  end;
  Src := ExpandConstant('{app}\resolve-plugin\');
  Dst := ExpandConstant('{commonappdata}\Blackmagic Design\DaVinci Resolve\Support\Workflow Integration Plugins\Endora Highlights\');
  if not ForceDirectories(Dst) then
  begin
    Log('Cannot create ' + Dst);
    exit;
  end;
  SetArrayLength(Names, 6);
  Names[0] := 'manifest.xml'; Names[1] := 'package.json'; Names[2] := 'main.js';
  Names[3] := 'preload.js'; Names[4] := 'index.html'; Names[5] := 'renderer.js';
  for i := 0 to GetArrayLength(Names) - 1 do
    if not FileCopy(Src + Names[i], Dst + Names[i], False) then
      Log('Copy failed: ' + Names[i]);
  Node := ExpandConstant('{commonappdata}\Blackmagic Design\DaVinci Resolve\Support\Developer\Workflow Integrations\Examples\SamplePlugin\WorkflowIntegration.node');
  if FileExists(Node) then
    FileCopy(Node, Dst + 'WorkflowIntegration.node', False)
  else
    Log('WorkflowIntegration.node not found (Resolve Studio needed)');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    InstallResolvePlugin();
  if (CurStep = ssPostInstall) and (not UpiaExists) and (not WizardSilent) then
    MsgBox('Creative Cloud plugin installer was not found.' + #13#10 +
           'Install the Premiere plugin by double-clicking:' + #13#10 +
           ExpandConstant('{app}\PoruwaCut.ccx') + #13#10 +
           '(or drag it onto the aescripts ZXP/UXP Installer).', mbInformation, MB_OK);
end;
