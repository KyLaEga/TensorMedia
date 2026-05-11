# ============================================================
# MODULE: main.py
# ============================================================
import sys
import os

# --- PRE-FLIGHT BOOTSTRAPPER ---
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')

if getattr(sys, 'frozen', False):
    app_dir = os.path.dirname(sys.executable)
    # В macOS внутри .app исполняемый файл лежит в Contents/MacOS/
    # Нам нужно подняться выше или использовать ресурсы
    if sys.platform == 'darwin':
        app_dir = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
else:
    app_dir = os.path.dirname(os.path.abspath(__file__))

os.chdir(app_dir)
sys.path.insert(0, app_dir)

# Настройка путей для ML-библиотек (Offline Mode)
models_path = os.path.join(app_dir, "models")
os.environ["TORCH_HOME"] = os.path.join(models_path, "torch")
os.environ["HF_HOME"] = os.path.join(models_path, "huggingface")
os.environ["TRANSFORMERS_OFFLINE"] = "1"

if sys.platform == 'win32':
    try:
        os.add_dll_directory(app_dir)
        # Добавляем путь к подпапке PySide6 для Windows
        pyside_path = os.path.join(app_dir, "PySide6")
        if os.path.exists(pyside_path):
            os.add_dll_directory(pyside_path)
    except AttributeError:
        pass
# -------------------------------

import traceback
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

from utils.logger import auditor
from ui.views.main_window import MainWindow
from ui.controllers.main_controller import MainController
from core.services.ml_orchestrator import MLOrchestrator
from utils.env_config import setup_offline_env

class ApplicationBootstrap:
    @staticmethod
    def execute():
        sys.excepthook = ApplicationBootstrap._global_exception_handler
        auditor.info("Initializing TensorMedia core architecture...")
        
        setup_offline_env()
        
        app = QApplication(sys.argv)
        app.setApplicationName("TensorMedia")
        
        try:
            ml_orchestrator = MLOrchestrator()
            window = MainWindow()
            controller = MainController(window)
            
            app._ml_orchestrator = ml_orchestrator
            app._controller = controller
            
            window.show()
            auditor.info("Application successfully bootstrapped. Entering event loop.")
            sys.exit(app.exec())
            
        except Exception as e:
            ApplicationBootstrap._global_exception_handler(type(e), e, e.__traceback__)

    @staticmethod
    def _global_exception_handler(exc_type, exc_value, exc_traceback):
        error_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        try:
            auditor.critical(f"Unhandled exception:\n{error_msg}")
        except: pass
        sys.exit(1)

if __name__ == "__main__":
    ApplicationBootstrap.execute()