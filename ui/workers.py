import os
import time
import zipfile
import io
import numpy as np
import cv2
from pathlib import Path
from collections import OrderedDict
from PIL import Image
from threading import Lock

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QImage

from core.ml.cluster_engine import SmartClusterEngine
from utils.i18n import translator

class MultiVideoWorker(QThread):
    frame_ready = pyqtSignal(str, QImage)
    
    def __init__(self):
        super().__init__()
        self.requests = {} 
        self.caps = OrderedDict() 
        self.max_caps = 4 
        self.is_running = True
        self.lock = Lock()
        
    def request_frames(self, paths, pct):
        with self.lock:
            self.requests.clear()
            for p in paths:
                self.requests[p] = pct
        if not self.isRunning(): 
            self.start()
            
    def run(self):
        while self.is_running:
            with self.lock:
                paths_to_process = list(self.requests.keys())
                
            if not paths_to_process:
                time.sleep(0.01)
                continue
                
            for p in paths_to_process:
                if not self.is_running: break 
                
                with self.lock:
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
                                target_frame = int(tot_frames * (pct / 100.0))
                                target_frame = min(max(0, target_frame), tot_frames - 1)
                                img.seek(target_frame)
                                frame_pil = img.convert("RGB")
                                frame = np.array(frame_pil)
                                frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                                ret = True
                        except Exception:
                            ret = False

                    elif ext == '.cbz':
                        try:
                            with zipfile.ZipFile(p, 'r') as z:
                                names = sorted([n for n in z.namelist() if n.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))])
                                if names:
                                    target_idx = min(max(0, int(len(names) * (pct / 100.0))), len(names) - 1)
                                    with z.open(names[target_idx]) as f:
                                        img = Image.open(io.BytesIO(f.read())).convert("RGB")
                                        frame = np.array(img)
                                        frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                                        ret = True
                        except Exception:
                            ret = False

                    elif ext == '.pdf':
                        try:
                            import fitz
                            doc = fitz.open(p)
                            tot_frames = len(doc)
                            if tot_frames > 0:
                                target_frame = int(tot_frames * (pct / 100.0))
                                target_frame = min(max(0, target_frame), tot_frames - 1)
                                page = doc.load_page(target_frame)
                                pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
                                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                                frame = np.array(img)
                                frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                                ret = True
                        except Exception:
                            ret = False
                            
                    else:
                        if p not in self.caps:
                            if len(self.caps) >= self.max_caps:
                                _, old_cap = self.caps.popitem(last=False)
                                old_cap.release()
                                
                            cap = cv2.VideoCapture(p) 
                            if cap.isOpened(): 
                                self.caps[p] = cap
                            else: 
                                continue
                            
                        cap = self.caps.pop(p)
                        self.caps[p] = cap
                        
                        tot = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                        if tot > 0:
                            cap.set(cv2.CAP_PROP_POS_FRAMES, int(tot * (pct / 100.0)))
                            ret, cap_frame = cap.read()
                            frame = cap_frame
                            
                        if not ret:
                            cap.release()
                            cap = cv2.VideoCapture(p)
                            self.caps[p] = cap
                            ret, frame = cap.read()

                    if ret and frame is not None:
                        h, w = frame.shape[:2]
                        if h > 0 and w > 0:
                            scale = 640.0 / max(w, h)
                            if scale < 1.0:
                                frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
                            
                            if len(frame.shape) == 2:
                                frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2RGB)
                            elif len(frame.shape) == 3 and frame.shape[2] == 3:
                                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                            elif len(frame.shape) == 3 and frame.shape[2] == 4:
                                frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGB)
                            
                            frame = np.ascontiguousarray(frame)
                            h, w, ch = frame.shape
                            bytes_per_line = ch * w
                            
                            qimg = QImage(frame.data, w, h, bytes_per_line, QImage.Format.Format_RGB888).copy()
                            self.frame_ready.emit(p, qimg)
                    else:
                        self.frame_ready.emit(p, QImage())

                except Exception:
                    self.frame_ready.emit(p, QImage())
                    
            time.sleep(0.005) 
            
    def stop(self):
        self.is_running = False
        self.wait()
        for cap in self.caps.values(): 
            cap.release()
        self.caps.clear()

class ScannerBridge(QThread):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, engine, target_dirs, allowed_exts, mode):
        super().__init__()
        self.engine = engine
        self.target_dirs = target_dirs
        self.allowed_exts = allowed_exts
        self.mode = mode

    def run(self):
        try:
            self.progress.emit(0, 100, f"{translator.tr('scan_prep')} ({self.mode})...")
            self.engine.load_models(self.mode)
            self.engine.extract_features(self.target_dirs, allowed_exts=self.allowed_exts, progress_callback=lambda c, t, m: self.progress.emit(c, t, m))
            self.finished.emit()
        except Exception as e: 
            self.error.emit(str(e))

class ClusterWorker(QThread):
    finished = pyqtSignal(list)
    def __init__(self, engine, threshold):
        super().__init__()
        self.engine = engine
        self.threshold = threshold

    def run(self):
        clusters = self.engine.build_clusters(self.threshold)
        self.finished.emit(clusters)

class EngineWarmupWorker(QThread):
    engine_ready = pyqtSignal(object)
    
    def run(self):
        engine = SmartClusterEngine()
        self.engine_ready.emit(engine)