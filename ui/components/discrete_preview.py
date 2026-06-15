# ============================================================
# MODULE: ui/components/discrete_preview.py
# ============================================================
"""DiscreteScrubbingEngine — дискретный контур панели предпросмотра.

Бифуркация конвейера предпросмотра: видео (непрерывное время t) обслуживает
StreamPlaybackEngine (BuiltInVideoPlayer, QMediaPlayer→QVideoSink), а МНОГО-
КАДРОВЫЕ/МНОГОСТРАНИЧНЫЕ статические форматы — GIF, PDF, CBZ — этот модуль.

Для них непрерывное декодирование физического смысла не имеет: состояние — это
дискретный целочисленный индекс n ∈ [0, N-1] (кадр GIF, страница PDF/CBZ).
Поэтому здесь НЕТ QMediaPlayer/QVideoSink и связанных с ними нативных потоков
рендеринга (а значит — нет и встречного GIL-дедлока при смене источника).
Слайдер работает прямым индексатором (setRange(0, N-1), tracking), а его
valueChanged(int) синхронно адресует нужный кадр/страницу и рисует растр на
QLabel-подобном виджете — мгновенно, без отложенных воркеров.
"""

import io
import zipfile
from pathlib import Path

from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QBoxLayout,
                               QFrame, QLabel, QSizePolicy)
from PySide6.QtGui import QImage, QColor, QPainter, QPalette
from PySide6.QtCore import Qt, QTimer, QElapsedTimer

from PIL import Image

from ui.components.video_player import JumpSlider, paint_corrupted_placeholder
from utils.theme_manager import ThemeManager
from utils.logger import auditor


# Расширения, которые обслуживает дискретный контур (НЕ потоковое видео).
DISCRETE_EXTS = {'.gif', '.pdf', '.cbz'}


def _pil_to_qimage(img) -> QImage:
    """PIL.Image → QImage (копия, владеющая своими байтами)."""
    if img.mode == "RGBA":
        data = img.tobytes("raw", "RGBA")
        return QImage(data, img.width, img.height,
                      QImage.Format.Format_RGBA8888).copy()
    if img.mode != "RGB":
        img = img.convert("RGB")
    data = img.tobytes("raw", "RGB")
    return QImage(data, img.width, img.height, img.width * 3,
                  QImage.Format.Format_RGB888).copy()


# ---- Поставщики кадров (Frame Providers) ----------------------------------
# Единый контракт: .count (int >= 1) и .frame(index) -> QImage. Источник
# (PIL/fitz/zip) держится открытым между обращениями, чтобы скраб по индексу
# был O(адресация), а не O(переоткрытие файла). close() освобождает хэндл.

class _GifProvider:
    """Кадры GIF c RAM-кэшем декодированных QImage (Zero-Lag Scrubbing).

    PIL.seek(i) для GIF — это НЕ прямая адресация: дельта-кадры заставляют PIL
    перематывать и накладывать всю цепочку с нуля, т.е. каждый тик слайдера
    стоил O(i) дискового декода и давал микрофризы UI. Поэтому кадр декодируется
    РОВНО ОДИН РАЗ и оседает в self._cache: list[QImage|None]; слот слайдера
    достаёт готовый растр по индексу за O(1) без обращения к диску.

    Прогрев кэша кооперативный: DiscreteScrubbingWidget зовёт prefetch_step()
    мелкими бюджетами из QTimer UI-потока (никаких фоновых потоков — нечего
    глушить при teardown, см. правила завершения). Кадры, запрошенные скрабом
    раньше префетча, кэшируются по требованию тем же _decode."""

    # Потолок превентивного прогрева: гигантские GIF (кадры × W × H × 4 байта)
    # целиком в RAM не тащим — для них остаётся кэш по требованию. 512 МБ
    # (снижено с 768): при парном сравнении кэш одновременно греют ДВА провайдера,
    # и общий потолок 2×768 МБ грозил OOM. 512 МБ оставляет буфер для плавного
    # скраббинга тяжёлых анимаций, но снимает риск падения.
    _PRECACHE_MAX_BYTES = 512 * 1024 * 1024

    def __init__(self, path):
        self._img = Image.open(path)
        self.count = max(1, getattr(self._img, "n_frames", 1))
        self._cache = [None] * self.count
        w, h = self._img.size
        self._precache_on = (self.count * w * h * 4) <= self._PRECACHE_MAX_BYTES
        self._next_prefetch = 0

    def frame(self, index) -> QImage:
        index = max(0, min(int(index), self.count - 1))
        cached = self._cache[index]
        if cached is not None:
            return cached
        return self._decode(index)

    def _decode(self, index) -> QImage:
        try:
            self._img.seek(index)
            qimg = _pil_to_qimage(self._img.convert("RGBA"))
        except Exception as e:
            auditor.warning(f"[GifProvider] frame {index} failed: {e}")
            return QImage()
        self._cache[index] = qimg
        return qimg

    def prefetch_step(self) -> bool:
        """Декодирует один ещё не кэшированный кадр. True — есть что греть дальше."""
        if not self._precache_on:
            return False
        while (self._next_prefetch < self.count
               and self._cache[self._next_prefetch] is not None):
            self._next_prefetch += 1
        if self._next_prefetch >= self.count:
            return False
        self._decode(self._next_prefetch)
        self._next_prefetch += 1
        return self._next_prefetch < self.count

    def close(self):
        self._cache = []
        try:
            self._img.close()
        except Exception:
            pass


