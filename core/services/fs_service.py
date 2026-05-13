import os
import shutil
import platform
import subprocess
from pathlib import Path
from PySide6.QtCore import QObject, Qt, QTimer, Signal, QMutex, QMutexLocker, QMetaObject, Q_ARG, QThread
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from utils.logger import auditor

class SafeFSExecutor:
    @staticmethod
    def move_files(paths, dest_dir):
        success = 0
        for p in paths:
            try:
                shutil.move(p, os.path.join(dest_dir, os.path.basename(p)))
                success += 1
            except Exception as e:
                auditor.error(f"Move failed for {p}: {e}")
        return {"deleted": 0, "moved": success, "failed": len(paths) - success}

    @staticmethod
    def hard_delete(paths):
        success = 0
        for p in paths:
            try:
                if os.path.isfile(p) or os.path.islink(p): os.remove(p)
                elif os.path.isdir(p): shutil.rmtree(p)
                success += 1
            except Exception as e:
                auditor.error(f"Hard delete failed for {p}: {e}")
        return {"deleted": success, "moved": 0, "failed": len(paths) - success}
        
    @staticmethod
    def safe_delete(paths):
        success = 0
        failed = 0
        import platform
        is_macos = platform.system() == "Darwin"
        
        if is_macos:
            # На macOS используем каскадный метод:
            # 1. NSWorkspace (самый тихий и быстрый)
            # 2. send2trash (лучше работает на внешних дисках)
            # 3. AppleScript (последний шанс)
            
            try:
                from AppKit import NSWorkspace, NSURL
                from Foundation import NSArray
                from send2trash import send2trash as s2t_macos
                
                ws = NSWorkspace.sharedWorkspace()
                for p in paths:
                    abs_path = os.path.abspath(str(p))
                    if not os.path.exists(abs_path):
                        failed += 1
                        continue
                    
                    # Попытка 1: NSWorkspace (Native macOS)
                    try:
                        file_url = NSURL.fileURLWithPath_(abs_path)
                        urls = NSArray.arrayWithObject_(file_url)
                        ws.recycleURLs_completionHandler_(urls, None)
                        
                        # NSWorkspace асинхронен, даем небольшую паузу и проверяем
                        time.sleep(0.1)
                        if not os.path.exists(abs_path):
                            success += 1
                            continue
                    except Exception as e:
                        auditor.warning(f"NSWorkspace failed for {p}: {e}")
                    
                    # Попытка 2: send2trash
                    try:
                        s2t_macos(abs_path)
                        if not os.path.exists(abs_path):
                            success += 1
                            continue
                    except Exception as e:
                        auditor.warning(f"send2trash failed for {p}: {e}")
                    
                    # Попытка 3: AppleScript (Finder)
                    try:
                        # Используем quoted form для путей с пробелами и спецсимволами
                        script = f'tell application "Finder" to delete POSIX file "{abs_path}"'
                        subprocess.run(['osascript', '-e', script], check=True, capture_output=True)
                        
                        time.sleep(0.2) # Finder может быть медленным
                        if not os.path.exists(abs_path):
                            success += 1
                            continue
                    except Exception as e:
                        auditor.error(f"All trash methods failed for {p}: {e}")
                        failed += 1
                return {"deleted": success, "moved": 0, "failed": failed}
            except ImportError:
                auditor.warning("AppKit/send2trash missing, falling back to AppleScript...")
                for p in paths:
                    try:
                        abs_path = os.path.abspath(str(p))
                        script = f'tell application "Finder" to delete POSIX file "{abs_path}"'
                        subprocess.run(['osascript', '-e', script], check=True, capture_output=True)
                        if not os.path.exists(abs_path):
                            success += 1
                        else: failed += 1
                    except Exception as e:
                        auditor.error(f"AppleScript fallback failed for {p}: {e}")
                        failed += 1
                return {"deleted": success, "moved": 0, "failed": failed}

        # Для Windows и Linux оставляем send2trash
        try:
            from send2trash import send2trash
            for p in paths:
                try:
                    abs_path = os.path.abspath(str(p))
                    if os.path.exists(abs_path):
                        send2trash(abs_path)
                        success += 1
                    else:
                        failed += 1
                except Exception as e:
                    auditor.error(f"Send2Trash failed for {p}: {e}")
                    failed += 1
        except ImportError:
            auditor.error("CRITICAL: send2trash module not found.")
            return {"deleted": 0, "moved": 0, "failed": len(paths), "error": "send2trash_missing"}
            
        return {"deleted": success, "moved": 0, "failed": failed}

def reveal_in_os(path: str):
    sys_name = platform.system()
    clean_path = str(Path(path).resolve().absolute())
    try:
        if sys_name == "Windows":
            subprocess.run(['explorer', '/select,', clean_path], shell=False) # nosec B603 B607
        elif sys_name == "Darwin":
            subprocess.run(['open', '-R', clean_path], shell=False) # nosec B603 B607
        else:
            subprocess.run(['xdg-open', os.path.dirname(clean_path)], shell=False) # nosec B603 B607
    except Exception as e:
        auditor.error(f"OS Explorer reveal failed: {e}")

class BatchOpWorker(QThread):
    finished = Signal(object)
    
    def __init__(self, func, *args, **kwargs):
        super().__init__()
        self.func = func
        self.args = args
        self.kwargs = kwargs
        
    def run(self):
        try:
            res = self.func(*self.args, **self.kwargs)
            self.finished.emit(res)
        except Exception as e:
            auditor.error(f"Batch operation failed: {e}")
            self.finished.emit({"deleted": 0, "moved": 0, "failed": len(self.args[0])})

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
        self.pending_missing_paths = set()
        
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
        with QMutexLocker(self._mutex):
            if not os.path.exists(path):
                self.pending_missing_paths.add(path)
        
        if not self.fs_timer.isActive() and self.pending_missing_paths:
            self.fs_timer.start(1000)

    def _verify_fs_integrity(self):
        with QMutexLocker(self._mutex):
            if not self.pending_missing_paths:
                return
            missing_paths = list(self.pending_missing_paths)
            self.pending_missing_paths.clear()

        if missing_paths:
            auditor.info(f"Reactive FS: Detected {len(missing_paths)} deleted files. Emitting pruning signal...")
            self.integrity_violation_detected.emit(missing_paths)

    def stop(self):
        if self.observer.is_alive():
            self.observer.stop()
            self.observer.join()