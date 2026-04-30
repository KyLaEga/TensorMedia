import os
import io
import zipfile
import cv2
import numpy as np
from pathlib import Path
from PIL import Image

from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
                             QScrollArea, QWidget, QGridLayout, QPushButton)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap, QImage

class VisualDeleteDialog(QDialog):
    def __init__(self, files, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Предпросмотр транзакции удаления")
        self.resize(800, 550)
        self.delete_hard = False
        
        layout = QVBoxLayout(self)
        
        total_bytes = sum(os.path.getsize(p) for p in files if os.path.exists(p) and os.path.isfile(p))
        size_mb = total_bytes / (1024 * 1024)
        
        info_html = (
            f"<div style='margin-bottom: 10px;'>"
            f"<span style='font-size: 16px; color: #DA3633; font-weight: bold;'>Транзакция удаления</span><br>"
            f"<span style='font-size: 14px; color: #DCDDDE;'>Файлов к уничтожению: <b>{len(files)}</b></span><br>"
            f"<span style='font-size: 14px; color: #DCDDDE;'>Освобождаемое пространство: <b>{size_mb:.2f} MB</b></span>"
            f"</div>"
        )
        lbl = QLabel(info_html)
        layout.addWidget(lbl)
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background-color: #1E1E22; }")
        
        container = QWidget()
        grid = QGridLayout(container)
        grid.setSpacing(10)
        
        for r in range(50):
            grid.setRowStretch(r, 0)
        
        cols = 4
        total_rows = (len(files) - 1) // cols + 1
        
        for i, p in enumerate(files):
            ext = Path(p).suffix.lower()
            pixmap = None
            
            if ext in {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'}:
                try:
                    cap = cv2.VideoCapture(p)
                    if cap.isOpened():
                        tot = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                        cap.set(cv2.CAP_PROP_POS_FRAMES, int(tot * 0.15) if tot > 0 else 0)
                        ret, frame = cap.read()
                        if ret:
                            h, w = frame.shape[:2]
                            scale = 160.0 / max(w, h)
                            frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
                            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                            qim = QImage(frame.data, frame.shape[1], frame.shape[0], frame.shape[1] * 3, QImage.Format.Format_RGB888).copy()
                            pixmap = QPixmap.fromImage(qim)
                    cap.release()
                except: pass
                
            elif ext == '.pdf':
                try:
                    import fitz
                    doc = fitz.open(p)
                    if len(doc) > 0:
                        page = doc.load_page(0)
                        pix = page.get_pixmap(matrix=fitz.Matrix(0.5, 0.5))
                        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                        img.thumbnail((200, 200))
                        data = img.tobytes("raw", "RGB")
                        qim = QImage(data, img.width, img.height, img.width * 3, QImage.Format.Format_RGB888)
                        pixmap = QPixmap.fromImage(qim)
                except Exception: pass
                
            elif ext == '.cbz':
                try:
                    with zipfile.ZipFile(p, 'r') as z:
                        names = sorted([n for n in z.namelist() if n.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))])
                        if names:
                            with z.open(names[0]) as f:
                                img_data = f.read()
                                img = Image.open(io.BytesIO(img_data)).convert("RGB")
                                img.thumbnail((200, 200))
                                data = img.tobytes("raw", "RGB")
                                qim = QImage(data, img.width, img.height, img.width * 3, QImage.Format.Format_RGB888)
                                pixmap = QPixmap.fromImage(qim)
                except: pass
            else:
                try:
                    pixmap = QPixmap(p).scaled(160, 160, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                except:
                    pass
            
            vbox = QVBoxLayout()
            vbox.setContentsMargins(0, 0, 0, 0)
            vbox.setSpacing(5)
            vbox.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
            
            img_lbl = QLabel()
            img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            img_lbl.setFixedSize(160, 160)
            img_lbl.setStyleSheet("background-color: #2B2D31; border-radius: 8px;")
            if pixmap and not pixmap.isNull():
                img_lbl.setPixmap(pixmap)
            else:
                icon_text = "🎥 Битый файл" if ext in {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'} else "📄"
                img_lbl.setText(icon_text)
                img_lbl.setStyleSheet("font-size: 16px; font-weight: bold; background-color: #2B2D31; border-radius: 8px; color: #DA3633;")
                
            name_lbl = QLabel()
            name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            name_lbl.setStyleSheet("font-size: 11px; color: #DCDDDE;")
            name_lbl.setMinimumWidth(160)
            name_lbl.setMaximumWidth(160)
            
            fm = name_lbl.fontMetrics()
            name_lbl.setText(fm.elidedText(Path(p).name, Qt.TextElideMode.ElideMiddle, 150))

            vbox.addWidget(img_lbl)
            vbox.addWidget(name_lbl)
            
            cell = QWidget()
            cell.setLayout(vbox)
            
            grid.addWidget(cell, i // cols, i % cols, Qt.AlignmentFlag.AlignCenter)
            grid.setRowStretch(i // cols, 10)
            
        grid.setRowStretch(total_rows, 1)
            
        scroll.setWidget(container)
        layout.addWidget(scroll, stretch=1)
        
        btn_layout = QHBoxLayout()
        btn_safe = QPushButton("🗑️ В корзину (Safe)")
        btn_safe.setMinimumHeight(35)
        btn_safe.setObjectName("secondary")
        btn_safe.clicked.connect(self._safe_del)
        
        btn_hard = QPushButton("⚠️ Насовсем (Hard)")
        btn_hard.setMinimumHeight(35)
        btn_hard.setStyleSheet("QPushButton { background-color: #DA3633; color: white; border: none; font-weight: bold; } QPushButton:hover { background-color: #E24C49; }")
        btn_hard.clicked.connect(self._hard_del)
        
        btn_cancel = QPushButton("Отмена")
        btn_cancel.setMinimumHeight(35)
        btn_cancel.setObjectName("primary")
        btn_cancel.clicked.connect(self.reject)
        
        btn_layout.addWidget(btn_safe)
        btn_layout.addWidget(btn_hard)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_cancel)
        layout.addLayout(btn_layout)
        
    def _safe_del(self):
        self.delete_hard = False
        self.accept()
        
    def _hard_del(self):
        self.delete_hard = True
        self.accept()