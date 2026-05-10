import os
import sys
from pathlib import Path

def get_base_path() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    else:
        return Path(os.path.abspath(__file__)).parent.parent

def get_models_dir() -> Path:
    base_path = get_base_path()
    if getattr(sys, 'frozen', False) and sys.platform == "darwin":
        return base_path.parent / "Resources" / "models"
    return base_path / "models"

def get_app_data_dir() -> Path:
    if sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "TensorMedia"
    elif sys.platform == "win32":
        path = Path(os.environ.get("APPDATA", Path.home())) / "TensorMedia"
    else:
        path = Path.home() / ".local" / "share" / "TensorMedia"
    path.mkdir(parents=True, exist_ok=True)
    return path

def get_cache_dir() -> Path:
    cache_path = get_base_path() / "cache"
    cache_path.mkdir(parents=True, exist_ok=True)
    return cache_path

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