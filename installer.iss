#ifndef MyAppVersion
  #define MyAppVersion "2.3.0"
#endif

#define MyAppName "Twitch Stream Bot"
#define MyAppPublisher "prophews"
#define MyAppURL "https://github.com/prophews/Twitch-Stream-Bot"
#define MyAppExeName "Twitch Stream Bot 2.0.exe"

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
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist_public
OutputBaseFilename=Twitch Stream Bot Setup {#MyAppVersion}
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

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "dist_public\Twitch Stream Bot 2.0\Twitch Stream Bot 2.0.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist_public\Twitch Stream Bot 2.0\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Twitch Stream Bot"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\Twitch Stream Bot"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{autoprograms}\Twitch Stream Bot User Guide"; Filename: "https://github.com/prophews/Twitch-Stream-Bot/blob/main/docs/USER_GUIDE.md"
Name: "{autoprograms}\Check for Twitch Stream Bot Updates"; Filename: "https://github.com/prophews/Twitch-Stream-Bot/releases/latest"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Twitch Stream Bot"; Flags: nowait postinstall skipifsilent
