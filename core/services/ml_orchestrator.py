import time
import psutil
import multiprocessing
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
        
        self.maintenance_worker = MaintenanceWorker()
        self.maintenance_worker.start()

    def _on_engine_error(self, err_msg):
        auditor.error(f"Warmup failed: {err_msg}")
        bus.evt_engine_ready.emit(None)

    def _handle_scan(self, dirs, exts, mode):
        if self.idle_timer.isActive():
            self.idle_timer.stop()
            
        self.t_start = time.time()
        
        if self.engine is None or (self.scanner and self.scanner.isRunning()):
            auditor.warning("Scan rejected: Engine not ready or scanner already running.")
            return
            
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
            self.scanner.stop()
            self.scanner.wait(2000)
            if self.scanner.isRunning():
                self.scanner.terminate()
                self.scanner.wait()
        
        if self.engine:
            self.engine.request_scan_abort()
            auditor.warning("Engine execution explicitly stopped by user.")

    def _handle_recluster(self, threshold):
        if self.idle_timer.isActive():
            self.idle_timer.stop()
            
        self.t_start = time.time()
        
        if self.cluster_worker and self.cluster_worker.isRunning(): 
            return
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
        auditor.info("Tearing down ML Orchestrator and terminating active threads...")
        self.idle_timer.stop()
        if self.scanner and hasattr(self.scanner, "stop"):
            try:
                self.scanner.stop()
            except Exception as e:
                auditor.warning(f"ScannerBridge.stop failed: {e}")
        if self.engine:
            self.engine.request_scan_abort()

        def _stop_qthread(t, name: str, timeout_ms: int = 5000):
            if not t:
                return
            try:
                if hasattr(t, "requestInterruption"):
                    t.requestInterruption()
                if hasattr(t, "quit"):
                    t.quit()
                if hasattr(t, "isRunning") and t.isRunning():
                    if not t.wait(timeout_ms):
                        auditor.warning(f"{name} did not stop in {timeout_ms}ms. Forcing terminate().")
                        try:
                            t.terminate()
                        except Exception as e:
                            auditor.error(f"Failed to terminate {name}: {e}")
                        t.wait(2000)
            except Exception as e:
                auditor.error(f"Failed stopping {name}: {e}")

        _stop_qthread(self.scanner, "ScannerBridge", timeout_ms=8000)
        _stop_qthread(self.cluster_worker, "ClusterWorker", timeout_ms=5000)
        _stop_qthread(self.warmup_worker, "EngineWarmupWorker", timeout_ms=5000)
        _stop_qthread(self.maintenance_worker, "MaintenanceWorker", timeout_ms=5000)

        try:
            children = multiprocessing.active_children()
            if children:
                auditor.warning(f"Terminating {len(children)} active child process(es).")
            for p in children:
                try:
                    p.terminate()
                except Exception:
                    pass
            for p in children:
                try:
                    p.join(timeout=2.0)
                except Exception:
                    pass
            for p in children:
                try:
                    if p.is_alive() and hasattr(p, "kill"):
                        p.kill()
                except Exception:
                    pass
        except Exception as e:
            auditor.error(f"Failed to terminate child processes: {e}")