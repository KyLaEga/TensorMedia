import os
import sys
from pathlib import Path

def get_base_path() -> Path:
    """Определяет корень приложения независимо от способа запуска."""
    if getattr(sys, 'frozen', False):
        # Режим скомпилированного бинарника (.app или .exe)
        return Path(sys.executable).parent
    else:
        # Режим разработки (python main.py)
        # __file__ указывает на TensorMedia/utils/env_config.py
        # .parent (это utils) -> .parent (это корень проекта TensorMedia)
        return Path(os.path.abspath(__file__)).parent.parent

def get_models_dir() -> Path:
    """Динамический роутинг к тяжелым весам."""
    base_path = get_base_path()
    
    if getattr(sys, 'frozen', False) and sys.platform == "darwin":
        # Внутри macOS .app бандла данные лежат в Contents/Resources
        return base_path.parent / "Resources" / "models"
    
    # Для Windows .exe и режима разработки (python main.py)
    return base_path / "models"

def get_app_data_dir() -> Path:
    """Путь для SQLite баз данных (остается в системе для сохранения кэша)."""
    if sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "TensorMedia"
    elif sys.platform == "win32":
        path = Path(os.environ.get("APPDATA", Path.home())) / "TensorMedia"
    else:
        path = Path.home() / ".local" / "share" / "TensorMedia"
    path.mkdir(parents=True, exist_ok=True)
    return path

def setup_offline_env():
    """Абсолютная изоляция от сети."""
    models_dir = get_models_dir()
    os.environ["HF_HOME"] = str(models_dir)
    os.environ["TORCH_HOME"] = str(models_dir / "torch")
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"

def resource_path(relative_path: str) -> str:
    """Маршрутизация для ресурсов PyInstaller (иконки и т.д.)"""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)