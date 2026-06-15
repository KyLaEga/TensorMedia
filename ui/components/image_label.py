# ============================================================
# MODULE: ui/components/image_label.py
# ============================================================
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

class ScalableImageLabel(QLabel):
    def __init__(self):
        super().__init__()
        self.setMinimumSize(50, 50)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setStyleSheet("background-color: transparent; border: none;") 
        
        self._pixmap = None
        self._cached_scaled_pixmap = None
        self._last_size = QSize()
        
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

    def load_image(self, path: str):
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

    def paintEvent(self, event):
        from utils.i18n import translator
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.eraseRect(self.rect())

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