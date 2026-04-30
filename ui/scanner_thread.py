from PyQt6.QtCore import QThread, pyqtSignal
from pathlib import Path
from core.ml.tensor_engine import TensorClusterEngine

class ScannerThread(QThread):
    """Изолированный рабочий поток для защиты интерфейса."""
    
    # Сигналы для связи с главным окном PyQt6
    progress_signal = pyqtSignal(int, int, str)
    result_signal = pyqtSignal(list)
    error_signal = pyqtSignal(str)

    def __init__(self, target_dir: str, threshold: float = 0.88):
        super().__init__()
        self.target_dir = Path(target_dir)
        self.threshold = threshold

    def run(self):
        try:
            engine = TensorClusterEngine(sim_threshold=self.threshold)
            clusters = engine.scan_and_cluster(
                self.target_dir, 
                progress_callback=lambda curr, total, msg: self.progress_signal.emit(curr, total, msg)
            )
            self.result_signal.emit(clusters)
        except Exception as e:
            self.error_signal.emit(f"Критический сбой ядра: {str(e)}")