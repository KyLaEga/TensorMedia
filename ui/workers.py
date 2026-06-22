# ============================================================
# MODULE: ui/workers.py
# ============================================================
import sys
import time
import zipfile
import numpy as np
import cv2
from pathlib import Path
from collections import OrderedDict
from PIL import Image
from threading import Condition

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

cv2.setNumThreads(0)

# AVFoundation is macOS-only; on Windows/Linux it does not exist and forcing it
# makes every VideoCapture fail to open. Pick the platform-native backend and
# let OpenCV auto-select (CAP_ANY) everywhere else.
_VIDEO_BACKEND = getattr(cv2, 'CAP_AVFOUNDATION', cv2.CAP_ANY) if sys.platform == "darwin" else cv2.CAP_ANY

from core.ml.cluster_engine import SmartClusterEngine
from utils.i18n import translator
from utils.logger import auditor

class MultiVideoWorker(QThread):
    frame_ready = Signal(str, QImage)

    # Жёсткий бюджет одной видеопробы (открытие + seek + чтение кадра), мс —
    # паритет с CompareVideoWorker: за лимитом проба прерывается пустым кадром,
    # чтобы гигантский контейнер (парс MOOV) не копил «пробку» в очереди воркера.
    _PROBE_BUDGET_MS = 2000
    
    def __init__(self):
        super().__init__()
        self.requests = {} 
        self.caps = OrderedDict() 
        self.max_caps = 4 
        self.is_running = True
        self.cond = Condition()

    def request_frames(self, paths, pct):
        with self.cond:
            self.requests.clear()
            for p in paths:
                self.requests[p] = pct
            # Re-arm under the lock so a concurrent stop() can't interleave
            # between the isRunning() check and start(). Setting the flag before
            # notify also prevents run()'s loop from exiting on a stale False.
            self.is_running = True
            self.cond.notify_all()
            if not self.isRunning():
                self.start()
            
    def _open_capture(self, p):
        """Открывает VideoCapture с таймаутами на открытие/чтение и HW-ускорением;
        откат на дефолтный бэкенд при неудаче. None — открыть не удалось.

        CAP_PROP_OPEN_TIMEOUT_MSEC / READ_TIMEOUT_MSEC — нативный таймаут FFMPEG-
        бэкенда: единственный способ оборвать зависшее открытие/чтение ВНУТРИ
        синхронного вызова cv2. На бэкендах, где свойства игнорируются (напр.
        AVFoundation), страховка — внешний бюджет по wall-clock в run()."""
        # Нативные таймауты OPEN/READ — фича FFMPEG; AVFoundation (macOS) их
        # ОТВЕРГАЕТ: open падает с -213 applyParametersFallback и бэкенд «can't be
        # used to capture by name». Поэтому на darwin их НЕ передаём (там бэкенд
        # AVFoundation) — кросс-бэкендной страховкой остаётся wall-clock бюджет в
        # run(). HW-ускорение AVFoundation допускает, его шлём всегда.
        params = [cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY]
        if sys.platform != "darwin":
            budget = int(self._PROBE_BUDGET_MS)
            params += [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, budget,
                       cv2.CAP_PROP_READ_TIMEOUT_MSEC, budget]
        try:
            cap = cv2.VideoCapture(p, _VIDEO_BACKEND, params)
            if not cap.isOpened():
                cap.release()
                cap = cv2.VideoCapture(p)
        except Exception as e:
            auditor.warning(f"Failed to open video {p} with HW backend: {e}")
            cap = cv2.VideoCapture(p)
        if cap is not None and cap.isOpened():
            return cap
        if cap is not None:
            cap.release()
        return None

    def run(self):
        try:
            while self.is_running:
                with self.cond:
                    while not self.requests and self.is_running:
                        self.cond.wait()
                    
                    if not self.is_running: break
                    paths_to_process = list(self.requests.keys())
                    
                for p in paths_to_process:
                    if not self.is_running: break 
                    
                    with self.cond:
                        pct = self.requests.pop(p, None) 
                        
                    if pct is None: continue
                    
                    try:
                        ext = Path(p).suffix.lower()
                        ret = False
                        frame = None
                        
                        if ext == '.gif':
                            try:
                                with Image.open(p) as img:
                                    tot_frames = getattr(img, "n_frames", 1)
                                    target_frame = min(max(0, int(tot_frames * (pct / 100.0))), tot_frames - 1)
                                    img.seek(target_frame)
                                    frame_pil = img.convert("RGB")
                                    frame = cv2.cvtColor(np.array(frame_pil), cv2.COLOR_RGB2BGR)
                                    ret = True
                            except Exception as e:
                                auditor.warning(f"Failed to process GIF {p}: {e}")

                        elif ext == '.cbz':
                            try:
                                with zipfile.ZipFile(p, 'r') as z:
                                    names = sorted([n for n in z.namelist() if n.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))])
                                    if names:
                                        target_idx = min(max(0, int(len(names) * (pct / 100.0))), len(names) - 1)
                                        with z.open(names[target_idx]) as f:
                                            with Image.open(f) as img_opened:
                                                img_opened.load()
                                                img_opened.thumbnail((1920, 1080), Image.Resampling.LANCZOS)
                                                frame = cv2.cvtColor(np.array(img_opened.convert("RGB")), cv2.COLOR_RGB2BGR)
                                                ret = True
                            except Exception as e:
                                auditor.warning(f"Failed to process CBZ {p}: {e}")

                        elif ext == '.pdf':
                            try:
                                from utils.pdf_render import open_document, render_page
                                doc = open_document(p)
                                try:
                                    tot_frames = len(doc)
                                    if tot_frames > 0:
                                        target_frame = min(max(0, int(tot_frames * (pct / 100.0))), tot_frames - 1)
                                        with render_page(doc, target_frame, 1.0) as img:
                                            frame = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
                                            ret = True
                                finally:
                                    doc.close()
                            except Exception as e:
                                auditor.warning(f"Failed to process PDF {p}: {e}")
                                
                        else:
                            cap = None
                            t0 = time.monotonic()   # старт бюджета видеопробы
                            try:
                                if p not in self.caps:
                                    if len(self.caps) >= self.max_caps:
                                        _, old_cap = self.caps.popitem(last=False)
                                        old_cap.release()
                                    cap = self._open_capture(p)
                                    if cap is None:
                                        # Открытие не удалось/сорвало таймаут —
                                        # пустой кадр, очередь остальных не копим.
                                        self.frame_ready.emit(p, QImage())
                                        continue
                                    self.caps[p] = cap

                                cap = self.caps.pop(p)
                                self.caps[p] = cap

                                # ЧЕКПОЙНТ 1: парс заголовка/MOOV уже съел бюджет.
                                if (time.monotonic() - t0) * 1000.0 > self._PROBE_BUDGET_MS:
                                    auditor.warning(f"Multi probe budget exceeded on open: {p}")
                                    self.frame_ready.emit(p, QImage())
                                    continue

                                tot = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                                if tot > 0:
                                    cap.set(cv2.CAP_PROP_POS_FRAMES, int(tot * (pct / 100.0)))
                                    # ЧЕКПОЙНТ 2: длинный seek сам вышел за лимит.
                                    if (time.monotonic() - t0) * 1000.0 > self._PROBE_BUDGET_MS:
                                        auditor.warning(f"Multi probe budget exceeded on seek: {p}")
                                        self.frame_ready.emit(p, QImage())
                                        continue
                                    ret, frame = cap.read()

                                # Один bounded повтор при сбое чтения — только если
                                # остался бюджет (не давим reopen на «тяжёлый» файл).
                                if not ret and (time.monotonic() - t0) * 1000.0 <= self._PROBE_BUDGET_MS:
                                    cap.release()
                                    self.caps.pop(p, None)
                                    cap = self._open_capture(p)
                                    if cap is not None:
                                        self.caps[p] = cap
                                        ret, frame = cap.read()
                            except Exception as e:
                                auditor.warning(f"Failed to process video {p}: {e}")
                                if cap is not None: cap.release()
                                if p in self.caps: del self.caps[p]
                                self.frame_ready.emit(p, QImage())

                        if ret and frame is not None:
                            h, w = frame.shape[:2]
                            if h > 0 and w > 0:
                                scale = 640.0 / max(w, h)
                                if scale < 1.0:
                                    frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
                                
                                if len(frame.shape) == 2: frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2RGB)
                                elif len(frame.shape) == 3 and frame.shape[2] == 3: frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                                elif len(frame.shape) == 3 and frame.shape[2] == 4: frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGB)
                                    
                                frame = np.ascontiguousarray(frame)
                                h, w, ch = frame.shape
                                bytes_per_line = ch * w
                                
                                qimg = QImage(frame.data, w, h, bytes_per_line, QImage.Format.Format_RGB888).copy()
                                self.frame_ready.emit(p, qimg)
                        else:
                            self.frame_ready.emit(p, QImage())

                    except Exception as e:
                        auditor.error(f"Critical failure in MultiVideoWorker.run for {p}: {e}", exc_info=True)
                        self.frame_ready.emit(p, QImage())
        finally:
            # Принудительная очистка H/W дескрипторов
            for cap in list(self.caps.values()):
                if cap and cap.isOpened():
                    cap.release()
            self.caps.clear()
            
    def stop(self):
        self.is_running = False
        with self.cond:
            self.cond.notify_all()
        self.quit()
        # Join so the GUI can't destroy this QThread (and its VideoCapture
        # handles) while run()/its finally cleanup is still executing.
        self.wait(2000)

