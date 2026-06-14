#ifndef MyAppVersion
  #define MyAppVersion "2.3.4"
#endif

#define MyAppName "Twitch Stream Bot"
#define MyAppPublisher "prophews"
#define MyAppURL "https://github.com/prophews/Twitch-Stream-Bot"
#define MyAppExeName "Twitch Stream Bot.exe"

[Setup]
AppId={{7CA939E7-B87C-47D5-9038-09B4E50A3789}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases/latest
DefaultDirName={localappdata}\Programs\Twitch Stream Bot
DefaultGroupName=Twitch Stream Bot
DisableDirPage=auto
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
CreateUninstallRegKey=no
UpdateUninstallLogAppName=no
OutputDir=dist_public
OutputBaseFilename=Twitch Stream Bot App Update {#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "dist_public\Twitch Stream Bot\Twitch Stream Bot.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist_public\Twitch Stream Bot\_internal\*"; DestDir: "{app}\_internal"; Excludes: "ffmpeg.exe,ffprobe.exe"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: files; Name: "{app}\Twitch Stream Bot 2.0.exe"

[Icons]
Name: "{autoprograms}\Twitch Stream Bot"; Filename: "{app}\{#MyAppExeName}"
Name: "{autoprograms}\Twitch Stream Bot User Guide"; Filename: "https://github.com/prophews/Twitch-Stream-Bot/blob/main/docs/USER_GUIDE.md"
Name: "{autoprograms}\Check for Twitch Stream Bot Updates"; Filename: "https://github.com/prophews/Twitch-Stream-Bot/releases/latest"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Twitch Stream Bot"; Flags: nowait postinstall skipifsilent

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  FFmpegPath: String;
  FFprobePath: String;
begin
  FFmpegPath := ExpandConstant('{app}\_internal\ffmpeg.exe');
  FFprobePath := ExpandConstant('{app}\_internal\ffprobe.exe');

  if (not FileExists(FFmpegPath)) or (not FileExists(FFprobePath)) then
  begin
    Result :=
      'The app-only update requires an existing full Twitch Stream Bot installation.' +
      Chr(13) + Chr(10) + Chr(13) + Chr(10) +
      'Install the full Twitch Stream Bot Setup package first.';
    exit;
  end;

  Result := '';
end;
