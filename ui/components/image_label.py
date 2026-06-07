# ============================================================
# MODULE: ui/components/image_label.py
# ============================================================
import io
import zipfile
from pathlib import Path
from PIL import Image

from PySide6.QtWidgets import QLabel, QSizePolicy
from PySide6.QtCore import Qt, QSize, QThreadPool, QRunnable, QObject, Signal
from PySide6.QtGui import QPixmap, QImage, QPainter

class CancellationToken:
    def __init__(self):
        self.cancelled = False

class WorkerSignals(QObject):
    finished = Signal(str, QImage)
    error = Signal(str)

class ImageLoaderWorker(QRunnable):
    def __init__(self, path: str, token: CancellationToken):
        super().__init__()
        self.path = path
        self.token = token
        self.signals = WorkerSignals()
        self.setAutoDelete(True)

    def run(self):
        if self.token.cancelled: return
        
        from PySide6.QtGui import QImageReader
        qim = QImage()
        try:
            reader = QImageReader(self.path)
            reader.setAutoTransform(True)
            
            # Ограничиваем размер при чтении для экономии RAM и ускорения отрисовки
            # 1280x720 достаточно для большинства превью
            size = reader.size()
            if size.isValid():
                if size.width() > 1280 or size.height() > 1280:
                    size.scale(1280, 1280, Qt.AspectRatioMode.KeepAspectRatio)
                    reader.setScaledSize(size)
            
            if self.token.cancelled: return
            qim = reader.read()
            
            if not self.token.cancelled:
                self.signals.finished.emit(self.path, qim)
        except Exception as e:
            from utils.logger import auditor
            auditor.error(f"[ImageLoader] Decoding error {self.path}: {e}")
            if not self.token.cancelled:
                self.signals.error.emit(self.path)

class DocLoaderWorker(QRunnable):
    def __init__(self, path: str, token: CancellationToken):
        super().__init__()
        self.path = path
        self.token = token
        self.signals = WorkerSignals()
        self.setAutoDelete(True)

    def run(self):
        if self.token.cancelled: return
        
        ext = Path(self.path).suffix.lower()
        qim = QImage()
        try:
            if ext == '.pdf':
                import fitz
                with fitz.open(self.path) as doc:
                    if self.token.cancelled: return
                    if len(doc) > 0:
                        page = doc.load_page(0)
                        pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
                        if self.token.cancelled: return
                        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                        data = img.tobytes("raw", "RGB")
                        qim = QImage(data, img.width, img.height, img.width * 3, QImage.Format.Format_RGB888).copy()
                        
            elif ext == '.cbz':
                with zipfile.ZipFile(self.path, 'r') as z:
                    names = sorted([n for n in z.namelist() if n.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))])
                    if names:
                        with z.open(names[0]) as f:
                            if self.token.cancelled: return
                            img_data = f.read()
                            img = Image.open(io.BytesIO(img_data)).convert("RGB")
                            img.thumbnail((800, 800), Image.Resampling.LANCZOS)
                            data = img.tobytes("raw", "RGB")
                            qim = QImage(data, img.width, img.height, img.width * 3, QImage.Format.Format_RGB888).copy()
            
            if not self.token.cancelled:
                self.signals.finished.emit(self.path, qim)
        except Exception as e:
            from utils.logger import auditor
            auditor.error(f"[DocLoader] Decoding error {self.path}: {e}")
            if not self.token.cancelled:
                self.signals.error.emit(self.path)