class CompareVideoWorker(QThread):
    frame_ready = Signal(str, QImage)

    # ЖЁСТКИЙ БЮДЖЕТ одной пробы (открытие + seek + чтение одного кадра), мс.
    # Гигантский контейнер (1 ГБ / ~100 мин) парсит заголовок/MOOV-атом секунды;
    # за этим лимитом проба прерывается и отдаётся пустой кадр, чтобы медленный
    # файл не копил «пробку» в очереди фонового воркера и не тормозил остальные.
    _PROBE_BUDGET_MS = 2000

    def __init__(self):
        super().__init__()
        self.requests = {}
        self.caps = OrderedDict()
        # Одиночное превью/скраб работает с ОДНИМ видео за раз → держать пул из 12
        # открытых декодеров незачем (это и раздувало RAM до ~1.1 ГБ + плодило
        # AVFoundation/FFmpeg decode-потоки, см. лог нагрузки). 2 хватает: текущий
        # файл + один «прошлый» на случай быстрого переключения туда-обратно.
        self.max_caps = 2
        self.is_running = True
        self.cond = Condition()

    def request_frames(self, paths, pct, cache=False):
        # cache=True (load_video, фикс. pct=25) разрешает дисковый кэш миниатюр;
        # скраб одиночного плеера шлёт cache=False (позиция произвольная → не кэшируем).
        with self.cond:
            for p in paths: self.requests[p] = (pct, cache)
            # Re-arm under the lock (see MultiVideoWorker.request_frames).
            self.is_running = True
            self.cond.notify_all()
            if not self.isRunning():
                self.start()

    def _open_capture(self, p):
        """Открывает VideoCapture с таймаутами на открытие/чтение и HW-ускорением;
        откат на дефолтный бэкенд при неудаче. None — открыть не удалось.

        CAP_PROP_OPEN_TIMEOUT_MSEC / READ_TIMEOUT_MSEC — НАТИВНЫЙ таймаут
        FFMPEG-бэкенда: он один способен оборвать зависшее открытие/чтение ВНУТРИ
        синхронного вызова cv2 (из самого потока его не прервать). На бэкендах,
        где свойства игнорируются (напр. AVFoundation), страховкой служит
        внешний бюджет по wall-clock в run() между шагами пробы."""
        # Нативные таймауты OPEN/READ — фича FFMPEG; AVFoundation (macOS) их
        # ОТВЕРГАЕТ: open падает с -213 applyParametersFallback и бэкенд «can't be
        # used to capture by name». Поэтому на darwin их НЕ передаём (там бэкенд
        # AVFoundation) — кросс-бэкендной страховкой остаётся wall-clock бюджет в
        # run(). HW-ускорение AVFoundation допускает, его шлём всегда.
        params = [cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY]
        if sys.platform != "darwin":
            budget = int(self._PROBE_BUDGET_MS)
            params += [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, budget,
                       cv2.CAP_PROP_READ_TIMEOUT_MSEC, budget]
        try:
            cap = cv2.VideoCapture(p, _VIDEO_BACKEND, params)
            if not cap.isOpened():
                cap.release()
                cap = cv2.VideoCapture(p)
        except Exception as e:
            auditor.warning(f"Failed to open video {p} with HW backend: {e}")
            cap = cv2.VideoCapture(p)
        if cap is not None and cap.isOpened():
            return cap
        if cap is not None:
            cap.release()
        return None

    def run(self):
        try:
            while self.is_running:
                with self.cond:
                    while not self.requests and self.is_running:
                        self.cond.wait()
                    if not self.is_running: break
                    paths_to_process = list(self.requests.keys())

                for p in paths_to_process:
                    if not self.is_running: break

                    with self.cond: req = self.requests.pop(p, None)
                    if req is None: continue
                    pct, want_cache = req

                    # КЭШ МИНИАТЮРЫ (только cache=True): JPEG уже на диске → отдаём
                    # мгновенно, без открытия контейнера. Главный буст «долго грузится».
                    if want_cache:
                        try:
                            from utils.thumb_cache import thumb_path_for
                            tp = thumb_path_for(p)
                            if tp is not None and tp.exists():
                                cached = cv2.imread(str(tp))
                                if cached is not None and cached.size:
                                    cached = np.ascontiguousarray(cv2.cvtColor(cached, cv2.COLOR_BGR2RGB))
                                    hh, ww, cc = cached.shape
                                    self.frame_ready.emit(p, QImage(cached.data, ww, hh, cc * ww, QImage.Format.Format_RGB888).copy())
                                    continue
                        except Exception as e:
                            auditor.debug(f"Thumb cache read failed for {p}: {e}")

                    cap = None
                    t0 = time.monotonic()   # старт отсчёта бюджета пробы
                    try:
                        if p not in self.caps:
                            if len(self.caps) >= self.max_caps:
                                _, old_cap = self.caps.popitem(last=False)
                                old_cap.release()

                            cap = self._open_capture(p)
                            if cap is None:
                                # Контейнер не открылся ИЛИ открытие сорвало
                                # нативный таймаут — пустой кадр, очередь не копим.
                                self.frame_ready.emit(p, QImage())
                                continue
                            self.caps[p] = cap

                        cap = self.caps.pop(p)
                        self.caps[p] = cap

                        # ЧЕКПОЙНТ 1: открытие/готовность контейнера (парс MOOV)
                        # уже съели весь бюджет — кадр не тянем (read добил бы UX
                        # и забил очередь). cap оставляем в кэше: повторный seek
                        # дешевле, чем переоткрытие гигабайтного файла.
                        if (time.monotonic() - t0) * 1000.0 > self._PROBE_BUDGET_MS:
                            auditor.warning(f"Compare probe budget exceeded on open: {p}")
                            self.frame_ready.emit(p, QImage())
                            continue

                        tot = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                        if tot > 0:
                            cap.set(cv2.CAP_PROP_POS_FRAMES, int(tot * (pct / 100.0)))
                            # ЧЕКПОЙНТ 2: один только seek по длинному файлу мог
                            # выйти за лимит — не давим cap.read() поверх.
                            if (time.monotonic() - t0) * 1000.0 > self._PROBE_BUDGET_MS:
                                auditor.warning(f"Compare probe budget exceeded on seek: {p}")
                                self.frame_ready.emit(p, QImage())
                                continue
                        # tot<=0: FRAME_COUNT неизвестен (типично для AVFoundation/macOS
                        # и части контейнеров) → seek по POS_FRAMES невозможен. РАНЬШЕ
                        # эта ветка не эмитила НИЧЕГО → одиночное превью «иногда не
                        # грузилось». Теперь без seek читаем первый декодируемый кадр:
                        # для статичного превью этого достаточно, и заявка не виснет.
                        ret, frame = cap.read()
                        if ret:
                            h, w = frame.shape[:2]
                            scale = min(640.0 / w, 360.0 / h)
                            if scale < 1.0:
                                nw, nh = int(w * scale), int(h * scale)
                                frame = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)

                            # Заполняем дисковый кэш миниатюр (BGR до конверсии) —
                            # следующий показ этого видео будет мгновенным.
                            if want_cache:
                                try:
                                    from utils.thumb_cache import thumb_path_for, save_thumb_bgr
                                    save_thumb_bgr(thumb_path_for(p), frame)
                                except Exception as e:
                                    auditor.debug(f"Thumb cache write failed for {p}: {e}")

                            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                            frame = np.ascontiguousarray(frame)
                            h_new, w_new, ch = frame.shape
                            qimg = QImage(frame.data, w_new, h_new, ch * w_new, QImage.Format.Format_RGB888).copy()
                            self.frame_ready.emit(p, qimg)
                        else:
                            # Кадр не прочитался (в т.ч. по READ_TIMEOUT) —
                            # пустой кадр, чтобы заявка не «висела» в очереди.
                            self.frame_ready.emit(p, QImage())

                        # ОДНОКРАТНЫЙ ПОКАЗ (cache=True): кадр уже на диске —
                        # держать открытый AVFoundation/FFmpeg-декодер незачем. Он
                        # жрёт RAM и плодит decode-потоки (см. лог нагрузки: пул
                        # открытых VideoCapture доминировал в footprint). Закрываем
                        # сразу; следующий показ пойдёт из дискового кэша. Скраб
                        # (cache=False) декодер сохраняет — там нужен быстрый re-seek.
                        if want_cache:
                            c = self.caps.pop(p, None)
                            if c is not None:
                                try: c.release()
                                except Exception: pass
                    except Exception as e:
                        auditor.warning(f"Failed to process video {p} in CompareVideoWorker: {e}")
                        if cap is not None: cap.release()
                        if p in self.caps: del self.caps[p]
                        self.frame_ready.emit(p, QImage())
        finally:
            # Принудительная очистка H/W дескрипторов
            for cap in list(self.caps.values()):
                if cap and cap.isOpened():
                    cap.release()
            self.caps.clear()
            
    def stop(self):
        self.is_running = False
        with self.cond:
            self.cond.notify_all()
        self.quit()
        self.wait(2000)

