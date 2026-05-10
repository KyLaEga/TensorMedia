# ============================================================
# MODULE: ui/views/multi_compare.py
# ============================================================
import os
import cv2
import time
from collections import OrderedDict
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QGridLayout, QLabel, 
                               QPushButton, QScrollArea, QWidget, QCheckBox, QHBoxLayout, QFrame, QRadioButton, QSizePolicy)
from PySide6.QtGui import QPixmap, QImageReader, QImage, QPainter, QMovie
from PySide6.QtCore import Qt, QTimer, QThread, Signal

from ui.components.video_player import JumpSlider
from ui.workers import CompareVideoWorker 

class GridImageLabel(QLabel):
    def __init__(self):
        super().__init__()
        self.setMinimumSize(50, 50) 
        
        size_policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        size_policy.setHeightForWidth(True)
        self.setSizePolicy(size_policy)
        
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background-color: transparent; border: none;") 
        self._pixmap = None
        self._movie = None
        self._aspect_ratio = 0.5625 

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return int(width * self._aspect_ratio)

    def setPixmap(self, pixmap):
        self._clear_movie()
        self._pixmap = pixmap
        if pixmap and not pixmap.isNull() and pixmap.width() > 0:
            self._aspect_ratio = pixmap.height() / pixmap.width()
            self.updateGeometry() 
        self.update()

    def setMovie(self, movie):
        self._clear_movie()
        self._movie = movie
        super().setMovie(self._movie)
        self._movie.frameChanged.connect(self._on_frame_update)
        self._movie.start()

    def _on_frame_update(self):
        if self._movie and self._movie.currentPixmap():
            pm = self._movie.currentPixmap()
            if pm.width() > 0:
                new_ratio = pm.height() / pm.width()
                if abs(self._aspect_ratio - new_ratio) > 0.01:
                    self._aspect_ratio = new_ratio
                    self.updateGeometry()
        self.update()

    def clear_view(self):
        self._clear_movie()
        self._pixmap = None
        self.update()

    def _clear_movie(self):
        if self._movie:
            self._movie.stop()
            self._movie.setFileName("") 
            try:
                self._movie.frameChanged.disconnect(self._on_frame_update)
            except Exception:
                pass
            self._movie.deleteLater()
            self._movie = None

    def paintEvent(self, event):
        pm = None
        if self._movie: 
            pm = self._movie.currentPixmap()
        elif self._pixmap and not self._pixmap.isNull(): 
            pm = self._pixmap

        if pm and not pm.isNull():
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            
            scaled = pm.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            
            painter.drawPixmap(x, y, scaled)
        else: 
            super().paintEvent(event)


