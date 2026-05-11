import sys
import os

# 1. Изоляция I/O: предотвращает падение без консоли на macOS/Windows
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')

# 2. Чистая маршрутизация
if getattr(sys, 'frozen', False):
    app_dir = os.path.dirname(sys.executable)
    if sys.platform == 'darwin':
        app_dir = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
else:
    app_dir = os.path.dirname(os.path.abspath(__file__))

os.chdir(app_dir)
sys.path.insert(0, app_dir)

models_path = os.path.join(app_dir, "models")
os.environ["TORCH_HOME"] = os.path.join(models_path, "torch")
os.environ["HF_HOME"] = os.path.join(models_path, "huggingface")
os.environ["TRANSFORMERS_OFFLINE"] = "1"

if sys.platform == 'win32':
    try:
        os.add_dll_directory(app_dir)
        pyside_path = os.path.join(app_dir, "PySide6")
        if os.path.exists(pyside_path):
            os.add_dll_directory(pyside_path)
    except AttributeError: pass

import multiprocessing
import traceback
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
from PySide6.QtCore import Qt, QMetaObject

from utils.env_config import setup_offline_env
from utils.batch_operations import BatchOperations
from core.ml.weight_manager import LocalWeightValidator
from utils.logger import auditor

class ApplicationBootstrap:
    orchestrator = None
    
    @staticmethod
    def _render_critical_ui(exc_value, error_msg):
        try:
            msg_box = QMessageBox()
            msg_box.setIcon(QMessageBox.Icon.Critical)
            msg_box.setWindowTitle("Критический сбой")
            msg_box.setText(f"Произошла фатальная ошибка:\n\n{exc_value}")
            msg_box.setDetailedText(error_msg)
            msg_box.exec()
        except Exception: pass

    @staticmethod
    def global_exception_handler(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return

        error_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        app = QApplication.instance()
        if app and not app.closingDown():
            QMetaObject.invokeMethod(
                app, 
                lambda: ApplicationBootstrap._render_critical_ui(exc_value, error_msg), 
                Qt.ConnectionType.QueuedConnection
            )

    @classmethod
    def execute(cls):
        # ЗАЩИТА ОТ ФОРК-БОМБЫ (Критично для macOS)
        multiprocessing.freeze_support()
        sys.excepthook = cls.global_exception_handler
        
        setup_offline_env()
        BatchOperations.check_and_recover_pending_transactions()
        
        QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
        app = QApplication(sys.argv)
        
        validator = LocalWeightValidator()
        validator.start()
        
        if validator.exec() == QDialog.Accepted:
            from core.services.ml_orchestrator import MLOrchestrator
            from ui.views.main_window import MainWindow
            from ui.controllers.main_controller import MainController
            
            cls.orchestrator = MLOrchestrator()
            window = MainWindow()
            controller = MainController(window) 
            window.window_closed.connect(cls.orchestrator.stop_all)
            
            window.show()
            sys.exit(app.exec())
        else:
            sys.exit(1)

if __name__ == "__main__":
    ApplicationBootstrap.execute()