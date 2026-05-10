# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# Собираем только логику PySide6, бинарники PyInstaller подтянет своими хуками
pyside_hidden = collect_submodules('PySide6')

EXCLUDES = ['matplotlib', 'scipy', 'tensorboard', 'tkinter', 'PyQt5', 'PyQt6', 'wx', 'jupyter']

HIDDEN_IMPORTS = [
    'torchvision', 'facenet_pytorch', 'faiss', 'safetensors',
    'core.services.fs_service', 'core.services.auto_selector',
    'core.ml.cluster_engine', 'core.profiler',
    'shiboken6', 'fitz', 'cv2'
] + pyside_hidden

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[], # Очищено, предотвращает дублирование и порчу qt.conf
    datas=[],
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
    entitlements_file='entitlements.plist',
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