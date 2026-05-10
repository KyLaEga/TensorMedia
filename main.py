# ============================================================
# MODULE: main.py
# ============================================================
import sys
import os
import traceback
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import QMetaObject, Qt

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

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
        app.setApplicationVersion("1.0.0")
        
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
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
            
        error_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        
        try:
            auditor.critical(f"Unhandled GUI/Core exception:\n{error_msg}")
        except Exception:
            pass
            
        try:
            print(f"CRITICAL RUNTIME ERROR:\n{error_msg}", file=sys.stderr)
        except Exception:
            import os
            os.write(2, f"CRITICAL RUNTIME ERROR:\n{error_msg}".encode('utf-8'))
        sys.exit(1)

if __name__ == "__main__":
    os.environ["QT_API"] = "pyside6"
    os.environ["OMP_NUM_THREADS"] = "1" 
    
    ApplicationBootstrap.execute()