import sys
import multiprocessing
from PyQt6.QtWidgets import QApplication, QDialog
from utils.env_config import setup_offline_env, get_models_dir
from core.ml.weight_manager import LocalWeightValidator

if __name__ == "__main__":
    multiprocessing.freeze_support()
    
    # 1. Активация Offline-режима
    setup_offline_env()
    
    app = QApplication(sys.argv)
    
    # 2. Быстрая проверка: есть ли папка с моделями рядом?
    validator = LocalWeightValidator()
    validator.start()
    
    if validator.exec() == QDialog.DialogCode.Accepted:
        from ui.views.main_window import MainWindow
        window = MainWindow()
        window.show()
        sys.exit(app.exec())
    else:
        # Если validator вывел ошибку (как на вашем скриншоте), приложение закроется
        sys.exit(0)