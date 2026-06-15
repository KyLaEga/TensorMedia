# -*- mode: python ; coding: utf-8 -*-
#
# Кросс-платформенный spec для PyInstaller 6.x (macOS / Windows / Linux).
# ВАЖНО: НЕ перегенерировать этот файл командой `pyinstaller main.py` —
# дефолтный autogen затирает ВСЕ настройки ниже (entitlements, hiddenimports,
# collect_data_files, upx=False, bundle_identifier, info_plist), после чего
# .app падает при старте (PyTorch JIT / FAISS память / отсутствующие модули).
import sys
import os
from PyInstaller.utils.hooks import collect_data_files, collect_all, copy_metadata

IS_DARWIN = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

# --- Данные, попадающие внутрь бандла ---------------------------------------
datas = [
    ("assets", "assets"),
]
# Linux-CPU дистрибутив ОБЯЗАН укладываться в лимит GitHub Releases (2 ГБ):
# веса моделей (~480 МБ SigLIP + FaceNet) в бандл НЕ кладём — они скачиваются
# при первом запуске в ~/.local/share/TensorMedia/models (см. weight_manager /
# env_config.get_models_dir). macOS/Windows остаются полностью офлайновыми.
if not IS_LINUX and os.path.isdir("models"):
    datas.append(("models", "models"))

# --- transformers: ПОЛНЫЙ сбор (RCA фатального краша SigLIP в бандле) --------
# Ленивый импорт transformers резолвит SiglipVisionModel через строковые имена
# модулей (importlib), которые статический анализатор PyInstaller не видит, а
# проверка is_torch_available() читает ВЕРСИЮ torch через importlib.metadata —
# т.е. требует папку dist-info, которую PyInstaller по умолчанию не кладёт.
# Без метаданных transformers считает, что torch «не установлен», и валит
# рантайм с "SiglipVisionModel requires the PyTorch library but it was not
# found..." при живом, физически присутствующем torch.
_tf_datas, _tf_binaries, _tf_hidden = collect_all("transformers")
datas += _tf_datas
extra_binaries = list(_tf_binaries)
datas += collect_data_files("facenet_pytorch")

# --- certifi: bundle the CA store so the ML net contour can verify TLS --------
# huggingface_hub/requests resolve the trust store via certifi.where(). Frozen,
# that path must point INSIDE the bundle or weight download (Linux first-run,
# weight_manager.snapshot_download) dies with CERTIFICATE_VERIFY_FAILED. The
# stdlib `ssl` default verify-path is OpenSSL's compiled-in CApath
# (/opt/homebrew/etc/openssl@3 on the build box) which is absent on end-user
# machines — see runtime hook pyi_rth_ssl_certs.py which redirects SSL_CERT_FILE
# to this bundled cacert.pem.
datas += collect_data_files("certifi")

# --- faiss: collect swig binary + libfaiss.dylib + SIMD-variant loader --------
# faiss/loader.py selects a kernel via runtime try/except imports
# (swigfaiss_avx512_spr/avx512/avx2/sve -> generic swigfaiss). PyInstaller's
# static graph sees `import faiss` (core/ml/faiss_manager.py) but not those
# string-conditional submodules nor the sibling libfaiss.dylib, so collect_all
# pulls binaries+data+hidden explicitly (on arm64 only the generic kernel
# exists; collect_all enumerates real files, so no phantom AVX hiddenimports).
_fa_datas, _fa_binaries, _fa_hidden = collect_all("faiss")
datas += _fa_datas
extra_binaries += _fa_binaries

# dist-info для всех пакетов, чьи версии transformers/huggingface_hub опрашивают
# в рантайме через importlib.metadata (отсутствие любого -> ложный "not found").
for _pkg in (
    "torch", "transformers", "safetensors", "tokenizers", "huggingface-hub",
    "numpy", "tqdm", "regex", "requests", "packaging", "filelock", "pyyaml",
    "certifi",
):
    try:
        datas += copy_metadata(_pkg)
    except Exception:
        pass  # пакет может называться иначе в конкретном окружении — не фатально

