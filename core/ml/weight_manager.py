# ============================================================
# MODULE: core/ml/weight_manager.py
# ============================================================
import os
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


class WeightFetchThread(QThread):
    """Runtime-загрузка весов в пользовательскую директорию моделей.

    Используется дистрибутивами БЕЗ упакованных весов (Linux-CPU, который
    обязан укладываться в лимит GitHub Releases 2 ГБ): SigLIP тянется через
    huggingface_hub, FaceNet — через стандартный torch-hub кэш (TORCH_HOME
    переключён на models_dir/torch). Логика зеркалит setup_models.py."""

    progress = Signal(str)
    fetch_completed = Signal(bool, str)

    def run(self):
        try:
            models_dir = get_models_dir()
            models_dir.mkdir(parents=True, exist_ok=True)
            siglip_dir = models_dir / "siglip-base-patch16-224"
            torch_home = models_dir / "torch"

            self.progress.emit("Downloading SigLIP (~370 MB)...")
            # snapshot_download уважает HF_HUB_OFFLINE, а не TRANSFORMERS_OFFLINE,
            # поэтому офлайн-флаги инференса загрузке не мешают.
            from huggingface_hub import snapshot_download
            snapshot_download(
                repo_id="google/siglip-base-patch16-224",
                local_dir=str(siglip_dir),
                # Только safetensors: дублирующий pytorch_model.bin (~775 МБ) и
                # TF/Flax/ONNX-веса не нужны (инференс читает model.safetensors).
                # Это и приводит размер первой загрузки к заявленным ~370 МБ.
                ignore_patterns=["*.bin", "*.h5", "*.msgpack", "*.onnx", "*.pth"],
            )

            self.progress.emit("Downloading FaceNet VGGFace2 (~110 MB)...")
            os.environ["TORCH_HOME"] = str(torch_home)
            from facenet_pytorch import InceptionResnetV1
            InceptionResnetV1(pretrained="vggface2").eval()

            self.fetch_completed.emit(True, "Weights downloaded.")
        except Exception as e:
            self.fetch_completed.emit(False, f"Download failed:\n{e}")


class LocalWeightValidator(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(translator.tr("npu_integrity"))
        self.setFixedSize(450, 170)
        self.setWindowFlags(Qt.WindowType.CustomizeWindowHint | Qt.WindowType.WindowTitleHint)
        self.setStyleSheet("background-color: #2B2D31; color: #DBDEE1;")

        layout = QVBoxLayout(self)
        self.lbl_status = QLabel(translator.tr("npu_analyzing"))
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl_status)

        # Runtime-загрузка весов (дистрибутивы без упакованных моделей):
        # вместо фатального выхода предлагаем скачать веса в app-data.
        is_ru = getattr(translator, "current_lang", "en") == "ru"
        self.btn_download = QPushButton(
            "Скачать веса (~480 МБ)" if is_ru else "Download weights (~480 MB)"
        )
        self.btn_download.setStyleSheet(
            "background-color: #23A559; color: white; border-radius: 4px; "
            "padding: 6px; font-weight: bold;"
        )
        self.btn_download.clicked.connect(self._start_download)
        self.btn_download.hide()
        layout.addWidget(self.btn_download)

        self.btn_exit = QPushButton(translator.tr("npu_exit"))
        self.btn_exit.setStyleSheet("background-color: #DA3633; color: white; border-radius: 4px; padding: 6px; font-weight: bold;")
        self.btn_exit.clicked.connect(self.reject)
        self.btn_exit.hide()
        layout.addWidget(self.btn_exit)

        # NB: must not be named `self.thread` — that shadows QObject.thread().
        # Parent to the dialog so Qt tracks ownership.
        self._check_thread = IntegrityCheckThread(self)
        self._check_thread.check_completed.connect(self._on_check_finished)
        self._fetch_thread = None

    def start(self):
        self._check_thread.start()

    def closeEvent(self, event):
        # Prevent "QThread: Destroyed while thread is still running" if the
        # dialog is closed while the integrity check is mid-flight.
        if self._check_thread.isRunning():
            self._check_thread.quit()
            self._check_thread.wait(2000)
        if self._fetch_thread is not None and self._fetch_thread.isRunning():
            self._fetch_thread.quit()
            self._fetch_thread.wait(2000)
        super().closeEvent(event)

    def _start_download(self):
        self.btn_download.setEnabled(False)
        self.btn_exit.hide()
        self.lbl_status.setStyleSheet("")
        self.lbl_status.setText("Initializing download...")
        self._fetch_thread = WeightFetchThread(self)
        self._fetch_thread.progress.connect(self.lbl_status.setText)
        self._fetch_thread.fetch_completed.connect(self._on_fetch_finished)
        self._fetch_thread.start()

    def _on_fetch_finished(self, success, msg):
        self.btn_download.setEnabled(True)
        if success:
            # Повторная верификация скачанных весов тем же контуром целостности.
            self.btn_download.hide()
            self.lbl_status.setText(translator.tr("npu_analyzing"))
            self._check_thread = IntegrityCheckThread(self)
            self._check_thread.check_completed.connect(self._on_check_finished)
            self._check_thread.start()
        else:
            self.lbl_status.setText(msg)
            self.lbl_status.setStyleSheet("color: #DA3633; font-weight: bold;")
            self.btn_exit.show()

    def _on_check_finished(self, success, msg):
        if success:
            self.accept()
        else:
            self.lbl_status.setText(translator.tr("npu_fatal").format(msg))
            self.lbl_status.setStyleSheet("color: #DA3633; font-weight: bold;")
            self.btn_download.show()
            self.btn_exit.show()
