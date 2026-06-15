import gc
import time
import psutil
import multiprocessing
from PySide6.QtCore import QObject, QTimer
from core.events import bus
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
        self.scanner.scan_completed.connect(self._on_scan_complete)
        self.scanner.error_occurred.connect(self._handle_scan_error)
        self.scanner.start()

    def _on_scan_complete(self):
        # Bound slot on this QObject (GUI thread). A free nested function would
        # connect as a direct functor and execute in the ScannerBridge worker
        # thread, where QTimer.start/stop is illegal ("Timers cannot be started
        # from another thread") and the idle-timer reset is silently dropped.
        self._reset_idle_timer()
        self._emit_telemetry("Scan+Vectorize")
        auditor.info("Scan Matrix processing complete.")
        bus.evt_scan_completed.emit()

    def _handle_scan_error(self, err_msg):
        self._reset_idle_timer()
        auditor.error(f"Scan Matrix pipeline crashed: {err_msg}")
        bus.evt_scan_error.emit(err_msg)

    def _handle_pause(self):
        if self.engine:
            # is_paused is a plain bool written here (GUI thread) and polled by
            # the extract_features() worker thread. A single bool read/write is
            # atomic under the GIL and the flag is advisory (worst case: one
            # extra poll iteration before the pause takes effect), so no lock is
            # required.
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
        self.cluster_worker.clustering_completed.connect(self._on_cluster_complete)
        self.cluster_worker.start()

    def _on_cluster_complete(self, res):
        # Bound slot on this QObject (GUI thread) — see _on_scan_complete: keeps
        # QTimer/bus access off the ClusterWorker thread.
        self._reset_idle_timer()
        self._emit_telemetry("FAISS_Clustering")
        bus.evt_clustering_completed.emit(res)

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
                # Ограниченный wait() даёт run() шанс выйти штатно (чтобы Qt не
                # разрушил QThread «на ходу» → SIGABRT). НО terminate() здесь
                # ЗАПРЕЩЁН: воркер может стоять в нативном коде (faiss/OpenMP,
                # torch). QThread.terminate() обрывает поток посреди OpenMP-секции,
                # оставляя залоченными OMP/allocator-локи → следующий gc.collect()
                # дедлочится навсегда (регресс закрытия после FAISS re-clustering).
                # Поэтому просто бросаем ещё живой поток: финальный os._exit(0)
                # убьёт его мгновенно, без запуска C++-деструктора.
                if hasattr(t, "wait") and not t.wait(2000):
                    auditor.warning(f"Thread {name} did not stop in 2s; abandoning to os._exit")
            except Exception as e:
                auditor.error(f"Failed stopping {name}: {e}")

        _soft_stop_qthread(self.scanner, "ScannerBridge")
        _soft_stop_qthread(self.cluster_worker, "ClusterWorker")
        _soft_stop_qthread(self.warmup_worker, "EngineWarmupWorker")
        _soft_stop_qthread(self.maintenance_worker, "MaintenanceWorker")

        # GRACEFUL TEARDOWN ПУЛА multiprocessing — СИНХРОННО, до выхода процесса.
        # К этому моменту ScannerBridge уже остановлен (его wait() выше дал
        # extract_features() отработать свой finally и закрыть собственный пул),
        # поэтому гонки с кооперативным teardown'ом воркера больше нет — раньше
        # это делалось в daemon-потоке со sleep(1.5), который os._exit на выходе
        # просто не успевал доработать, и семафоры утекали. Теперь доводим всё до
        # конца здесь: terminate()/join() освобождает SemLock'и пула, иначе при
        # hard-exit в консоль падает 'leaked semaphore objects'.
        if self.engine is not None and hasattr(self.engine, "shutdown_pool"):
            try:
                self.engine.shutdown_pool()
            except Exception as e:
                auditor.error(f"Engine pool shutdown failed: {e}", exc_info=True)

        # Подбираем всё, что ещё живо (на случай зависшего воркера/таймаута wait):
        # terminate + join СИНХРОННО, чтобы семафоры дочерних процессов
        # освободились ДО os._exit, а не утекли в resource_tracker.
        try:
            children = multiprocessing.active_children()
            if children:
                auditor.warning(f"Reaping {len(children)} live child process(es) on shutdown.")
            for p in children:
                try:
                    p.terminate()
                except Exception as e:
                    auditor.warning(f"Failed to terminate child {getattr(p, 'pid', '?')}: {e}", exc_info=True)
            for p in children:
                try:
                    p.join(timeout=2.0)
                    # Воркер мог проигнорировать SIGTERM (застрял в нативном коде):
                    # join по таймауту вернётся, а процесс останется жив и удержит
                    # семафоры. Добиваем SIGKILL, иначе teardown не детерминирован.
                    if p.is_alive():
                        auditor.warning(f"Child {getattr(p, 'pid', '?')} survived terminate -> kill()")
                        if hasattr(p, "kill"):
                            p.kill()
                        p.join(timeout=1.0)
                except Exception as e:
                    auditor.debug(f"Child {getattr(p, 'pid', '?')} join failed: {e}", exc_info=True)
        except Exception as e:
            auditor.error(f"Failed to reap child processes: {e}", exc_info=True)

        # ПОСЛЕДНИЙ ШАГ — детерминированный teardown multiprocessing.
        # Pool/Value держат POSIX-семафоры (SemLock), чьи имена снимаются
        # (sem_unlink) и снимаются с учёта в resource_tracker только их
        # финализаторами. terminate()/join() этого НЕ делает.
        #
        # gc.collect() финализирует лишь те объекты, на которые УЖЕ нет ссылок;
        # если хоть один SemLock ещё жив (например, главный поток держит
        # Value(lock=True) или внутренний lock Pool'а, чей хэндлер-поток не
        # доехал до конца), gc его не тронет — и при os._exit(0) resource_tracker
        # печатает 'leaked semaphore objects' (ровно 1 на каждый такой SemLock).
        #
        # Поэтому вместо «мягкого» gc вызываем штатный atexit-обработчик
        # multiprocessing — _exit_function(). Он принудительно прогоняет ВСЕ
        # util.Finalize-финализаторы (sem_unlink + UNREGISTER в resource_tracker)
        # независимо от живых Python-ссылок и добивает дочерние процессы. Именно
        # этот хук пропускает os._exit(0); вызвав его руками ДО hard-exit, мы
        # гарантируем нулевую утечку семафоров. Вызов идемпотентен и безопасен,
        # даже если multiprocessing в этой сессии вообще не использовался.
        try:
            gc.collect()
        except Exception as e:
            auditor.debug(f"Final gc.collect() during teardown failed: {e}", exc_info=True)

        # _exit_function() внутри делает p.join() БЕЗ таймаута по всем живым
        # детям. Сами дети к этому моменту уже сняты SIGKILL'ом в reaping-цикле
        # выше (active_children пуст), поэтому join тривиален. Но чтобы наглухо
        # исключить регресс «незавершающегося закрытия» (см. историю с unbounded
        # Pool.join()), гоним хук в daemon-watchdog'е с ограниченным ожиданием:
        # не уложился — бросаем на os._exit(0) (в худшем случае останется лишь
        # косметический warning resource_tracker, но процесс НЕ виснет).
        try:
            import threading
            from multiprocessing.util import _exit_function
            et = threading.Thread(target=_exit_function, daemon=True)
            et.start()
            et.join(3.0)
            if et.is_alive():
                auditor.warning("multiprocessing _exit_function() timeout; abandoning to hard-exit")
        except Exception as e:
            auditor.debug(f"multiprocessing _exit_function() during teardown failed: {e}", exc_info=True)