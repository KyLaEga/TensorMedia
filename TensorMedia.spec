# -*- mode: python ; coding: utf-8 -*-
import os
import facenet_pytorch

block_cipher = None

# Физический путь к facenet_pytorch для извлечения pnet.pt
facenet_path = os.path.dirname(facenet_pytorch.__file__)

EXCLUDES = [
    'matplotlib', 'scipy', 'tensorboard', 'tkinter', 'PyQt5', 'PyQt6', 'wx', 
    'jupyter', 'notebook', 'IPython', 'pandas.tests', 'numpy.random._examples'
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        # Твои старые пути
        ('models/siglip-base-patch16-224', 'models/siglip-base-patch16-224'),
        ('models/torch', 'models/torch'),
        # ФИКС: Явный перенос весов facenet в бандл
        (os.path.join(facenet_path, 'data'), 'facenet_pytorch/data')
    ],
    hiddenimports=[
        'torchvision', 'facenet_pytorch', 'faiss', 'safetensors',
        'shiboken6', 'PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets'
    ],
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