class ScannerBridge(QThread):
    progress_updated = Signal(int, int, str)
    scan_completed = Signal()
    error_occurred = Signal(str)

    def __init__(self, engine, target_dirs, allowed_exts, mode):
        super().__init__()
        self.engine = engine
        self.target_dirs = target_dirs
        self.allowed_exts = allowed_exts
        self.mode = mode
        self._is_cancelled = False

    def stop(self):
        """Signal SmartClusterEngine + pool abort (may run from GUI/orchestrator thread)."""
        self._is_cancelled = True
        if self.engine is not None and hasattr(self.engine, "request_scan_abort"):
            self.engine.request_scan_abort()
        elif self.engine is not None:
            self.engine.is_stopped = True
        self.requestInterruption()

    def run(self):
        self._is_cancelled = False
        try:
            self.progress_updated.emit(0, 100, f"{translator.tr('scan_prep')} ({self.mode})...")
            self.engine.load_models(self.mode)
            
            if self._is_cancelled or self.isInterruptionRequested():
                return

            self.engine.extract_features(
                self.target_dirs, 
                allowed_exts=self.allowed_exts, 
                progress_callback=lambda c, t, m: self.progress_updated.emit(c, t, m)
            )
            if self._is_cancelled or self.isInterruptionRequested():
                return
            if self.engine is not None and getattr(self.engine, "is_stopped", False):
                return
            self.scan_completed.emit()
        except Exception as e:
            if self._is_cancelled or (self.engine is not None and getattr(self.engine, "is_stopped", False)):
                return
            self.error_occurred.emit(str(e))

