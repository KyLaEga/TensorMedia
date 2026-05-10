from PySide6.QtCore import QObject, Signal

class AppEventBus(QObject):
    """Глобальная шина событий для декомпозиции UI-слоя и ML-ядра."""
    
    cmd_warmup_engine = Signal()
    cmd_start_scan = Signal(list, set, str) 
    cmd_toggle_pause = Signal()
    cmd_stop_scan = Signal()
    cmd_recluster = Signal(float) 

    evt_engine_ready = Signal(object)
    evt_scan_progress = Signal(int, int, str)
    evt_scan_completed = Signal()
    evt_scan_error = Signal(str)
    evt_clustering_completed = Signal(list)
    
    # Вектор телеметрии: передача метрик производительности
    evt_telemetry_update = Signal(dict)

bus = AppEventBus()