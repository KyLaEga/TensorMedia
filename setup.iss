; Версия — единый источник = git-тег. CI передаёт её из тега:
;   iscc /DMyAppVersion=1.2.4 setup.iss
; Дефолт ниже держим равным текущему релизу, чтобы ручная сборка без /D была корректной.
#ifndef MyAppVersion
  #define MyAppVersion "1.2.4"
#endif

[Setup]
AppName=TensorMedia
AppVersion={#MyAppVersion}
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