class ClusterWorker(QThread):
    clustering_completed = Signal(list)
    
    def __init__(self, engine, threshold):
        super().__init__()
        self.engine = engine
        self.threshold = threshold

    def run(self):
        try:
            clusters = self.engine.build_clusters(self.threshold)
            self.clustering_completed.emit(clusters)
        except Exception as e:
            auditor.error(f"Critical failure in ClusterWorker.run: {e}", exc_info=True)
            self.clustering_completed.emit([])

class EngineWarmupWorker(QThread):
    engine_ready = Signal(object)
    error_occurred = Signal(str)
    
    def run(self):
        try:
            engine = SmartClusterEngine()
            self.engine_ready.emit(engine)
        except Exception as e:
            self.error_occurred.emit(str(e))

class MaintenanceWorker(QThread):
    """Фоновый воркер для очистки и дефрагментации баз данных."""
    finished = Signal()

    def run(self):
        try:
            from utils.batch_operations import DBConnectionPool
            DBConnectionPool.run_maintenance_all()
        except Exception as e:
            auditor.error(f"MaintenanceWorker failed: {e}")
        finally:
            self.finished.emit()


class PurgeWorker(QThread):
    """Фоновый воркер для полного удаления всех проиндексированных данных
    (SQLite-векторы + дисковый кэш FAISS)."""
    finished = Signal()

    def run(self):
        try:
            from utils.batch_operations import DBConnectionPool
            from core.ml.faiss_manager import FaissManager
            DBConnectionPool.purge_all()
            FaissManager.purge_disk_cache()
        except Exception as e:
            auditor.error(f"PurgeWorker failed: {e}")
        finally:
            self.finished.emit()