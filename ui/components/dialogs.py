# ============================================================
# MODULE: ui/components/dialogs.py
# ============================================================
import os
import io
import zipfile
import cv2
from pathlib import Path
from PIL import Image
from utils.logger import auditor

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
                               QScrollArea, QWidget, QGridLayout, QPushButton, 
                               QSizePolicy, QRadioButton)
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QPixmap, QImage

from ui.components.image_label import ScalableImageLabel
from utils.i18n import translator

class ThumbnailWorker(QThread):
    thumbnail_ready = Signal(int, QImage)

    def __init__(self, files):
        super().__init__()
        self.files = files
        self.is_running = True

    def run(self):
        for i, p in enumerate(self.files):
            if not self.is_running:
                break
            ext = Path(p).suffix.lower()
            qim = QImage()
            try:
                if ext in {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'}:
                    cap = cv2.VideoCapture(p)
                    if cap.isOpened():
                        tot = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                        cap.set(cv2.CAP_PROP_POS_FRAMES, int(tot * 0.15) if tot > 0 else 0)
                        ret, frame = cap.read()
                        if ret:
                            h, w = frame.shape[:2]
                            scale = 400.0 / max(w, h)
                            frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
                            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                            qim = QImage(frame.data, frame.shape[1], frame.shape[0], frame.shape[1] * 3, QImage.Format.Format_RGB888).copy()
                    cap.release()
                elif ext == '.pdf':
                    import fitz
                    with fitz.open(p) as doc:
                        if len(doc) > 0:
                            page = doc.load_page(0)
                            pix = page.get_pixmap(matrix=fitz.Matrix(0.5, 0.5))
                            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                            img.thumbnail((400, 400))
                            qim = QImage(img.tobytes("raw", "RGB"), img.width, img.height, img.width * 3, QImage.Format.Format_RGB888).copy()
                elif ext == '.cbz':
                    with zipfile.ZipFile(p, 'r') as z:
                        names = sorted([n for n in z.namelist() if n.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))])
                        if names:
                            with z.open(names[0]) as f:
                                img = Image.open(io.BytesIO(f.read())).convert("RGB")
                                img.thumbnail((400, 400))
                                qim = QImage(img.tobytes("raw", "RGB"), img.width, img.height, img.width * 3, QImage.Format.Format_RGB888).copy()
                else:
                    temp_img = QImage(p)
                    if not temp_img.isNull():
                        qim = temp_img.scaled(400, 400, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            except Exception as e:
                # Маршрутизация ошибок в системный лог вместо стандартного вывода
                auditor.warning(f"ThumbnailWorker Error {p}: {e}")
            
            self.thumbnail_ready.emit(i, qim)

    def stop_and_detach(self):
        self.is_running = False
        self.quit()

class VisualDeleteDialog(QDialog):
    _orphaned_workers = []

    def __init__(self, files, parent=None):
        super().__init__(parent)
        self.setWindowTitle(translator.tr("dialog_del_preview"))
        self.resize(850, 600)
        self.delete_hard = False
        self.files = files
        self.labels = {}
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)
        
        total_bytes = 0
        for p in files:
            try:
                if os.path.exists(p) and os.path.isfile(p):
                    total_bytes += os.path.getsize(p)
            except OSError as e:
                from utils.logger import auditor
                auditor.warning(f"Failed to get size for {p}: {e}")
        
        size_mb = total_bytes / (1024 * 1024)
        
        info_html = (
            f"<div style='margin-bottom: 5px;'>"
            f"<span style='font-size: 16px; color: #DA3633; font-weight: bold;'>{translator.tr('dialog_del_warn')}</span><br>"
            f"<span style='font-size: 14px; color: #DCDDDE;'>{translator.tr('dialog_del_files').format(len(files))}</span><br>"
            f"<span style='font-size: 14px; color: #DCDDDE;'>{translator.tr('dialog_del_space').format(size_mb)}</span>"
            f"</div>"
        )
        layout.addWidget(QLabel(info_html))
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: 1px solid #3F4147; border-radius: 8px; background-color: #1E1E22; }")
        
        self.grid_container = QWidget()
        self.grid = QGridLayout(self.grid_container)
        # Установка марджина и отступов 1% от ширины диалога
        margin = max(10, int(self.width() * 0.01))
        self.grid.setSpacing(margin)
        self.grid.setContentsMargins(margin, margin, margin, margin)
        
        count = len(files)
        if count <= 2: cols = 2
        elif count <= 4: cols = 2
        elif count <= 9: cols = 3
        else: cols = 4
        
        for i, p in enumerate(files):
            ext = Path(p).suffix.lower()
            
            card_frame = QWidget()
            card_frame.setMinimumHeight(220)
            card_frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            card_frame.setStyleSheet("background-color: #2B2D31; border-radius: 6px;")
            
            vbox = QVBoxLayout(card_frame)
            vbox.setContentsMargins(4, 4, 4, 4) 
            vbox.setSpacing(4)
            
            img_lbl = ScalableImageLabel()
            img_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            
            self.labels[i] = (img_lbl, ext)
            
            name_lbl = QLabel()
            name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            name_lbl.setStyleSheet("font-size: 11px; color: #DCDDDE; padding: 2px;")
            name_lbl.setToolTip(Path(p).name)
            
            fm = name_lbl.fontMetrics()
            name_lbl.setText(fm.elidedText(Path(p).name, Qt.TextElideMode.ElideMiddle, 160))

            vbox.addWidget(img_lbl, stretch=1)
            vbox.addWidget(name_lbl)
            
            self.grid.addWidget(card_frame, i // cols, i % cols)
            
        total_rows = (count - 1) // cols + 1
        self.grid.setRowStretch(total_rows, 1)
        
        scroll.setWidget(self.grid_container)
        layout.addWidget(scroll, stretch=1)
        
        btn_layout = QHBoxLayout()
        btn_safe = QPushButton(translator.tr("btn_safe_del"))
        btn_safe.setMinimumHeight(40)
        btn_safe.setObjectName("secondary")
        btn_safe.clicked.connect(self._safe_del)
        
        self.rb_hard = QRadioButton()
        
        btn_hard = QPushButton(translator.tr("btn_hard_del"))
        btn_hard.setMinimumHeight(40)
        btn_hard.setStyleSheet("QPushButton { background-color: #DA3633; color: white; font-weight: bold; border: none; border-radius: 6px; padding: 0 15px; } QPushButton:hover { background-color: #C02E2B; }")
        btn_hard.clicked.connect(self._hard_del)
        
        btn_cancel = QPushButton(translator.tr("btn_cancel"))
        btn_cancel.setMinimumHeight(40)
        btn_cancel.setObjectName("secondary")
        btn_cancel.clicked.connect(self.reject)
        
        btn_layout.addWidget(btn_safe)
        btn_layout.addWidget(btn_hard)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_cancel)
        layout.addLayout(btn_layout)

        self.worker = ThumbnailWorker(self.files)
        self.worker.thumbnail_ready.connect(self._on_thumbnail_ready)
        self.worker.start()
        
    def _on_thumbnail_ready(self, index, qimage):
        if index in self.labels:
            img_lbl, ext = self.labels[index]
            if not qimage.isNull():
                img_lbl.setPixmap(QPixmap.fromImage(qimage))
            else:
                icon_text = "🎥 ERROR" if ext in {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'} else "📄 ERROR"
                if hasattr(img_lbl, 'clear_view'): img_lbl.clear_view()
                img_lbl.setText(icon_text)
                img_lbl.setStyleSheet("font-size: 14px; font-weight: bold; color: #DA3633;")

    def _cleanup_labels(self):
        for img_lbl, _ in self.labels.values():
            if hasattr(img_lbl, 'clear_view'):
                img_lbl.clear_view()

    def _detach_worker(self):
        if self.worker.isRunning():
            self.worker.setParent(None)
            VisualDeleteDialog._orphaned_workers.append(self.worker)
            self.worker.finished.connect(lambda w=self.worker: VisualDeleteDialog._orphaned_workers.remove(w) if w in VisualDeleteDialog._orphaned_workers else None)
            self.worker.finished.connect(self.worker.deleteLater)
            self.worker.stop_and_detach()
        else:
            self.worker.deleteLater()

    def reject(self):
        self._detach_worker()
        self._cleanup_labels()
        super().reject()

    def _safe_del(self):
        self._detach_worker()
        self.delete_hard = False
        self._cleanup_labels()
        self.accept()
        
    def _hard_del(self):
        self._detach_worker()
        self.delete_hard = True
        self._cleanup_labels()
        self.accept()