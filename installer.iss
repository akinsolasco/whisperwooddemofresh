[Setup]
AppId={{0F73DF40-CE7D-4E72-893E-00B0C3AE5B5D}
AppName=Enhanced Living Whisperwood Demo
AppVersion=2.2.5
AppPublisher=Enhanced Living Whisperwood Demo
DefaultDirName={localappdata}\Programs\Enhanced Living Whisperwood Demo
DefaultGroupName=Enhanced Living Whisperwood Demo
OutputDir=dist_installer
OutputBaseFilename=WhisperwoodDemoSetup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
UsePreviousAppDir=no
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=assets\enhanced_living_whisperwood_icon.ico
UninstallDisplayIcon={app}\WhisperwoodDemo.exe

[Files]
Source: "dist\WhisperwoodDemo\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "scripts\post_update_launcher.bat"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
Type: files; Name: "{autodesktop}\Whisperwood Demo.lnk"
Type: files; Name: "{autodesktop}\Whisperwood Villa.lnk"
Type: filesandordirs; Name: "{userprograms}\Whisperwood Villa"

[Icons]
Name: "{group}\Enhanced Living Whisperwood Demo"; Filename: "{app}\WhisperwoodDemo.exe"; IconFilename: "{app}\WhisperwoodDemo.exe"
Name: "{userdesktop}\Enhanced Living Whisperwood Demo"; Filename: "{app}\WhisperwoodDemo.exe"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\post_update_launcher.bat"; Flags: runhidden nowait; Check: IsSilentInstall
Filename: "{app}\WhisperwoodDemo.exe"; Description: "Launch Enhanced Living Whisperwood Demo"; Flags: nowait postinstall skipifsilent

[Code]
function IsSilentInstall: Boolean;
begin
  Result := WizardSilent();
end;
