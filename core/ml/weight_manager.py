# ============================================================
# MODULE: core/ml/weight_manager.py
# ============================================================
import os
from pathlib import Path
from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton
from PySide6.QtCore import Qt, QThread, Signal
from utils.env_config import get_models_dir
from utils.i18n import translator

class IntegrityCheckThread(QThread):
    check_completed = Signal(bool, str) 

    def run(self):
        try:
            models_dir = get_models_dir()
            siglip_path = models_dir / "siglip-base-patch16-224"
            torch_home = models_dir / "torch"
            
            siglip_model = siglip_path / "model.safetensors"
            if not siglip_model.exists() or siglip_model.stat().st_size < 367001600:
                self.check_completed.emit(False, f"SigLIP weights corrupted or missing in:\n{siglip_path}")
                return
                
            facenet_model = torch_home / "checkpoints" / "20180402-114759-vggface2.pt"
            if not facenet_model.exists() or facenet_model.stat().st_size < 104857600:
                self.check_completed.emit(False, f"FaceNet weights corrupted or missing in:\n{torch_home}")
                return

            os.environ["HF_HOME"] = str(models_dir)
            os.environ["TORCH_HOME"] = str(torch_home)
            
            self.check_completed.emit(True, "NPU Architecture Verified.")
        except Exception as e:
            self.check_completed.emit(False, f"Storage I/O Error:\n{str(e)}")

class LocalWeightValidator(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(translator.tr("npu_integrity"))
        self.setFixedSize(450, 150)
        self.setWindowFlags(Qt.WindowType.CustomizeWindowHint | Qt.WindowType.WindowTitleHint)
        self.setStyleSheet("background-color: #2B2D31; color: #DBDEE1;")
        
        layout = QVBoxLayout(self)
        self.lbl_status = QLabel(translator.tr("npu_analyzing"))
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl_status)
        
        self.btn_exit = QPushButton(translator.tr("npu_exit"))
        self.btn_exit.setStyleSheet("background-color: #DA3633; color: white; border-radius: 4px; padding: 6px; font-weight: bold;")
        self.btn_exit.clicked.connect(self.reject) 
        self.btn_exit.hide()
        layout.addWidget(self.btn_exit)

        self.thread = IntegrityCheckThread()
        self.thread.check_completed.connect(self._on_check_finished)

    def start(self):
        self.thread.start()

    def _on_check_finished(self, success, msg):
        if success:
            self.accept()
        else:
            self.lbl_status.setText(translator.tr("npu_fatal").format(msg))
            self.lbl_status.setStyleSheet("color: #DA3633; font-weight: bold;")
            self.btn_exit.show()