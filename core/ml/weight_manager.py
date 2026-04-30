import os
import sys
from pathlib import Path
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton, QApplication
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from utils.env_config import get_models_dir

class IntegrityCheckThread(QThread):
    finished = pyqtSignal(bool, str)

    def run(self):
        try:
            models_dir = get_models_dir()
            siglip_path = models_dir / "siglip-base-patch16-224"
            torch_home = models_dir / "torch"
            
            # Проверка 1: Наличие папки SigLIP и её конфигурации
            if not (siglip_path / "config.json").exists():
                self.finished.emit(False, f"Отсутствуют веса SigLIP в:\n{siglip_path}")
                return
                
            # Проверка 2: Наличие кэша FaceNet (VGGFace2)
            if not torch_home.exists():
                self.finished.emit(False, f"Отсутствует кэш FaceNet в:\n{torch_home}")
                return

            # Настройка переменных окружения для библиотек
            os.environ["HF_HOME"] = str(models_dir)
            os.environ["TORCH_HOME"] = str(torch_home)
            
            self.finished.emit(True, "Интеграция подтверждена.")
        except Exception as e:
            self.finished.emit(False, f"Ошибка ввода-вывода:\n{str(e)}")

class LocalWeightValidator(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Tensor Media: NPU Integrity")
        self.setFixedSize(450, 150)
        self.setWindowFlags(Qt.WindowType.CustomizeWindowHint | Qt.WindowType.WindowTitleHint)
        self.setStyleSheet("background-color: #2B2D31; color: #DBDEE1;")
        
        layout = QVBoxLayout(self)
        self.lbl_status = QLabel("Анализ локальной тензорной архитектуры...")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl_status)
        
        self.btn_exit = QPushButton("Завершить процесс")
        self.btn_exit.setStyleSheet("background-color: #DA3633; color: white; border-radius: 4px; padding: 6px; font-weight: bold;")
        self.btn_exit.clicked.connect(sys.exit)
        self.btn_exit.hide()
        layout.addWidget(self.btn_exit)

        self.thread = IntegrityCheckThread()
        self.thread.finished.connect(self._on_check_finished)

    def start(self):
        self.thread.start()

    def _on_check_finished(self, success, msg):
        if success:
            self.accept()
        else:
            self.lbl_status.setText(f"КРИТИЧЕСКИЙ СБОЙ NPU:\n{msg}\n\nПереустановите приложение или проверьте целостность архива.")
            self.lbl_status.setStyleSheet("color: #DA3633; font-weight: bold;")
            self.btn_exit.show()