# -*- mode: python ; coding: utf-8 -*-
import os
import facenet_pytorch
from PyInstaller.utils.hooks import collect_submodules, collect_dynamic_libs, collect_data_files

block_cipher = None

# 1. Находим физический путь к библиотеке facenet_pytorch для извлечения весов (.pt)
facenet_path = os.path.dirname(facenet_pytorch.__file__)

# 2. Собираем все необходимые данные
pyside_hidden = collect_submodules('PySide6')
pyside_binaries = collect_dynamic_libs('PySide6')

# Формируем список данных для упаковки
project_datas = [
    # Копируем веса facenet строго туда, где библиотека ожидает их увидеть
    (os.path.join(facenet_path, 'data'), 'facenet_pytorch/data'),
]

# Автоматический захват ваших моделей и ассетов
for folder in ['models', 'assets', 'resources', 'core/ml/models']:
    if os.path.exists(folder):
        # В бандле папки должны лежать в корне рядом с exe/бинарником
        project_datas.append((folder, folder))

HIDDEN_IMPORTS = [
    'torchvision', 'facenet_pytorch', 'faiss', 'safetensors',
    'shiboken6', 'fitz', 'cv2', 'transformers', 'tokenizers'
] + pyside_hidden

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=pyside_binaries,
    datas=project_datas,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'scipy', 'tkinter'],
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