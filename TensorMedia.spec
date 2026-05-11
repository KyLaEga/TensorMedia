# -*- mode: python ; coding: utf-8 -*-
import os
import facenet_pytorch
from PyInstaller.utils.hooks import collect_submodules, collect_dynamic_libs

block_cipher = None

facenet_path = os.path.dirname(facenet_pytorch.__file__)
pyside_binaries = collect_dynamic_libs('PySide6')

project_datas = [
    # Фикс NPU: забираем веса facenet (pnet.pt) из виртуальной среды
    (os.path.join(facenet_path, 'data'), 'facenet_pytorch/data'),
]

# Захват папок моделей, которые скачает CI
for folder in ['models', 'assets', 'resources', 'core/ml/models']:
    if os.path.exists(folder):
        project_datas.append((folder, folder))

HIDDEN_IMPORTS = [
    'torchvision', 'facenet_pytorch', 'faiss', 'safetensors',
    'shiboken6', 'fitz', 'cv2', 'transformers', 'tokenizers'
] + collect_submodules('PySide6')

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=pyside_binaries,
    datas=project_datas,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'scipy', 'tkinter', 'jupyter'],
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