class _PdfProvider:
    """Страницы PDF через PyMuPDF (fitz) — рендер по номеру страницы (High-DPI).

    Растеризация Retina-aware: PDF — это ВЕКТОР, чёткость готового растра задаётся
    зумом матрицы при get_pixmap, а не апскейлом QLabel постфактум. Базовый
    логический зум _BASE_ZOOM умножаем на devicePixelRatio экрана (его прокидывает
    виджет через render_scale): на 2x-дисплее плотный A4-текст рендерится в вдвое
    большее число ФИЗИЧЕСКИХ пикселей и остаётся кристально чётким после вписывания
    (downscale) в вьюпорт. _MAX_ZOOM — страховка от гигантских pixmap на 3x."""

    _BASE_ZOOM = 2.0
    _MAX_ZOOM = 4.0

    def __init__(self, path, render_scale: float = 1.0):
        import fitz
        self._doc = fitz.open(path)
        self.count = max(1, self._doc.page_count)
        self._fitz = fitz
        # zoom = базовый логический масштаб × DPR экрана, с потолком.
        self._zoom = min(self._BASE_ZOOM * max(1.0, render_scale), self._MAX_ZOOM)

    def frame(self, index) -> QImage:
        index = max(0, min(int(index), self.count - 1))
        try:
            page = self._doc.load_page(index)
            # Матрица трансформации масштабирует страницу ПРИ растеризации.
            matrix = self._fitz.Matrix(self._zoom, self._zoom)
            # alpha=False — без альфа-канала (исторический патч против SegFault
            # на некоторых PDF при прямом QImage из RGBA-самплов).
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            return QImage(pix.samples, pix.width, pix.height, pix.stride,
                          QImage.Format.Format_RGB888).copy()
        except Exception as e:
            auditor.warning(f"[PdfProvider] page {index} failed: {e}")
            return QImage()

    def close(self):
        try:
            self._doc.close()
        except Exception:
            pass


class _CbzProvider:
    """Страницы CBZ — извлечение записи архива по прямому индексу."""

    def __init__(self, path):
        self._zip = zipfile.ZipFile(path, 'r')
        self._names = sorted(
            n for n in self._zip.namelist()
            if n.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.bmp'))
        )
        self.count = max(1, len(self._names))

    def frame(self, index) -> QImage:
        if not self._names:
            return QImage()
        index = max(0, min(int(index), len(self._names) - 1))
        try:
            with self._zip.open(self._names[index]) as f:
                raw = f.read()
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            img.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            return _pil_to_qimage(img)
        except Exception as e:
            auditor.warning(f"[CbzProvider] page {index} failed: {e}")
            return QImage()

    def close(self):
        try:
            self._zip.close()
        except Exception:
            pass


class _StaticProvider:
    """Одиночный растр (фото) — count == 1, индекс игнорируется."""

    def __init__(self, path):
        self._img = QImage(path)
        self.count = 1

    def frame(self, index) -> QImage:
        return self._img

    def close(self):
        self._img = None


def make_provider(path, render_scale: float = 1.0):
    """Фабрика поставщика по расширению; падение декодера не валит UI.

    render_scale — devicePixelRatio экрана; используется только PDF-поставщиком
    для Retina-растеризации (остальным форматам зум не нужен)."""
    ext = Path(path).suffix.lower()
    try:
        if ext == '.gif':
            return _GifProvider(path)
        if ext == '.pdf':
            return _PdfProvider(path, render_scale=render_scale)
        if ext == '.cbz':
            return _CbzProvider(path)
        return _StaticProvider(path)
    except Exception as e:
        auditor.warning(f"[discrete_preview] provider init failed for {path}: {e}")
        return None


# ---- Растровый вьюпорт ------------------------------------------------------

