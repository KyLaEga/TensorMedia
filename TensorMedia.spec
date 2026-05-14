# -*- mode: python ; coding: utf-8 -*-
import sys
import os
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None
base_path = os.path.abspath('.')

datas = [
    ('assets', 'assets'),
    ('models', 'models'),
]

# Исключаем полные бинарники PyTorch, полагаясь на анализ импортов, 
# собираем только необходимые конфигурации transformers и facenet.
datas += collect_data_files('transformers')
datas += collect_data_files('facenet_pytorch')

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
    'fitz',
    'pymupdf',
    'blake3',
    'send2trash',
    'psutil',
    'transformers.models.siglip',
    'facenet_pytorch',
]

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
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    # КРИТИЧНО для macOS ARM64 (Apple Silicon): '-' означает Ad-Hoc подпись
    codesign_identity='-' if sys.platform == 'darwin' else None,
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