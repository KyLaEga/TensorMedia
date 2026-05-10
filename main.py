# ============================================================
# MODULE: main.py
# ============================================================
import sys
import os

# --- PRE-FLIGHT BOOTSTRAPPER ---
# 1. Изоляция I/O: предотвращает падение без консоли
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')

# 2. Вычисление абсолютного пути ядра в скомпилированной среде
if getattr(sys, 'frozen', False):
    # PyInstaller onedir/bundle mode
    app_dir = os.path.dirname(sys.executable)
else:
    # Source execution mode
    app_dir = os.path.dirname(os.path.abspath(__file__))

# 3. macOS Fix: Принудительный захват рабочей директории
os.chdir(app_dir)
sys.path.insert(0, app_dir)

# 4. Windows Fix: Насильственная инъекция DLL-путей (Python 3.8+)
if sys.platform == 'win32':
    try:
        os.add_dll_directory(app_dir)
        pyside_dir = os.path.join(app_dir, 'PySide6')
        if os.path.exists(pyside_dir):
            os.add_dll_directory(pyside_dir)
    except AttributeError:
        pass
    os.environ['PATH'] = app_dir + os.pathsep + os.environ.get('PATH', '')
# -------------------------------

# ТОЛЬКО ТЕПЕРЬ импортируем графику, когда пути ОС взломаны
import traceback
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import QMetaObject, Qt

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