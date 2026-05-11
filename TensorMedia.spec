# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

# ФИКС NPU: Официальный хук PyInstaller скопирует данные facenet внутрь его же модуля
facenet_datas = collect_data_files('facenet_pytorch')

EXCLUDES = [
    'matplotlib', 'scipy', 'tensorboard', 'tkinter', 'PyQt5', 'PyQt6', 'wx', 
    'jupyter', 'notebook', 'IPython', 'pandas.tests', 'numpy.random._examples'
]

# ФИКС WINDOWS: Оставляем PySide6 здесь, PyInstaller сам создаст правильный qt.conf
HIDDEN_IMPORTS = [
    'torchvision', 'facenet_pytorch', 'faiss', 'safetensors',
    'PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets', 'PySide6.QtMultimedia',
    'shiboken6', 'cv2'
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[], # Обязательно пусто, иначе сломаются DLL на Windows
    datas=[
        ('models/siglip-base-patch16-224', 'models/siglip-base-patch16-224'),
        ('models/torch', 'models/torch')
    ] + facenet_datas,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
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
    codesign_identity=None,
    entitlements_file=None,
    icon=None
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='TensorMedia' 
)

app = BUNDLE(
    coll,
    name='TensorMedia.app',
    icon=None,
    bundle_identifier='com.tensormedia.app',
)