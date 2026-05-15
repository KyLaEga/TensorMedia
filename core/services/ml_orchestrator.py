import time
import psutil
import multiprocessing
import threading
from PySide6.QtCore import QObject, QTimer
from core.events import bus
from core.ml.cluster_engine import SmartClusterEngine
from ui.workers import ScannerBridge, ClusterWorker, EngineWarmupWorker, MaintenanceWorker
from utils.logger import auditor

class MLOrchestrator(QObject):
    def __init__(self):
        super().__init__()
        self.engine = None
        self.scanner = None
        self.cluster_worker = None
        self.warmup_worker = None
        self.maintenance_worker = None
        self.t_start = 0.0

        self.idle_timer = QTimer(self)
        self.idle_timer.setInterval(300000) 
        self.idle_timer.setSingleShot(True)
        self.idle_timer.timeout.connect(self._on_idle_timeout)

        self._stop_all_called = False
        self._bind_bus()

    def _bind_bus(self):
        bus.cmd_warmup_engine.connect(self._handle_warmup)
        bus.cmd_start_scan.connect(self._handle_scan)
        bus.cmd_toggle_pause.connect(self._handle_pause)
        bus.cmd_stop_scan.connect(self._handle_stop)
        bus.cmd_recluster.connect(self._handle_recluster)

    def _reset_idle_timer(self):
        if self.idle_timer.isActive():
            self.idle_timer.stop()
        self.idle_timer.start()

    def _on_idle_timeout(self):
        if self.engine:
            auditor.info("Idle timeout reached. Unloading NPU models to free RAM/VRAM.")
            self.engine.unload_models()

    def _emit_telemetry(self, phase_name):
        duration = time.time() - self.t_start
        process = psutil.Process()
        ram_mb = process.memory_info().rss / (1024 * 1024)
        auditor.debug(f"Telemetry Metric [Phase: {phase_name} | Time: {duration:.2f}s | RAM Peak: {ram_mb:.0f}MB]")
        bus.evt_telemetry_update.emit({
            "phase": phase_name,
            "time": duration,
            "ram_mb": ram_mb
        })

    def _handle_warmup(self):
        if self.warmup_worker is not None:
            if self.warmup_worker.isRunning(): return
            self.warmup_worker.deleteLater()
            self.warmup_worker = None

        auditor.info("Initiating NPU Engine Warmup sequence...")
        self.warmup_worker = EngineWarmupWorker()
        self.warmup_worker.engine_ready.connect(self._on_engine_ready)
        self.warmup_worker.error_occurred.connect(self._on_engine_error)
        self.warmup_worker.start()

    def _on_engine_ready(self, engine):
        self.engine = engine
        self._reset_idle_timer()
        auditor.info("NPU Engine Warmup complete. Bus event emitted.")
        bus.evt_engine_ready.emit(engine)
        
        if self.maintenance_worker is not None:
            self.maintenance_worker.deleteLater()
        self.maintenance_worker = MaintenanceWorker()
        self.maintenance_worker.start()

    def _on_engine_error(self, err_msg):
        auditor.error(f"Warmup failed: {err_msg}")
        bus.evt_engine_ready.emit(None)

    def _handle_scan(self, dirs, exts, mode):
        if self.idle_timer.isActive():
            self.idle_timer.stop()
            
        self.t_start = time.time()
        
        if self.engine is None:
            auditor.warning("Scan rejected: Engine not ready.")
            return

        if self.scanner is not None:
            if self.scanner.isRunning():
                auditor.warning("Scan rejected: Scanner already running.")
                return
            self.scanner.deleteLater()
            self.scanner = None
            
        auditor.info(f"Starting Scan Matrix [Mode: {mode} | Dirs: {dirs}]")
        self.scanner = ScannerBridge(self.engine, dirs, exts, mode)
        self.scanner.progress_updated.connect(bus.evt_scan_progress.emit)
        
        def _on_scan_complete():
            self._reset_idle_timer()
            self._emit_telemetry("Scan+Vectorize")
            auditor.info("Scan Matrix processing complete.")
            bus.evt_scan_completed.emit()
            
        self.scanner.scan_completed.connect(_on_scan_complete)
        self.scanner.error_occurred.connect(self._handle_scan_error)
        self.scanner.start()

    def _handle_scan_error(self, err_msg):
        self._reset_idle_timer()
        auditor.error(f"Scan Matrix pipeline crashed: {err_msg}")
        bus.evt_scan_error.emit(err_msg)

    def _handle_pause(self):
        if self.engine:
            self.engine.is_paused = not self.engine.is_paused
            auditor.info(f"Engine execution paused: {self.engine.is_paused}")

    def _handle_stop(self):
        self._reset_idle_timer()
        if self.scanner and self.scanner.isRunning():
            auditor.warning("Stopping active scanner thread...")
            if hasattr(self.scanner, "stop"):
                self.scanner.stop()
            self.scanner.requestInterruption()
            self.scanner.quit()
        
        if self.engine:
            self.engine.request_scan_abort()
            auditor.warning("Engine execution explicitly stopped by user.")

    def _handle_recluster(self, threshold):
        if self.idle_timer.isActive():
            self.idle_timer.stop()
            
        self.t_start = time.time()
        
        # КРИТИЧЕСКИЙ ПАТЧ: Безопасное удаление старого воркера перед созданием нового
        if self.cluster_worker is not None:
            if self.cluster_worker.isRunning(): 
                return # Игнорируем спам ползунком, пока идет расчет
            self.cluster_worker.deleteLater()
            self.cluster_worker = None

        if not self.engine or not getattr(self.engine, 'current_file_data', []): 
            bus.evt_clustering_completed.emit([])
            self._reset_idle_timer()
            return
            
        available_ram_mb = psutil.virtual_memory().available / (1024 * 1024)
        if available_ram_mb < 512.0:
            auditor.critical(f"OOM Prevented: Available RAM ({available_ram_mb:.1f} MB) is critically low for FAISS.")
            bus.evt_scan_error.emit("Not enough system memory to perform clustering safely.")
            self._reset_idle_timer()
            return
            
        auditor.info(f"Initiating FAISS Re-clustering at threshold: {threshold}")
        self.cluster_worker = ClusterWorker(self.engine, threshold)
        
        def _on_cluster_complete(res):
            self._reset_idle_timer()
            self._emit_telemetry("FAISS_Clustering")
            bus.evt_clustering_completed.emit(res)
            
        self.cluster_worker.clustering_completed.connect(_on_cluster_complete)
        self.cluster_worker.start()

    def stop_all(self):
        if self._stop_all_called:
            return
        self._stop_all_called = True
        auditor.info("Tearing down ML Orchestrator asynchronously...")
        
        self.idle_timer.stop()
        if self.engine:
            self.engine.request_scan_abort()

        def _soft_stop_qthread(t, name: str):
            if not t:
                return
            try:
                if hasattr(t, "stop"):
                    t.stop()
                if hasattr(t, "requestInterruption"):
                    t.requestInterruption()
                if hasattr(t, "quit"):
                    t.quit()
                QTimer.singleShot(3000, lambda: auditor.warning(f"Thread {name} timeout") if t.isRunning() else None)
            except Exception as e:
                auditor.error(f"Failed stopping {name}: {e}")

        _soft_stop_qthread(self.scanner, "ScannerBridge")
        _soft_stop_qthread(self.cluster_worker, "ClusterWorker")
        _soft_stop_qthread(self.warmup_worker, "EngineWarmupWorker")
        _soft_stop_qthread(self.maintenance_worker, "MaintenanceWorker")

        def _kill_children():
            try:
                children = multiprocessing.active_children()
                if children:
                    auditor.warning(f"Terminating {len(children)} active child process(es).")
                for p in children:
                    try:
                        p.terminate()
                    except Exception:
                        pass
            except Exception as e:
                auditor.error(f"Failed to terminate child processes: {e}")
                
        threading.Thread(target=_kill_children, daemon=True).start()