class MultiCompareDialog(QDialog):
    _orphaned_workers = []

    def __init__(self, file_paths, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Матрица синхронного сравнения")
        self.resize(1300, 850)
        self.file_paths = file_paths
        self.files_to_delete = []
        self.delete_hard = False
        
        self.video_exts = {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'}
        self.has_videos = any(os.path.splitext(p)[1].lower() in self.video_exts for p in file_paths)
        
        self.worker = CompareVideoWorker()
        self.worker.frame_ready.connect(self._on_frame_ready)
        
        self._setup_ui()
        self._init_decoders()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        
        count = len(self.file_paths)
        
        if count <= 2: cols = 1
        elif count <= 4: cols = 2
        else: cols = 3 
        
        self.grid_container = QWidget()
        self.grid = QGridLayout(self.grid_container)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(8) 
        
        self.cards = {}
        for idx, path in enumerate(self.file_paths):
            card = self._create_card(path)
            self.cards[path] = card
            
            row_index = (idx // cols) + 1
            col_index = idx % cols
            self.grid.addWidget(card, row_index, col_index)
            
        self.grid.setRowStretch(0, 1)
        self.grid.setRowStretch(self.grid.rowCount(), 1)
             
        if count <= 4:
            self.grid_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            layout.addWidget(self.grid_container, stretch=1)
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setStyleSheet("background-color: transparent; border: none;")
            scroll.setWidget(self.grid_container)
            layout.addWidget(scroll, stretch=1)
        
        if self.has_videos:
            slider_container = QFrame()
            slider_container.setFixedHeight(50)
            slider_container.setStyleSheet("background-color: #1E1F22; border-radius: 8px;")
            slider_layout = QHBoxLayout(slider_container)
            
            self.slider = JumpSlider(Qt.Orientation.Horizontal)
            self.slider.setRange(0, 100)
            self.slider.setValue(25)
            self.slider.sliderReleased.connect(self._execute_sync_video_frames)
            
            slider_layout.addWidget(QLabel("⏱️"))
            slider_layout.addWidget(self.slider)
            layout.addWidget(slider_container)
        
        delete_control = QFrame()
        delete_control.setStyleSheet("background-color: #2B2D31; border-radius: 8px; border: 1px solid #4E5058;")
        dc_layout = QHBoxLayout(delete_control)
        
        self.rb_trash = QRadioButton("🗑️ В корзину")
        self.rb_hard = QRadioButton("🔥 Насовсем (Безвозвратно)")
        self.rb_trash.setChecked(True)
        self.rb_hard.setStyleSheet("color: #DA3633; font-weight: bold;")
        
        dc_layout.addWidget(QLabel("<b>Режим удаления выбранных:</b>"))
        dc_layout.addWidget(self.rb_trash)
        dc_layout.addWidget(self.rb_hard)
        layout.addWidget(delete_control)

        btn_layout = QHBoxLayout()
        btn_cancel = QPushButton("Отмена")
        btn_cancel.setMinimumHeight(40)
        btn_cancel.clicked.connect(self.reject)
        
        btn_confirm = QPushButton("Применить выбор и закрыть")
        btn_confirm.setObjectName("primary")
        btn_confirm.setMinimumHeight(40)
        btn_confirm.clicked.connect(self._confirm)
        
        btn_layout.addStretch()
        btn_layout.addWidget(btn_cancel)
        btn_layout.addWidget(btn_confirm)
        layout.addLayout(btn_layout)

    def _create_card(self, path):
        frame = QFrame()
        frame.setStyleSheet("QFrame { background-color: #2B2D31; padding: 2px; border-radius: 6px; }")
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        l = QVBoxLayout(frame)
        l.setContentsMargins(0, 0, 0, 0)
        
        lbl_img = GridImageLabel()
        lbl_img.setStyleSheet("background-color: #000; border-radius: 4px;")
        l.addWidget(lbl_img, stretch=1)
        
        bottom_panel = QWidget()
        bp_layout = QHBoxLayout(bottom_panel)
        bp_layout.setContentsMargins(10, 5, 10, 5)
        
        info = QLabel(f"{os.path.basename(path)}")
        info.setStyleSheet("font-size: 13px; color: #DBDEE1; font-weight: bold;")
        cb = QCheckBox("Удалить")
        cb.setStyleSheet("font-size: 13px; font-weight: bold; color: #DA3633;")
        
        bp_layout.addWidget(info, stretch=1)
        bp_layout.addWidget(cb)
        l.addWidget(bottom_panel)
        
        frame.lbl_img = lbl_img
        frame.checkbox = cb
        frame.path = path
        return frame

    def _init_decoders(self):
        v_paths = [p for p in self.file_paths if os.path.splitext(p)[1].lower() in self.video_exts]
        for p in self.file_paths:
            ext = os.path.splitext(p)[1].lower()
            if ext == '.gif':
                self.cards[p].lbl_img.setMovie(QMovie(p))
            elif ext not in self.video_exts:
                reader = QImageReader(p)
                img = reader.read()
                if not img.isNull():
                    self.cards[p].lbl_img.setPixmap(QPixmap.fromImage(img))
        if v_paths:
            self.worker.request_frames(v_paths, 25)

    def _execute_sync_video_frames(self):
        v_paths = [p for p in self.file_paths if os.path.splitext(p)[1].lower() in self.video_exts]
        self.worker.request_frames(v_paths, self.slider.value())

    def _on_frame_ready(self, path, qimg):
        if path in self.cards:
            self.cards[path].lbl_img.setPixmap(QPixmap.fromImage(qimg))

    def _cleanup(self):
        for card in self.cards.values():
            if hasattr(card.lbl_img, 'clear_view'):
                card.lbl_img.clear_view()

    def _detach_worker(self):
        if self.worker.isRunning():
            self.worker.setParent(None)
            MultiCompareDialog._orphaned_workers.append(self.worker)
            self.worker.finished.connect(lambda w=self.worker: MultiCompareDialog._orphaned_workers.remove(w) if w in MultiCompareDialog._orphaned_workers else None)
            self.worker.finished.connect(self.worker.deleteLater)
            self.worker.is_running = False
            self.worker.quit()
        else:
            self.worker.deleteLater()

    def reject(self):
        self._detach_worker()
        self._cleanup()
        super().reject()

    def _confirm(self):
        self._detach_worker()
        self._cleanup()
        self.files_to_delete = [c.path for c in self.cards.values() if c.checkbox.isChecked()]
        self.delete_hard = self.rb_hard.isChecked()
        self.accept()

    def closeEvent(self, event):
        self._detach_worker()
        self._cleanup()
        super().closeEvent(event)