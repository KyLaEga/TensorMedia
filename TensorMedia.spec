# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_submodules, collect_dynamic_libs, collect_data_files

block_cipher = None

# 1. Жесткий захват DLL для Windows
pyside_hidden = collect_submodules('PySide6')
pyside_binaries = collect_dynamic_libs('PySide6')

# 2. Извлечение скрытых весов нейросетей (pnet.pt, rnet.pt)
facenet_datas = collect_data_files('facenet_pytorch')

# 3. Инъекция пользовательских моделей
# Компилятор сам найдет папку models и запакует ее внутрь релиза
project_datas = facenet_datas
for folder in ['models', 'assets', 'resources', 'core/ml/models']:
    if os.path.exists(folder):
        project_datas.append((folder, folder))

EXCLUDES = ['matplotlib', 'scipy', 'tensorboard', 'tkinter', 'PyQt5', 'PyQt6', 'wx', 'jupyter']

HIDDEN_IMPORTS = [
    'torchvision', 'facenet_pytorch', 'faiss', 'safetensors',
    'core.services.fs_service', 'core.services.auto_selector',
    'core.ml.cluster_engine', 'core.profiler',
    'shiboken6', 'fitz', 'cv2', 'transformers'
] + pyside_hidden

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=pyside_binaries, # ФИКС ДЛЯ WINDOWS: возвращаем графические библиотеки
    datas=project_datas,      # ФИКС ДЛЯ MACOS NPU: пакуем папки с моделями
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