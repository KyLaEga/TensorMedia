[Setup]
AppName=TensorMedia
AppVersion=1.0.0
DefaultDirName={autopf}\TensorMedia
DefaultGroupName=TensorMedia
OutputDir=dist
OutputBaseFilename=TensorMedia_Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=lowest

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "dist\TensorMedia\TensorMedia.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\TensorMedia\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\TensorMedia"; Filename: "{app}\TensorMedia.exe"
Name: "{autodesktop}\TensorMedia"; Filename: "{app}\TensorMedia.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\TensorMedia.exe"; Description: "Launch TensorMedia"; Flags: nowait postinstall skipifsilent