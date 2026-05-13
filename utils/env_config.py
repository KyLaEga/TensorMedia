import os
import sys
from pathlib import Path

def get_base_path() -> Path:
    if getattr(sys, 'frozen', False):
        if hasattr(sys, '_MEIPASS'):
            return Path(sys._MEIPASS)
        return Path(sys.executable).parent
    else:
        return Path(os.path.abspath(__file__)).parent.parent

def get_models_dir() -> Path:
    if getattr(sys, 'frozen', False):
        if hasattr(sys, '_MEIPASS'):
            return Path(sys._MEIPASS) / "models"
        
        base_path = Path(sys.executable).parent
        if sys.platform == "darwin":
            # Для .app бандла ресурсы лежат в Contents/Resources
            return base_path.parent / "Resources" / "models"
    
    return get_base_path() / "models"

def get_app_data_dir() -> Path:
    if sys.platform == "darwin":
        # Используем локальную папку для данных, чтобы избежать проблем с песочницей macOS
        if getattr(sys, 'frozen', False):
            # Если запущено из .app, сохраняем данные рядом с .app
            app_bundle = Path(sys.executable).parent.parent.parent
            path = app_bundle.parent / "TensorMedia_Data"
        else:
            path = Path.home() / "Library" / "Application Support" / "TensorMedia"
    elif sys.platform == "win32":
        path = Path(os.environ.get("APPDATA", Path.home())) / "TensorMedia"
    else:
        path = Path.home() / ".local" / "share" / "TensorMedia"
    
    try:
        path.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        # Fallback to current working directory if permission denied
        path = Path.cwd() / "TensorMedia_Data"
        path.mkdir(parents=True, exist_ok=True)
        
    return path

def get_data_dir() -> Path:
    """Writable app data: SQLite, WAL/SHM, FAISS cache fragments, journals."""
    path = get_app_data_dir() / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path

def get_logs_dir() -> Path:
    path = get_app_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path

def get_cache_dir() -> Path:
    """SQLite DBs and on-disk vector cache (alias for get_data_dir)."""
    return get_data_dir()

def setup_offline_env():
    models_dir = get_models_dir()
    os.environ["HF_HOME"] = str(models_dir)
    os.environ["TORCH_HOME"] = str(models_dir / "torch")
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"
    
    # Hotfix_Architecture_Clash: Токенизатор SentencePiece (SigLIP) вызывает Segfault на Apple Silicon (ARM64)
    # при десериализации через C++ бэкенд Protobuf. Форсированный возврат к Python-слою.
    os.environ["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"

def resource_path(relative_path: str) -> str:
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)