# --- Модули, которые PyInstaller не видит статически -------------------------
hiddenimports = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtSvg",        # SVG image-handler: без него иконки плеера (play/
                            # pause/mute) рендерятся пустой областью в бандле
    "shiboken6",            # критично для инициализации PySide6 (особенно Windows)
    "torch",
    "safetensors",
    "numpy",
    "cv2",
    "PIL.Image",
    "fitz",
    "pymupdf",
    "blake3",
    "send2trash",
    "psutil",
    "transformers.models.siglip",
    "facenet_pytorch",
    "faiss",
    "certifi",
] + _tf_hidden + _fa_hidden

excludes = [
    "tkinter",
    "matplotlib",
    "notebook",
    "jupyter",
]

# UPX портит подписанные dylib/Qt6-библиотеки → отключаем повсеместно.
USE_UPX = False

# macOS: ad-hoc подпись ('-') + entitlements для PyTorch (JIT, unsigned exec
# memory) и загрузки неподписанных .dylib (FAISS/OpenCV/torch).
_codesign = "-" if IS_DARWIN else None
_entitlements = "entitlements.plist" if IS_DARWIN else None

# Иконка: .ico для EXE (Windows), .icns для BUNDLE (macOS) — только если есть.
_exe_icon = "assets/icons/app.ico" if os.path.exists("assets/icons/app.ico") else None
_app_icon = "assets/icons/app.icns" if os.path.exists("assets/icons/app.icns") else None


# --- App version: single source of truth = the git tag ----------------------
# info_plist (macOS) and setup.iss (Windows) used to carry a hand-typed "1.0.0"
# that silently drifted from the actual release (HEAD tag = v1.1.0). Derive it
# instead, in priority order:
#   1. $TENSORMEDIA_VERSION   — CI passes github.ref_name on a tag push (v*)
#   2. `git describe --tags --abbrev=0`  — local builds inside the repo
#   3. literal fallback       — source tarball with no git / no tags
# The winner is stripped of a leading 'v' and MUST be a dotted number
# (CFBundleVersion rejects anything else, e.g. a branch name from a manual run),
# otherwise we fall through to the next candidate.
def _app_version(_fallback="1.2.3"):
    import re
    import subprocess
    candidates = [os.environ.get("TENSORMEDIA_VERSION", "")]
    try:
        candidates.append(subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True, text=True, cwd=os.path.abspath("."),
        ).stdout.strip())
    except Exception:
        pass
    for _cand in candidates:
        _cand = (_cand or "").strip().lstrip("v")
        if re.fullmatch(r"\d+(\.\d+){0,3}", _cand):
            return _cand
    return _fallback

APP_VERSION = _app_version()