class _RasterView(QWidget):
    """Лёгкий вывод QImage с леттербоксом (KeepAspectRatio, Retina-aware).

    Намеренно НЕ ScalableImageLabel: тому нужны асинхронные воркеры и QMovie,
    а дискретному скраббингу нужна синхронная мгновенная отрисовка готового
    QImage — поставщик уже отдал кадр в UI-поток."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image = None
        # Гард «битый файл»: провайдер вернул null QImage (сбой декода кадра/
        # страницы). Отличаем от штатной очистки (None) — заглушку рисуем только
        # при реальном сбое, см. set_image.
        self._broken = False
        surface = QColor(ThemeManager.colors()["surface"])
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, surface)
        self.setPalette(pal)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(50, 50)

    def set_image(self, image: QImage):
        if image is not None and not image.isNull():
            self._image = image
            self._broken = False
        else:
            # Null QImage от провайдера = сбой декода (битый кадр/страница);
            # None = явная очистка вьюпорта. Различаем, чтобы заглушку «битый
            # файл» рисовать только при реальном сбое, а не при штатном clear().
            self._image = None
            self._broken = image is not None and image.isNull()
        self.update()

    def clear(self):
        self._image = None
        self._broken = False
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, QColor(ThemeManager.colors()["surface"]))
        if self._broken:
            paint_corrupted_placeholder(painter, rect)
            return
        if self._image is None:
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        dpr = self.devicePixelRatioF()
        scaled = self._image.scaled(
            rect.size() * dpr,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled.setDevicePixelRatio(dpr)
        w = scaled.width() / dpr
        h = scaled.height() / dpr
        x = rect.x() + (rect.width() - w) / 2
        y = rect.y() + (rect.height() - h) / 2
        painter.drawImage(int(x), int(y), scaled)


# ---- Дискретный движок ------------------------------------------------------

class DiscreteScrubbingWidget(QWidget):
    """Страница предпросмотра для GIF/PDF/CBZ с мгновенным индексным скраббингом.

    Один источник: одиночный просмотр. Два источника (ровно два GIF/PDF/CBZ из
    дерева): пакетная сетка 1×2 с ЕДИНЫМ слайдером — обе копии листаются
    синхронно по одному индексу страницы/кадра. Звуковой панели нет вовсе
    (для статических форматов она лишена смысла)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("discretePreview")
        self._providers = []
        self._views = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Направление компоновки вьюх ПЕРЕКЛЮЧАЕМОЕ (см. _apply_orientation):
        # QHBoxLayout — это QBoxLayout, и setDirection() меняет ось на месте,
        # без пересоздания layout'а и репарентинга вьюх.
        self._view_container = QWidget()
        self._view_layout = QHBoxLayout(self._view_container)
        self._view_layout.setContentsMargins(8, 8, 8, 8)
        self._view_layout.setSpacing(8)
        root.addWidget(self._view_container, stretch=1)

        # Кооперативный прогрев RAM-кэша GIF: тик = малый бюджет декода в
        # UI-потоке (interval 0 → между событиями), без фоновых потоков.
        self._prefetch_timer = QTimer(self)
        self._prefetch_timer.setInterval(0)
        self._prefetch_timer.timeout.connect(self._prefetch_tick)

        # Панель индексатора: слайдер кадра/страницы + индикатор «n / N».
        self._panel = QFrame()
        self._panel.setObjectName("discrete_slider_panel")
        self._panel.setFixedHeight(45)
        p_layout = QHBoxLayout(self._panel)
        p_layout.setContentsMargins(15, 0, 12, 0)
        p_layout.setSpacing(10)

        # JumpSlider: клик по дорожке прыгает на индекс. Трекинг включён —
        # valueChanged летит на КАЖДЫЙ шаг под мышью (мгновенный фидбек).
        self._slider = JumpSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 0)
        self._slider.setTracking(True)
        self._slider.valueChanged.connect(self._on_index_changed)

        c = ThemeManager.colors()
        self._lbl_index = QLabel("0 / 0")
        self._lbl_index.setStyleSheet(f"color: {c['text']}; border: none;")
        self._lbl_index.setAlignment(Qt.AlignmentFlag.AlignCenter)

        p_layout.addWidget(self._slider, stretch=1)
        p_layout.addWidget(self._lbl_index)
        root.addWidget(self._panel)

    # ---- Загрузка / разбор --------------------------------------------------

    def load(self, paths):
        """Строит дискретную сессию под 1 или 2 пути (GIF/PDF/CBZ)."""
        self._teardown()
        if not paths:
            return
        paths = list(paths)[:2]   # пакетный режим — максимум 1×2

        # DPR экрана прокидываем в фабрику ОДИН раз на сессию: для PDF он задаёт
        # зум Retina-растеризации (см. _PdfProvider), прочим форматам безвреден.
        dpr = self._device_pixel_ratio()

        for path in paths:
            provider = make_provider(path, render_scale=dpr)
            if provider is None:
                continue
            view = _RasterView(self._view_container)
            self._view_layout.addWidget(view, stretch=1)
            self._providers.append(provider)
            self._views.append(view)

        if not self._providers:
            return

        # Диапазон слайдера = максимум по числу кадров/страниц набора. Источник
        # с меньшим N клампится в _render (последняя страница «застывает»).
        total = max(p.count for p in self._providers)
        self._slider.blockSignals(True)
        self._slider.setRange(0, max(0, total - 1))
        self._slider.setValue(0)
        self._slider.blockSignals(False)
        # Одностраничный/однокадровый набор: индексатор не нужен — прячем панель.
        self._panel.setVisible(total > 1)

        self._apply_orientation()
        self._render(0)

        # Превентивный прогрев кэша кадров (актуален только для GIF-поставщиков;
        # у остальных prefetch_step отсутствует и тик завершится мгновенно).
        if any(hasattr(p, "prefetch_step") for p in self._providers):
            self._prefetch_timer.start()

    # Триггерная модель ориентации: AR = W/H первого кадра первого источника.
    # AR < 1.0 (портрет) → вьюхи СЛЕВА НАПРАВО (LeftToRight): высокие кадры
    # делят ширину, используя всю высоту панели. AR >= 1.0 (ландшафт/widescreen)
    # → СВЕРХУ ВНИЗ (TopToBottom): широкие кадры получают полную ширину каждой
    # строки вместо урезанной половины.
    def _apply_orientation(self):
        first = self._providers[0].frame(0)  # для GIF оседает в RAM-кэше
        if first.isNull() or first.height() <= 0:
            return
        ar = first.width() / first.height()
        self._view_layout.setDirection(
            QBoxLayout.Direction.LeftToRight if ar < 1.0
            else QBoxLayout.Direction.TopToBottom
        )

    # Бюджет одного тика прогрева, мс: достаточно для пачки кадров, но не
    # настолько, чтобы между событиями ввода появился ощутимый фриз.
    _PREFETCH_BUDGET_MS = 12

    def _prefetch_tick(self):
        clock = QElapsedTimer()
        clock.start()
        while clock.elapsed() < self._PREFETCH_BUDGET_MS:
            pending = False
            for provider in self._providers:
                step = getattr(provider, "prefetch_step", None)
                if step is not None and step():
                    pending = True
            if not pending:
                self._prefetch_timer.stop()
                return

    def _device_pixel_ratio(self) -> float:
        """DPR текущего экрана; устойчив к ещё не показанному виджету (screen()
        может быть None до первого show → откат на primaryScreen, затем 1.0)."""
        scr = self.screen()
        if scr is not None:
            return scr.devicePixelRatio()
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None and app.primaryScreen() is not None:
            return app.primaryScreen().devicePixelRatio()
        return self.devicePixelRatioF() or 1.0

    def _format_index(self, shown: int, total: int) -> str:
        # Agnostic Telemetry: строго числовой формат «n / N» без текстовых
        # префиксов («Кадр»/«Стр.»/Frame/Page). Числа локале-независимы — индикатор
        # не рассинхронизируется со сменой языка и не плодит визуальный шум.
        return f"{shown} / {total}"

    def _on_index_changed(self, index):
        # Прямой синхронный слот индексатора: адресуем кадр и рисуем СРАЗУ.
        self._render(index)

    def _render(self, index):
        index = int(index)
        max_count = 0
        for provider, view in zip(self._providers, self._views):
            local = min(index, provider.count - 1)
            view.set_image(provider.frame(local))
            max_count = max(max_count, provider.count)
        if max_count:
            shown = min(index, max_count - 1) + 1
            self._lbl_index.setText(self._format_index(shown, max_count))

    # ---- Очистка ------------------------------------------------------------

    def _teardown(self):
        """Закрывает поставщиков (освобождает fitz/zip/PIL хэндлы) и рушит вьюхи."""
        self._prefetch_timer.stop()
        for provider in self._providers:
            try:
                provider.close()
            except Exception:
                pass
        self._providers = []
        while self._view_layout.count():
            item = self._view_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._views = []
        self._slider.blockSignals(True)
        self._slider.setRange(0, 0)
        self._slider.setValue(0)
        self._slider.blockSignals(False)
        self._lbl_index.setText("0 / 0")

    def clear(self):
        self._teardown()

    def hideEvent(self, event):
        # Уход со страницы стека — освобождаем файловые хэндлы декодеров сразу,
        # не дожидаясь следующего load(). Источники откроются заново при показе.
        self._teardown()
        super().hideEvent(event)
