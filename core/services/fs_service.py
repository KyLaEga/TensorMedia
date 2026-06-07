import os
import time
import shutil
import platform
import subprocess
from pathlib import Path
from PySide6.QtCore import QObject, Qt, QTimer, Signal, QMutex, QMutexLocker, QMetaObject, Q_ARG, QThread
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from utils.logger import auditor


def _invalidate_cache_paths(paths):
    """Удаляет записи из ОБОИХ кэшей векторов (meta_v2_visual/faces.db) для
    указанных путей. Реальный путь удаления (SafeFSExecutor) раньше БД не трогал,
    поэтому записи удалённых файлов висели в кэше до плановой GC. Путь передаём
    как есть (он совпадает с ключом, под которым его сохранил сканер) плюс
    abspath-форму — лишние ключи безвредны (DELETE ... WHERE path=? ничего не
    тронет)."""
    if not paths:
        return
    try:
        from utils.batch_operations import DBConnectionPool
        keys = []
        for p in paths:
            s = str(p)
            keys.append(s)
            ab = os.path.normpath(os.path.abspath(s))
            if ab != s:
                keys.append(ab)
        for mode in ("visual", "faces"):
            DBConnectionPool.get_connection(f"meta_v2_{mode}.db").delete_paths(keys)
    except Exception as e:
        auditor.warning(f"Vector-cache invalidation (delete) failed: {e}")


def _remap_cache_paths(pairs):
    """Переносит записи кэша src→dst в обоих режимах при перемещении файла,
    чтобы кэшированный вектор не терялся и не пересчитывался после move."""
    if not pairs:
        return
    try:
        from utils.batch_operations import DBConnectionPool
        norm = []
        for src, dst in pairs:
            norm.append((str(src), str(dst)))
            ab_src = os.path.normpath(os.path.abspath(str(src)))
            if ab_src != str(src):
                norm.append((ab_src, str(dst)))
        for mode in ("visual", "faces"):
            DBConnectionPool.get_connection(f"meta_v2_{mode}.db").move_paths(norm)
    except Exception as e:
        auditor.warning(f"Vector-cache remap (move) failed: {e}")


class SafeFSExecutor:
    @staticmethod
    def move_files(paths, dest_dir):
        success = 0
        moved_pairs = []
        for p in paths:
            try:
                dst = os.path.join(dest_dir, os.path.basename(p))
                shutil.move(p, dst)
                success += 1
                moved_pairs.append((p, dst))
            except Exception as e:
                auditor.error(f"Move failed for {p}: {e}")
        _remap_cache_paths(moved_pairs)
        return {"deleted": 0, "moved": success, "failed": len(paths) - success}

    @staticmethod
    def hard_delete(paths):
        success = 0
        removed = []
        for p in paths:
            try:
                if os.path.isfile(p) or os.path.islink(p): os.remove(p)
                elif os.path.isdir(p): shutil.rmtree(p)
                success += 1
                removed.append(p)
            except Exception as e:
                auditor.error(f"Hard delete failed for {p}: {e}")
        _invalidate_cache_paths(removed)
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
                _invalidate_cache_paths([p for p in paths if not os.path.exists(os.path.abspath(str(p)))])
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
                _invalidate_cache_paths([p for p in paths if not os.path.exists(os.path.abspath(str(p)))])
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

        _invalidate_cache_paths([p for p in paths if not os.path.exists(os.path.abspath(str(p)))])
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
            except RuntimeError as e:
                # A watchdog Observer is a thread and cannot be restarted once
                # stopped/joined. Recreate it so FS monitoring survives a
                # stop()/restart cycle instead of silently dying.
                auditor.error(f"Observer.start() failed, recreating observer: {e}", exc_info=True)
                self.observer = Observer()
                for d in dirs_to_watch:
                    if os.path.exists(d) and os.path.isdir(d):
                        self.observer.schedule(self.handler, d, recursive=False)
                try:
                    self.observer.start()
                except RuntimeError as e2:
                    auditor.error(f"Observer restart failed: {e2}", exc_info=True)

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