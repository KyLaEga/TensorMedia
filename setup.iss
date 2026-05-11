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
; ТРЕБУЕТ ПРАВА АДМИНА: это обеспечит нормальную установку в Program Files
PrivilegesRequired=admin
; РАЗРЕШИТЬ ВЫБОР ПАПКИ
DisableDirPage=no
; ВКЛЮЧИТЬ УДАЛЕНИЕ
UninstallDisplayIcon={app}\TensorMedia.exe
UninstallDisplayName=TensorMedia

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Дополнительно:"; Flags: unchecked

[Files]
; Рекурсивный захват всех DLL и моделей нейросетей
Source: "dist\TensorMedia\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\TensorMedia"; Filename: "{app}\TensorMedia.exe"
Name: "{autodesktop}\TensorMedia"; Filename: "{app}\TensorMedia.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\TensorMedia.exe"; Description: "Запустить TensorMedia"; Flags: nowait postinstall skipifsilent