a = Analysis(
    ["main.py"],
    pathex=[os.path.abspath(".")],
    binaries=extra_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["pyi_rth_ssl_certs.py"],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
# --- macOS OpenSSL ABI conflict guard (Dyld Overriding Protection) ----------
# RCA: opencv-python's wheel vendors an OLD OpenSSL 3.0.x libcrypto/libssl under
# cv2/.dylibs. CPython's _ssl / _hashlib (and _hashlib's HKDF/X509 paths) are
# compiled against Homebrew openssl@3 (3.6.x) and need symbols that 3.0.x lacks
# — notably X509_STORE_get1_objects (added in OpenSSL 3.1). PyInstaller dedups
# dylibs by basename and binds _ssl's `libcrypto.3.dylib` reference to cv2's
# stale copy, so the .app dies at launch with:
#   Symbol not found: _X509_STORE_get1_objects
#   Expected in: .../cv2/.dylibs/libcrypto.3.dylib
# Fix: force EVERY libcrypto/libssl placed in the bundle to be the Homebrew copy
# (a binary-compatible superset — compat version 3.0.0), staged with
# self-relative (@loader_path) cross-references so it resolves correctly wherever
# PyInstaller drops it (bundle root *or* cv2/.dylibs). This converts a runtime
# dyld crash into a deterministic build-time assertion if OpenSSL is too old.
if IS_DARWIN:
    import glob
    import shutil
    import subprocess

    def _find_brew_openssl():
        candidates = []
        for prefix in ("/opt/homebrew", "/usr/local"):
            candidates += sorted(glob.glob(f"{prefix}/Cellar/openssl@3/*/lib"), reverse=True)
            candidates.append(f"{prefix}/opt/openssl@3/lib")
        for libdir in candidates:
            crypto = os.path.join(libdir, "libcrypto.3.dylib")
            ssl = os.path.join(libdir, "libssl.3.dylib")
            if os.path.exists(crypto) and os.path.exists(ssl):
                return os.path.realpath(crypto), os.path.realpath(ssl)
        return None, None

    _brew_crypto, _brew_ssl = _find_brew_openssl()
    if not _brew_crypto:
        raise SystemExit(
            "BUILD ABORT: Homebrew openssl@3 not found. Run `brew install openssl@3` "
            "— it is required to override cv2's stale vendored OpenSSL."
        )

    # Fail the BUILD (not the end user's launch) if the chosen libcrypto predates
    # the symbol _ssl needs.
    _nm = subprocess.run(["nm", "-gU", _brew_crypto], capture_output=True, text=True)
    if "_X509_STORE_get1_objects" not in _nm.stdout:
        raise SystemExit(
            f"BUILD ABORT: {_brew_crypto} lacks X509_STORE_get1_objects (needs "
            "OpenSSL >= 3.1). Run `brew upgrade openssl@3`."
        )

    # Stage ABI-correct copies with self-relative install names + ad-hoc signature
    # so they load whether PyInstaller places them at the bundle root or inside
    # cv2/.dylibs (both keep libssl and libcrypto as siblings → @loader_path works).
    _ssl_fix_dir = os.path.abspath("./build/_openssl_fix")
    os.makedirs(_ssl_fix_dir, exist_ok=True)
    _staged_crypto = os.path.join(_ssl_fix_dir, "libcrypto.3.dylib")
    _staged_ssl = os.path.join(_ssl_fix_dir, "libssl.3.dylib")
    shutil.copy2(_brew_crypto, _staged_crypto)
    shutil.copy2(_brew_ssl, _staged_ssl)
    os.chmod(_staged_crypto, 0o755)  # Cellar copies are 0444; make writable for install_name_tool
    os.chmod(_staged_ssl, 0o755)

    def _run(cmd):
        subprocess.run(cmd, check=True, capture_output=True)

    _run(["install_name_tool", "-id", "@rpath/libcrypto.3.dylib", _staged_crypto])
    _run(["install_name_tool", "-id", "@rpath/libssl.3.dylib", _staged_ssl])
    # libssl must find its sibling libcrypto wherever the pair lands in the bundle.
    _run(["install_name_tool", "-change", _brew_crypto,
          "@loader_path/libcrypto.3.dylib", _staged_ssl])
    # Rewriting load commands invalidates the arm64 signature → re-sign ad-hoc.
    for _f in (_staged_crypto, _staged_ssl):
        _run(["codesign", "-s", "-", "-f", _f])

    # Redirect every libcrypto/libssl entry to the staged (symbol-complete) bytes,
    # regardless of which destination PyInstaller chose for it.
    _ssl_override = {
        "libcrypto.3.dylib": _staged_crypto,
        "libssl.3.dylib": _staged_ssl,
    }
    a.binaries = [
        (dest, _ssl_override.get(os.path.basename(dest), src), typ)
        for dest, src, typ in a.binaries
    ]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TensorMedia",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=USE_UPX,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=_codesign,
    entitlements_file=_entitlements,
    icon=_exe_icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=USE_UPX,
    upx_exclude=[],
    name="TensorMedia",
)

if IS_DARWIN:
    app = BUNDLE(
        coll,
        name="TensorMedia.app",
        icon=_app_icon,
        bundle_identifier="com.tensormedia.arbitrage",
        info_plist={
            "NSHighResolutionCapable": True,
            "LSBackgroundOnly": False,
            "NSRequiresAquaSystemAppearance": False,
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": APP_VERSION,
        },
    )