class ScalableImageLabel(QLabel):
    def __init__(self):
        super().__init__()
        self.setMinimumSize(50, 50)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setStyleSheet("background-color: transparent; border: none;") 
        
        self._pixmap = None
        self._cached_scaled_pixmap = None
        self._last_size = QSize()
        
        self._movie = None
        self.is_loading = False 
        self.is_error = False
        self.is_empty = True
        
        self._current_load_path = ""
        self._current_token = None
        self._thread_pool = QThreadPool.globalInstance()

    def resizeEvent(self, event):
        self._cached_scaled_pixmap = None
        super().resizeEvent(event)

    def clear_view(self):
        self._current_load_path = ""
        self._cancel_pending_workers()
        self._clear_movie()
        self.is_loading = False
        self.is_error = False
        self.is_empty = True
        self._pixmap = None
        self._cached_scaled_pixmap = None
        self.update()

    def _cancel_pending_workers(self):
        if self._current_token is not None:
            self._current_token.cancelled = True
            self._current_token = None

    def setPixmap(self, pixmap):
        self._current_load_path = ""
        self._cancel_pending_workers()
        self._clear_movie()
        self.is_loading = False
        
        if pixmap is None or pixmap.isNull():
            self.is_empty = True
            self.is_error = False
            self._pixmap = None
        else:
            self.is_empty = False
            self.is_error = False
            self._pixmap = pixmap
            
        self._cached_scaled_pixmap = None
        self.update()

    def setMovie(self, movie):
        self._current_load_path = ""
        self._cancel_pending_workers()
        self._clear_movie()
        self._movie = movie
        self._movie.setParent(self) 
        
        self.is_loading = False
        self.is_error = False
        self.is_empty = False
        self._cached_scaled_pixmap = None
        
        self._movie.frameChanged.connect(self.update)
        self._movie.start()
        self.update()

    def load_image(self, path: str):
        self._clear_movie()
        self._cancel_pending_workers()
        
        self.is_loading = True
        self.is_error = False
        self.is_empty = False
        self._cached_scaled_pixmap = None
        self._pixmap = None
        self._current_load_path = path
        self.update()
        
        self._current_token = CancellationToken()
        worker = ImageLoaderWorker(path, self._current_token)
        worker.signals.finished.connect(self._on_document_loaded) # Используем тот же колбэк
        worker.signals.error.connect(self._on_document_error)
        self._thread_pool.start(worker)

    def load_document(self, path: str):
        self._clear_movie()
        self._cancel_pending_workers()
        
        self.is_loading = True
        self.is_error = False
        self.is_empty = False
        self._cached_scaled_pixmap = None
        self._pixmap = None
        self._current_load_path = path
        self.update()
        
        self._current_token = CancellationToken()
        worker = DocLoaderWorker(path, self._current_token)
        worker.signals.finished.connect(self._on_document_loaded)
        worker.signals.error.connect(self._on_document_error)
        self._thread_pool.start(worker)

    def _on_document_loaded(self, path: str, qimage: QImage):
        if path != self._current_load_path:
            return
            
        self.is_loading = False
        if not qimage.isNull():
            self._pixmap = QPixmap.fromImage(qimage)
            self.is_error = False
        else:
            self._pixmap = None
            self.is_error = True
            
        self._cached_scaled_pixmap = None
        self.update()

    def _on_document_error(self, path: str):
        if path != self._current_load_path:
            return
        self.is_loading = False
        self.is_error = True
        self._pixmap = None
        self.update()

    def _clear_movie(self):
        if self._movie:
            self._movie.stop()
            self._movie.setFileName("")
            try:
                self._movie.frameChanged.disconnect(self.update)
            except (RuntimeError, TypeError):
                # Signal may not have been connected — benign, no log needed.
                pass
            self._movie.deleteLater()
            self._movie = None

    def _draw_centered(self, painter, pm):
        # Масштабируем строго по доступному rect() с сохранением пропорций
        # (KeepAspectRatio гарантирует отсутствие обрезки), отрисовываем по центру.
        # Учитываем devicePixelRatio: на Retina цель в физических пикселях, иначе
        # изображение либо мылит, либо выходит за границы виджета в полноэкранном
        # режиме.
        rect = self.rect()
        dpr = self.devicePixelRatioF()
        target = rect.size() * dpr
        scaled = pm.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled.setDevicePixelRatio(dpr)
        # Логические размеры (с учётом dpr) — никогда не превышают rect().
        w = scaled.width() / dpr
        h = scaled.height() / dpr
        x = rect.x() + (rect.width() - w) / 2
        y = rect.y() + (rect.height() - h) / 2
        painter.drawPixmap(int(x), int(y), scaled)

    def paintEvent(self, event):
        from utils.i18n import translator
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.eraseRect(self.rect())

        if self._movie:
            pm = self._movie.currentPixmap()
            if not pm.isNull():
                self._draw_centered(painter, pm)
                return

        pm = self._pixmap
        if pm and not pm.isNull():
            # Кэшируем масштабированное изображение, пересоздаём при смене размера.
            if self._cached_scaled_pixmap is None or self.size() != self._last_size:
                dpr = self.devicePixelRatioF()
                scaled = pm.scaled(
                    self.rect().size() * dpr,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                scaled.setDevicePixelRatio(dpr)
                self._cached_scaled_pixmap = scaled
                self._last_size = self.size()

            scaled = self._cached_scaled_pixmap
            dpr = scaled.devicePixelRatio() or 1.0
            w = scaled.width() / dpr
            h = scaled.height() / dpr
            rect = self.rect()
            x = rect.x() + (rect.width() - w) / 2
            y = rect.y() + (rect.height() - h) / 2
            painter.drawPixmap(int(x), int(y), scaled)
        else:
            painter.setPen(Qt.GlobalColor.gray)
            # Шрифт холста не переопределяем — наследуется глобальный app-шрифт.

            if self.is_loading:
                text = translator.tr("img_loading")
            elif self.is_error:
                text = translator.tr("img_error")
                painter.setPen(Qt.GlobalColor.red)
            elif self.is_empty:
                text = translator.tr("img_empty")
            else:
                text = translator.tr("img_doc")
                
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                text,
            )