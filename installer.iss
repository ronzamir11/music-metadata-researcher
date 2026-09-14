#define MyAppName "Music Metadata Researcher"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "Ron Zamir"
#define MyAppExeName "MusicMetadataResearcher.exe"

[Setup]
AppId={{B8E19D9C-5E6B-4B35-9F8A-4B0A2A1F5A41}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Music Metadata Researcher
DefaultGroupName={#MyAppName}
OutputDir=installer-output
OutputBaseFilename=MusicMetadataResearcher-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "dist\MusicMetadataResearcher.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
