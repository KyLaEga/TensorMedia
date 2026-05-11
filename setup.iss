[Setup]
AppName=TensorMedia
AppVersion=1.0.0
DefaultDirName={commonpf}\TensorMedia
DefaultGroupName=TensorMedia
OutputDir=dist
OutputBaseFilename=TensorMedia_Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=admin
DisableDirPage=no
UninstallDisplayName=TensorMedia (Удалить)

[Files]
Source: "dist\TensorMedia\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\TensorMedia"; Filename: "{app}\TensorMedia.exe"
Name: "{autodesktop}\TensorMedia"; Filename: "{app}\TensorMedia.exe"

[Run]
Filename: "{app}\TensorMedia.exe"; Description: "Запустить TensorMedia"; Flags: nowait postinstall skipifsilent