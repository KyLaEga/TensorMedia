# ============================================================
# MODULE: ui/workers.py
# ============================================================
import os
import time
import zipfile
import io
import numpy as np
import cv2
import threading
from pathlib import Path
from collections import OrderedDict
from PIL import Image
from threading import Condition

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

cv2.setNumThreads(0)

from core.ml.cluster_engine import SmartClusterEngine
from utils.i18n import translator

class MultiVideoWorker(QThread):
    frame_ready = Signal(str, QImage)
    
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
            self.cond.notify_all()
            
        if not self.isRunning(): 
            self.start()
            
    def run(self):
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
                        except Exception: pass

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
                        except Exception: pass

                    elif ext == '.pdf':
                        try:
                            import fitz
                            with fitz.open(p) as doc:
                                tot_frames = len(doc)
                                if tot_frames > 0:
                                    target_frame = min(max(0, int(tot_frames * (pct / 100.0))), tot_frames - 1)
                                    page = doc.load_page(target_frame)
                                    pix = page.get_pixmap(matrix=fitz.Matrix(1.0, 1.0))
                                    with Image.frombytes("RGB", [pix.width, pix.height], pix.samples) as img:
                                        frame = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
                                        ret = True
                        except Exception: pass
                            
                    else:
                        cap = None
                        try:
                            if p not in self.caps:
                                if len(self.caps) >= self.max_caps:
                                    _, old_cap = self.caps.popitem(last=False)
                                    old_cap.release()
                                    
                                # Форсированная HW-инициализация Metal/VideoToolbox
                                try:
                                    cap = cv2.VideoCapture(p, cv2.CAP_AVFOUNDATION, [cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY])
                                    if not cap.isOpened(): cap = cv2.VideoCapture(p)
                                except Exception:
                                    cap = cv2.VideoCapture(p)

                                if cap.isOpened(): 
                                    self.caps[p] = cap
                                else: 
                                    cap.release()
                                    continue
                                
                            cap = self.caps.pop(p)
                            self.caps[p] = cap
                            
                            tot = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                            if tot > 0:
                                cap.set(cv2.CAP_PROP_POS_FRAMES, int(tot * (pct / 100.0)))
                                ret, frame = cap.read()
                                
                            if not ret:
                                cap.release()
                                try:
                                    cap = cv2.VideoCapture(p, cv2.CAP_AVFOUNDATION, [cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY])
                                    if not cap.isOpened(): cap = cv2.VideoCapture(p)
                                except Exception:
                                    cap = cv2.VideoCapture(p)

                                if cap.isOpened():
                                    self.caps[p] = cap
                                    ret, frame = cap.read()
                                else:
                                    cap.release()
                        except Exception:
                            if cap is not None: 
                                cap.release()
                            if p in self.caps: del self.caps[p]

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

                except Exception:
                    self.frame_ready.emit(p, QImage())
            
    def stop(self):
        self.is_running = False
        with self.cond: 
            self.cond.notify_all()
        self.quit()

class CompareVideoWorker(QThread):
    frame_ready = Signal(str, QImage)
    
    def __init__(self):
        super().__init__()
        self.requests = {} 
        self.caps = OrderedDict()
        self.max_caps = 12
        self.is_running = True
        self.cond = Condition()

    def request_frames(self, paths, pct):
        with self.cond:
            for p in paths: self.requests[p] = pct 
            self.cond.notify_all()
        if not self.isRunning(): self.start()
            
    def run(self):
        while self.is_running:
            with self.cond:
                while not self.requests and self.is_running:
                    self.cond.wait()
                if not self.is_running: break
                paths_to_process = list(self.requests.keys())
                
            for p in paths_to_process:
                if not self.is_running: break 
                
                with self.cond: pct = self.requests.pop(p, None) 
                if pct is None: continue
                
                cap = None
                try:
                    if p not in self.caps:
                        if len(self.caps) >= self.max_caps:
                            _, old_cap = self.caps.popitem(last=False)
                            old_cap.release()
                            
                        try:
                            cap = cv2.VideoCapture(p, cv2.CAP_AVFOUNDATION, [cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY])
                            if not cap.isOpened(): cap = cv2.VideoCapture(p)
                        except Exception:
                            cap = cv2.VideoCapture(p)

                        if cap.isOpened(): self.caps[p] = cap
                        else: 
                            cap.release()
                            continue
                            
                    cap = self.caps.pop(p)
                    self.caps[p] = cap
                    
                    tot = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                    if tot > 0:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, int(tot * (pct / 100.0)))
                        ret, frame = cap.read()
                        if ret:
                            h, w = frame.shape[:2]
                            scale = min(640.0 / w, 360.0 / h)
                            if scale < 1.0:
                                nw, nh = int(w * scale), int(h * scale)
                                frame = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
                                
                            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                            frame = np.ascontiguousarray(frame)
                            h_new, w_new, ch = frame.shape
                            qimg = QImage(frame.data, w_new, h_new, ch * w_new, QImage.Format.Format_RGB888).copy()
                            self.frame_ready.emit(p, qimg)
                except Exception:
                    if cap is not None: 
                        cap.release()
                    if p in self.caps: del self.caps[p]
            
    def stop(self):
        self.is_running = False
        with self.cond: 
            self.cond.notify_all()
        self.quit()

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

    def run(self):
        try:
            self.progress_updated.emit(0, 100, f"{translator.tr('scan_prep')} ({self.mode})...")
            self.engine.load_models(self.mode)
            self.engine.extract_features(
                self.target_dirs, 
                allowed_exts=self.allowed_exts, 
                progress_callback=lambda c, t, m: self.progress_updated.emit(c, t, m)
            )
            self.scan_completed.emit()
        except Exception as e: 
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
        except Exception:
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