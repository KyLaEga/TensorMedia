# -*- mode: python ; coding: utf-8 -*-
import os
import facenet_pytorch
from PyInstaller.utils.hooks import collect_submodules, collect_dynamic_libs

facenet_path = os.path.dirname(facenet_pytorch.__file__)
pyside_binaries = collect_dynamic_libs('PySide6')

project_datas = [
    (os.path.join(facenet_path, 'data'), 'facenet_pytorch/data'),
]

# Захват моделей, загруженных в CI
for folder in ['models', 'assets', 'resources']:
    if os.path.exists(folder):
        project_datas.append((folder, folder))

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=pyside_binaries,
    datas=project_datas,
    hiddenimports=collect_submodules('PySide6') + [
        'torchvision', 'facenet_pytorch', 'faiss', 'safetensors', 'shiboken6', 'cv2', 'transformers'
    ],
    excludes=['matplotlib', 'scipy', 'tkinter'],
    cipher=None,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)
exe = EXE(
    pyz, a.scripts, [],
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
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, name='TensorMedia')
app = BUNDLE(coll, name='TensorMedia.app', bundle_identifier='com.tensormedia.app')