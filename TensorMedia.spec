# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# Tree Shaking: Исключение избыточных библиотек для минимизации артефакта
EXCLUDES = [
    'matplotlib', 'scipy', 'tensorboard', 'tkinter', 'PyQt5', 'PyQt6', 'wx', 
    'jupyter', 'notebook', 'IPython', 'pandas.tests', 'numpy.random._examples'
]

# HOTFIX: Явное указание динамически загружаемых микросервисов и графики Windows (Фаза 4)
HIDDEN_IMPORTS = [
    'torchvision', 
    'facenet_pytorch', 
    'faiss', 
    'safetensors',
    'core.services.fs_service',
    'core.services.auto_selector',
    'core.ml.cluster_engine',
    'core.profiler',
    'PySide6.QtMultimedia',
    'PySide6.QtMultimediaWidgets',
    'PySide6.QtWidgets',
    'PySide6.QtGui',
    'PySide6.QtCore',
    'shiboken6',
    'fitz',
    'cv2'
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
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

# MAC OS BUNDLE INSTRUCTION
app = BUNDLE(
    coll,
    name='TensorMedia.app',
    icon=None,
    bundle_identifier='com.tensormedia.app',
)