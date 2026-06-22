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
            bundled = Path(sys._MEIPASS) / "models"
            # Linux-CPU дистрибутив поставляется БЕЗ весов (лимит GitHub 2 ГБ):
            # если в бандле их нет, веса живут в пользовательской app-data
            # директории (туда их скачивает weight_manager при первом запуске).
            # Бандл read-only — скачивать внутрь _internal нельзя в принципе.
            if (bundled / "siglip-base-patch16-224").exists():
                return bundled
            return get_app_data_dir() / "models"

        base_path = Path(sys.executable).parent
        if sys.platform == "darwin":
            return base_path.parent / "Resources" / "models"

    return get_base_path() / "models"

def get_app_data_dir() -> Path:
    if sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "TensorMedia"
    elif sys.platform == "win32":
        path = Path(os.environ.get("APPDATA", Path.home())) / "TensorMedia"
    else:
        path = Path.home() / ".local" / "share" / "TensorMedia"
    
    try:
        path.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        path = Path.home() / "TensorMedia_Data_Fallback"
        path.mkdir(parents=True, exist_ok=True)
        
    return path

def get_data_dir() -> Path:
    path = get_app_data_dir() / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path

def get_logs_dir() -> Path:
    path = get_app_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def setup_offline_env():
    models_dir = get_models_dir()
    os.environ["HF_HOME"] = str(models_dir)
    os.environ["TORCH_HOME"] = str(models_dir / "torch")
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"
    
    os.environ["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"
