import io
import zipfile
from pathlib import Path
from PIL import Image
from PyQt6.QtWidgets import QLabel, QSizePolicy
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap, QImage, QPainter

class ScalableImageLabel(QLabel):
    def __init__(self):
        super().__init__()
        self.setMinimumSize(50, 50)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setStyleSheet("background-color: transparent; border: none;") 
        self._pixmap = None
        self._movie = None
        self.is_loading = True 
        self.is_error = False

    def setPixmap(self, pixmap):
        self._clear_movie()
        self.is_loading = False
        if pixmap is None or pixmap.isNull():
            self.is_error = True
            self._pixmap = None
        else:
            self.is_error = False
            self._pixmap = pixmap
        self.update()

    def setMovie(self, movie):
        self._clear_movie()
        self._movie = movie
        self._movie.setParent(self) 
        self.is_loading = False
        self.is_error = False
        self._movie.frameChanged.connect(self._on_frame_update)
        self._movie.start()
        self.update()

    def load_document(self, path):
        self._clear_movie()
        self.is_loading = False
        self.is_error = False
        ext = Path(path).suffix.lower()
        pixmap = None

        if ext == '.pdf':
            try:
                import fitz
                doc = fitz.open(path)
                if len(doc) > 0:
                    page = doc.load_page(0)
                    pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    data = img.tobytes("raw", "RGB")
                    qim = QImage(data, img.width, img.height, img.width * 3, QImage.Format.Format_RGB888)
                    pixmap = QPixmap.fromImage(qim)
            except Exception:
                pass
        elif ext == '.cbz':
            try:
                with zipfile.ZipFile(path, 'r') as z:
                    names = sorted([n for n in z.namelist() if n.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))])
                    if names:
                        with z.open(names[0]) as f:
                            img_data = f.read()
                            img = Image.open(io.BytesIO(img_data)).convert("RGB")
                            img.thumbnail((800, 800))
                            data = img.tobytes("raw", "RGB")
                            qim = QImage(data, img.width, img.height, img.width * 3, QImage.Format.Format_RGB888)
                            pixmap = QPixmap.fromImage(qim)
            except Exception:
                pass

        if pixmap and not pixmap.isNull():
            self._pixmap = pixmap
        else:
            self._pixmap = None
        self.update()

    def _on_frame_update(self):
        self.update()

    def _clear_movie(self):
        if self._movie:
            self._movie.stop()
            try:
                self._movie.frameChanged.disconnect(self._on_frame_update)
            except Exception:
                pass
            self._movie = None

    def paintEvent(self, event):
        from utils.i18n import translator
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.eraseRect(self.rect())

        pm = None
        if self._movie: 
            pm = self._movie.currentPixmap()
        elif self._pixmap and not self._pixmap.isNull(): 
            pm = self._pixmap

        if pm and not pm.isNull():
            scaled = pm.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        else:
            painter.setPen(Qt.GlobalColor.gray)
            font = painter.font()
            font.setPointSize(14)
            painter.setFont(font)
            
            if self.is_loading:
                text = translator.tr("img_loading")
            elif self.is_error:
                text = translator.tr("img_error")
                painter.setPen(Qt.GlobalColor.red)
            else:
                text = translator.tr("img_doc")
                
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, text)