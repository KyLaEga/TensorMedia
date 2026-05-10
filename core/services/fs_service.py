import os
from PySide6.QtCore import QObject, Qt, QTimer, Signal, QMutex, QMutexLocker, QMetaObject, Q_ARG
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from utils.logger import auditor

class ReactiveFSEventHandler(FileSystemEventHandler):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def on_any_event(self, event):
        if not event.is_directory:
            self.callback(event.src_path)

class FileSystemService(QObject):
    integrity_violation_detected = Signal(list)
    fs_event_triggered = Signal(str)

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        
        self.observer = Observer()
        self.handler = ReactiveFSEventHandler(self._on_fs_event)
        self.active_watches = {}
        
        self.fs_timer = QTimer(self)
        self.fs_timer.setSingleShot(True)
        self.fs_timer.timeout.connect(self._verify_fs_integrity)
        
        # Перенаправление сигнала в event loop Qt
        self.fs_event_triggered.connect(self._handle_fs_signal, Qt.ConnectionType.QueuedConnection)
        
        self._mutex = QMutex()

    def update_watch_paths(self, dirs_to_watch: set):
        self.observer.unschedule_all()
        for d in dirs_to_watch:
            if os.path.exists(d) and os.path.isdir(d):
                self.observer.schedule(self.handler, d, recursive=False)
        
        if dirs_to_watch and not self.observer.is_alive():
            try:
                self.observer.start()
            except RuntimeError:
                pass

    def _on_fs_event(self, path: str):
        # FIX: Запуск таймера переведен в основной поток через QueuedConnection
        self.fs_event_triggered.emit(path)
        
    def _handle_fs_signal(self, path: str):
        if not self.fs_timer.isActive():
            self.fs_timer.start(1000)

    def _verify_fs_integrity(self):
        with QMutexLocker(self._mutex):
            if self.model.rowCount() == 0:
                return
                
            missing_paths = []
            for i in range(self.model.rowCount()):
                group = self.model.item(i, 0)
                for j in range(group.rowCount()):
                    child = group.child(j, 0)
                    data = child.data(Qt.ItemDataRole.UserRole)
                    if data and 'path' in data:
                        if not os.path.exists(data['path']):
                            missing_paths.append(data['path'])
                            
            if missing_paths:
                auditor.info(f"Reactive FS: Detected {len(missing_paths)} deleted files. Emitting pruning signal...")
                self.integrity_violation_detected.emit(missing_paths)

    def stop(self):
        if self.observer.is_alive():
            self.observer.stop()
            self.observer.join()