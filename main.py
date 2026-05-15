# ============================================================
# MODULE: main.py
# ============================================================
import sys
import os

if sys.stdout is None: sys.stdout = open(os.devnull, 'w')
if sys.stderr is None: sys.stderr = open(os.devnull, 'w')

# КРИТИЧЕСКИЙ ПАТЧ: Ограничение потоков на уровне ядра ОС для предотвращения крашей 
# OpenMP/BLAS (faiss, torch, numpy) на архитектуре Apple Silicon и Windows x64.
# Должно выполняться ДО ЛЮБЫХ импортов сторонних библиотек.
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

import multiprocessing
import traceback

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QIcon

from utils.env_config import setup_offline_env, resource_path
from utils.batch_operations import BatchOperations
from core.ml.weight_manager import LocalWeightValidator
from utils.logger import auditor

class ApplicationBootstrap:
    @staticmethod
    def _render_critical_ui(exc_value, error_msg):
        try:
            msg_box = QMessageBox()
            msg_box.setIcon(QMessageBox.Icon.Critical)
            msg_box.setWindowTitle("Критический сбой")
            msg_box.setText(f"Произошла фатальная ошибка:\n\n{exc_value}")
            msg_box.setDetailedText(error_msg)
            msg_box.exec()
        except Exception as gui_exc:
            auditor.error(f"FAILED TO RENDER CRITICAL UI: {gui_exc}")

    @staticmethod
    def global_exception_handler(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return

        error_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        auditor.critical(f"CRITICAL RUNTIME ERROR:\n{error_msg}")
        
        app = QApplication.instance()
        if app and not app.closingDown():
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: ApplicationBootstrap._render_critical_ui(exc_value, error_msg))

    @classmethod
    def execute(cls):
        os.environ["QT_API"] = "pyside6"
        sys.excepthook = cls.global_exception_handler
        
        if sys.platform == "win32":
            try:
                import ctypes
                myappid = 'com.tensormedia.arbitrage.v1'
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
            except Exception as e:
                auditor.warning(f"Failed to set Windows AppUserModelID: {e}")
        
        auditor.info("TensorMedia Application Bootstrapping Started.")
        
        setup_offline_env()
        BatchOperations.check_and_recover_pending_transactions()
        
        QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
        app = QApplication(sys.argv)
        
        if sys.platform == "win32":
            font = app.font()
            font.setFamily("Segoe UI")
            font.setPixelSize(13) 
            app.setFont(font)

        icon_path = resource_path("assets/icons/app.ico" if sys.platform == "win32" else "assets/icons/app.icns")
        if os.path.exists(icon_path):
            app.setWindowIcon(QIcon(icon_path))
        
        validator = LocalWeightValidator()
        validator.start()
        
        if validator.exec() == QDialog.Accepted:
            from core.services.ml_orchestrator import MLOrchestrator
            from ui.views.main_window import MainWindow
            from ui.controllers.main_controller import MainController
            
            cls.orchestrator = MLOrchestrator()
            window = MainWindow()
            controller = MainController(window) 
            
            if cls.orchestrator:
                window.window_closed.connect(cls.orchestrator.stop_all)
                app.aboutToQuit.connect(cls.orchestrator.stop_all)
            
            auditor.info("UI and NPU Orchestrator initialized successfully.")
            window.show()
            sys.exit(app.exec())
        else:
            auditor.warning("NPU Weight Validation Failed. Terminating process.")
            sys.exit(1)

if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        multiprocessing.set_start_method('spawn', force=True)
    except RuntimeError:
        pass
    ApplicationBootstrap.execute()