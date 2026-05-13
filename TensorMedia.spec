# -*- mode: python ; coding: utf-8 -*-
import sys
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# Определяем базовый путь
base_path = os.path.abspath('.')

# Собираем данные
datas = [
    ('assets', 'assets'),
    ('models', 'models'),
]

# Добавляем специфичные для библиотек данные
datas += collect_data_files('torch')
datas += collect_data_files('transformers')
datas += collect_data_files('facenet_pytorch')

# Скрытые импорты
hiddenimports = [
    'PySide6.QtCore',
    'PySide6.QtGui',
    'PySide6.QtWidgets',
    'PySide6.QtMultimedia',
    'PySide6.QtMultimediaWidgets',
    'torch',
    'numpy',
    'cv2',
    'PIL.Image',
    'fitz', # PyMuPDF
    'pymupdf',
    'blake3',
    'send2trash',
    'psutil',
    'transformers.models.siglip',
    'facenet_pytorch',
]

# Исключаем ненужные модули для уменьшения размера
excludes = [
    'tkinter',
    'unittest',
    'pytest',
    'matplotlib',
    'notebook',
    'jupyter',
]

a = Analysis(
    ['main.py'],
    pathex=[base_path],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TensorMedia',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False, # Скрываем консоль для GUI приложения
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file='entitlements.plist' if sys.platform == 'darwin' else None,
    icon='assets/icons/app.ico' if os.path.exists('assets/icons/app.ico') else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='TensorMedia',
)

if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='TensorMedia.app',
        icon='assets/icons/app.icns' if os.path.exists('assets/icons/app.icns') else None,
        bundle_identifier='com.tensormedia.arbitrage',
        info_plist={
            'NSHighResolutionCapable': 'True',
            'LSBackgroundOnly': 'False',
            'NSRequiresAquaSystemAppearance': 'False',
            'CFBundleShortVersionString': '1.0.0',
        },
    )
