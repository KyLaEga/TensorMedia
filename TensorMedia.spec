# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules
import os

block_cipher = None

pyside_binaries = collect_dynamic_libs('PySide6')
pyside_hidden = collect_submodules('PySide6')
facenet_datas = collect_data_files('facenet_pytorch')

project_datas = [
    ('models/siglip-base-patch16-224', 'models/siglip-base-patch16-224'),
    ('models/torch', 'models/torch')
] + facenet_datas

EXCLUDES = [
    'matplotlib', 'scipy', 'tensorboard', 'tkinter', 'PyQt5', 'PyQt6', 'wx', 
    'jupyter', 'notebook', 'IPython', 'pandas.tests', 'numpy.random._examples'
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=pyside_binaries,
    datas=project_datas,
    hiddenimports=[
        'torchvision', 'faiss', 'safetensors', 'shiboken6'
    ] + pyside_hidden,
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