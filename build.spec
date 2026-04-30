# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# Сбор скрытых зависимостей для корректной работы моделей
hidden_imports = []
hidden_imports += collect_submodules('transformers')
hidden_imports += collect_submodules('facenet_pytorch')
hidden_imports += ['faiss']

# Исключение лишних библиотек для снижения веса бинарника
excluded_modules = [
    'tkinter', 'matplotlib', 'scipy', 'notebook', 'IPython', 
    'jupyter', 'pytest', 'PySide6', 'PyQt5'
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded_modules,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TensorArbitrage',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True, # Использовать сжатие UPX (если установлено в системе)
    console=False, # True для отладки
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/icons/app_icon.icns' # Для Windows использовать .ico
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='TensorArbitrage',
)

# Специфично для macOS (ARM64)
app = BUNDLE(
    coll,
    name='TensorArbitrage.app',
    icon='assets/icons/app_icon.icns',
    bundle_identifier='com.tensor.arbitrage',
)