# ============================================================
# MODULE: ui/components/video_player.py
# ============================================================
import os
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLabel, QComboBox, QSlider, QSizePolicy)
from PySide6.QtCore import Qt, QUrl, QTimer, QSize, QByteArray
from PySide6.QtGui import QColor, QIcon, QPixmap, QPainter
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink
# Импорт QtSvg регистрирует image-handler формата "SVG", без которого
# QPixmap.loadFromData(..., "SVG") вернул бы пустой пиксмап.
from PySide6 import QtSvg  # noqa: F401

from utils.theme_manager import ThemeManager

# Inline-SVG глифы плеера (Material-style path data, viewBox 0 0 24 24). Вектор
# вместо emoji/системного шрифта: не мылится, не зависит от шрифта ОС и красится
# под активную тему.
SVG_PLAY = "M8 5v14l11-7z"
SVG_PAUSE = "M6 19h4V5H6v14zm8-14v14h4V5h-4z"
SVG_VOLUME = "M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02z"


def create_svg_icon(svg_path_d: str, color: str) -> QIcon:
    """Собирает QIcon из inline-SVG: оборачивает path-data `d` в <svg>, красит
    заливку в `color`, растеризует через QPixmap.loadFromData(..., "SVG")."""
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        f'<path fill="{color}" d="{svg_path_d}"/></svg>'
    )
    pixmap = QPixmap()
    pixmap.loadFromData(QByteArray(svg.encode("utf-8")), "SVG")
    return QIcon(pixmap)

class JumpSlider(QSlider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            val = self.minimum() + ((self.maximum() - self.minimum()) * event.position().x()) / self.width()
            self.setValue(int(val))
            self.sliderMoved.emit(int(val))
            self.sliderReleased.emit() 
        super().mousePressEvent(event)

class VideoSinkWidget(QWidget):
    """Виджет-приёмник видеокадров: получает декодированные кадры из QVideoSink
    и рисует их вручную через QPainter — БЕЗ нативного NSView-оверлея, который
    создаёт QVideoWidget.

    Почему так: на macOS QVideoWidget заводит собственный нативный слой
    (NSView/WindowServer-композитинг). Этот слой конфликтовал с отрисовкой окна
    (чёрный леттербокс просвечивал поверх палитры, артефакты при смене Spaces) и
    держал живые нативные ffmpeg-потоки, дедлочившие процесс при закрытии.
    QVideoSink же отдаёт кадры обратно в Python: мы сами жёстко заливаем фон
    цветом темы и рисуем кадр — никаких нативных оверлеев, поведение одинаково
    на macOS, Windows и Linux.

    bg_role — ключ палитры ThemeManager для заливки фона: 'surface' для карточек
    превью, 'bg' для области сравнения.
    """

    def __init__(self, parent=None, bg_role: str = "surface"):
        super().__init__(parent)
        self._bg_role = bg_role
        self._current_frame = None
        # Статичный превью-кадр (QImage), вытащенный через cv2 (как в множественном
        # просмотре). Показывается СРАЗУ при выборе видео — без чёрного экрана и без
        # прогрева QMediaPlayer. Как только пойдут живые кадры из синка (нажат Play),
        # превью сбрасывается и его место занимает реальное воспроизведение.
        self._preview_image = None
        self._sink = QVideoSink(self)
        self._sink.videoFrameChanged.connect(self._on_frame)
        # Фон рисуем сами в paintEvent на весь rect(), поэтому системную заливку
        # отключаем и помечаем отрисовку непрозрачной (меньше лишних перерисовок).
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def videoSink(self) -> QVideoSink:
        """Sink для QMediaPlayer.setVideoSink(...)."""
        return self._sink

    def set_preview_image(self, image):
        """Показать статичный превью-кадр (cv2 QImage) до начала воспроизведения."""
        self._preview_image = image
        # Сбрасываем возможный устаревший живой кадр от прошлого видео.
        self._current_frame = None
        self.update()

    def clear_preview(self):
        self._preview_image = None
        self._current_frame = None
        self.update()

    def _on_frame(self, frame):
        # Кадр приходит из потока декодера; кэшируем и просим перерисовку.
        self._current_frame = frame
        # Первый же валидный живой кадр делает статичное превью неактуальным.
        if frame is not None and frame.isValid():
            self._preview_image = None
        self.update()

    def _bg_color(self) -> QColor:
        colors = ThemeManager.colors()
        return QColor(colors.get(self._bg_role, colors["surface"]))

    def _draw_image(self, painter, rect, image):
        """Вписывает QImage в rect с сохранением пропорций и центрированием (Retina-aware)."""
        if image is None or image.isNull():
            return
        dpr = self.devicePixelRatioF()
        scaled = image.scaled(
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

    def paintEvent(self, event):
        painter = QPainter(self)
        rect = self.rect()
        # 1) Жёсткая НЕПРОЗРАЧНАЯ заливка всего rect() цветом активной темы —
        #    исключает чёрный леттербокс под кадром в любой теме.
        painter.fillRect(rect, self._bg_color())

        # 2) Приоритет — живой кадр воспроизведения (QVideoFrame -> QImage); если
        #    его нет, рисуем статичный cv2-превью-кадр (как в множественном
        #    просмотре). Так при выборе видео сразу виден кадр, а не пустота.
        frame = self._current_frame
        if frame is not None and frame.isValid():
            self._draw_image(painter, rect, frame.toImage())
        elif self._preview_image is not None:
            self._draw_image(painter, rect, self._preview_image)


class BuiltInVideoPlayer(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        
        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.5)
        self.player.setAudioOutput(self.audio_output)
        self._pending_seek = False
        # Путь текущего видео — чтобы отбросить запоздавший превью-кадр от
        # предыдущего выбора (cv2-воркер асинхронный).
        self._current_path = None

        # Превью-кадр тащим тем же путём, что и множественный просмотр — через
        # cv2 (CompareVideoWorker), а не через прогрев QMediaPlayer. Это даёт
        # мгновенный реальный кадр без чёрного экрана; QMediaPlayer стартует
        # только когда пользователь жмёт Play.
        from ui.workers import CompareVideoWorker
        self._thumb_worker = CompareVideoWorker()
        self._thumb_worker.frame_ready.connect(self._on_thumb_ready)

        self.video_container = QWidget()
        self.video_container.setObjectName("video_bg")

        vc_layout = QVBoxLayout(self.video_container)
        vc_layout.setContentsMargins(8, 8, 8, 8)

        # Видео рисуется вручную из QVideoSink (см. VideoSinkWidget), без
        # нативного оверлея QVideoWidget. Фон карточки-превью — токен 'surface',
        # пропорции и леттербокс держит сам paintEvent виджета.
        self.video_widget = VideoSinkWidget(bg_role="surface")
        self._apply_video_background()
        vc_layout.addWidget(self.video_widget)
        self.player.setVideoSink(self.video_widget.videoSink())

        self._setup_ui()
        self.player.positionChanged.connect(self._on_position_changed)
        self.player.durationChanged.connect(self._on_duration_changed)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.video_container, stretch=1)
        
        controls_panel = QWidget()
        controls_panel.setObjectName("controls_panel") 
        controls_panel.setFixedHeight(45)
        cp_layout = QHBoxLayout(controls_panel)
        cp_layout.setContentsMargins(10, 0, 10, 0)
        cp_layout.setSpacing(10)
        
        # Иконки кнопок — inline-SVG (вектор), а не emoji/системный шрифт:
        # системные шрифты macOS ломали выравнивание, а эмодзи динамика мылилась.
        # SVG красится под активную тему и масштабируется без потери чёткости.
        # Сбрасываем фон и рамку, жёстко фиксируем размеры кнопки/иконки.
        btn_qss = "QPushButton#player_btn { background: transparent; border: none; }"

        self.btn_play = QPushButton()
        self.btn_play.setObjectName("player_btn")
        self.btn_play.setStyleSheet(btn_qss)
        self.btn_play.setFixedSize(44, 44)
        self.btn_play.setIconSize(QSize(28, 28))
        self.btn_play.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_play.clicked.connect(self._toggle_play)
        self._set_play_icon()
        cp_layout.addWidget(self.btn_play)

        self.btn_mute = QPushButton()
        self.btn_mute.setObjectName("player_btn")
        self.btn_mute.setStyleSheet(btn_qss)
        self.btn_mute.setFixedSize(44, 44)
        self.btn_mute.setIconSize(QSize(28, 28))
        self.btn_mute.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_mute.clicked.connect(self._toggle_mute)
        self._set_volume_icon()
        cp_layout.addWidget(self.btn_mute)

        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(50)
        self.volume_slider.setFixedWidth(70)
        self.volume_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.volume_slider.valueChanged.connect(self._change_volume)
        cp_layout.addWidget(self.volume_slider)
        
        self.slider = JumpSlider(Qt.Orientation.Horizontal)
        self.slider.sliderMoved.connect(self._on_slider_moved)
        self.slider.sliderReleased.connect(self._execute_seek)
        cp_layout.addWidget(self.slider)
        
        self.lbl_time = QLabel("00:00")
        self.lbl_time.setObjectName("player_time")
        cp_layout.addWidget(self.lbl_time)
        
        self.combo_speed = QComboBox()
        self.combo_speed.addItems(["0.5x", "1.0x", "1.5x", "2.0x"])
        self.combo_speed.setCurrentIndex(1)
        self.combo_speed.setFixedWidth(65)
        self.combo_speed.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.combo_speed.currentTextChanged.connect(self._change_speed)
        cp_layout.addWidget(self.combo_speed)
        
        layout.addWidget(controls_panel)

    def _apply_video_background(self):
        """Красит контейнер видео в цвет 'surface' активной темы.

        Сам кадр и его фон рисует VideoSinkWidget.paintEvent (заливка rect()
        цветом темы), поэтому здесь достаточно покрасить рамку-контейнер
        #video_bg и попросить виджет перерисоваться под новый цвет темы.
        """
        surface = ThemeManager.colors()["surface"]
        self.video_container.setStyleSheet(
            f"QWidget#video_bg {{ background-color: {surface}; border-radius: 8px; }}"
        )
        self.video_widget.update()

    def apply_theme(self):
        """Перекрашивает леттербокс и SVG-иконки под новую тему после живой смены."""
        self._apply_video_background()
        # Глифы пересобираем под новый цвет: Play/Pause — по текущему состоянию.
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._set_pause_icon()
        else:
            self._set_play_icon()
        self._set_volume_icon()

    def _icon_color(self) -> str:
        # Цвет глифа зависит от активной темы: почти-белый в Dark, почти-чёрный
        # в Light. _active — это сам словарь DARK/LIGHT, сравниваем по identity.
        return "#F5F5F7" if ThemeManager.colors() is ThemeManager.DARK else "#1D1D1F"

    def _set_play_icon(self):
        self.btn_play.setIcon(create_svg_icon(SVG_PLAY, self._icon_color()))

    def _set_pause_icon(self):
        self.btn_play.setIcon(create_svg_icon(SVG_PAUSE, self._icon_color()))

    def _set_volume_icon(self):
        self.btn_mute.setIcon(create_svg_icon(SVG_VOLUME, self._icon_color()))

    def load_video(self, path: str):
        self.stop()

        # Возврат из «Сравнения»: _trigger_grid_compare ЯВНО прячет и сам плеер,
        # и его сцену (video_player.hide() + video_widget.hide()), чтобы погасить
        # нативный медиа-контекст перед сменой Space. Явно скрытый дочерний виджет
        # НЕ восстанавливается автоматически, когда QStackedWidget снова показывает
        # страницу плеера, — поэтому видеообласть оставалась пустой (виден только
        # фон-леттербокс). Принудительно показываем оба при каждой загрузке видео.
        self.show()
        self.video_widget.show()

        if not os.path.exists(path):
            from utils.logger import auditor
            auditor.error(f"Video file missing: {path}")
            self._current_path = None
            self.video_widget.clear_preview()
            return

        abs_path = os.path.abspath(path)
        self._current_path = abs_path
        # ПРЕВЬЮ-КАДР: тащим через cv2 (CompareVideoWorker) ровно как в
        # множественном просмотре — мгновенно и без чёрного экрана. Результат
        # прилетит в _on_thumb_ready и нарисуется как статичный кадр.
        self._thumb_worker.request_frames([abs_path], 25)

        # ОТВЯЗКА ОТ UI-ПОТОКА: тяжёлая инициализация ffmpegmediaplugin внутри
        # QMediaPlayer::setSource блокирует главный поток. Откладываем установку
        # источника, чтобы цикл событий UI успел отрисоваться до загрузки кодеков.
        # Сам плеер НЕ запускаем — стартует только по кнопке Play (поверх превью).
        QTimer.singleShot(100, lambda: self._apply_source(abs_path))

    def _on_thumb_ready(self, path, qimg):
        # Отбрасываем запоздавший кадр от ранее выбранного видео.
        if path != self._current_path or qimg is None or qimg.isNull():
            return
        self.video_widget.set_preview_image(qimg)

    def _apply_source(self, abs_path: str):
        # Готовим источник для РЕАЛЬНОГО воспроизведения, но не запускаем его —
        # на экране уже виден статичный cv2-превью-кадр. На durationChanged
        # выставим диапазон слайдера и позицию ~25%, чтобы Play стартовал с того
        # же места, что показывает превью.
        self.player.setSource(QUrl.fromLocalFile(abs_path))
        self._pending_seek = True
        self._set_play_icon()

    def stop(self):
        self._pending_seek = False
        if self.player.playbackState() != QMediaPlayer.PlaybackState.StoppedState:
            self.player.stop()

    def pause(self):
        """Ставит воспроизведение на паузу, НЕ разрушая источник/декодер.
        Безопасная альтернатива release_source() при переходе в полноэкранное
        сравнение на macOS: уничтожение AVPlayerLayer в fullscreen фатально для
        WindowServer и вызывает сброс Space, тогда как спящий (paused) контекст
        скрыть безопасно."""
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()

    def release_source(self):
        """Полностью отдаёт аппаратный медиа-контекст (AVFoundation/ffmpeg):
        останавливает воспроизведение И обнуляет источник QMediaPlayer, чтобы
        нативный декодер освободил GPU/Space-контекст ДО того, как плеер скроют
        (переход на страницу сравнения). Живой графический контекст в скрытом
        видеовиджете на macOS способен заставить WindowServer сбросить fullscreen
        Space — поэтому ресурсы отдаём заранее. Превью гасим: оно вернётся при
        следующем выборе файла (PreviewController снова вызовет load_video)."""
        self.stop()
        self._pending_seek = False
        self._current_path = None
        self.player.setSource(QUrl())
        self.video_widget.clear_preview()

    def _toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self._set_play_icon()
        else:
            self.player.play()
            self._set_pause_icon()

    def _toggle_mute(self):
        is_muted = not self.audio_output.isMuted()
        self.audio_output.setMuted(is_muted)
        self._set_volume_icon()

    def _change_volume(self, value):
        self.audio_output.setVolume(value / 100.0)
        # Поднятие громкости с нуля автоматически снимает mute.
        if value > 0 and self.audio_output.isMuted():
            self.audio_output.setMuted(False)
        self._set_volume_icon()
            
    def _change_speed(self, text):
        speed = float(text.replace('x', ''))
        self.player.setPlaybackRate(speed)

    def _on_position_changed(self, position):
        if not self.slider.isSliderDown():
            self.slider.blockSignals(True)
            self.slider.setValue(position)
            self.slider.blockSignals(False)
        self._update_time_label(position)

    def _on_duration_changed(self, duration):
        self.slider.setRange(0, duration)
        if self._pending_seek and duration > 0:
            target = int(duration * 0.25)
            self.player.setPosition(target)
            self.slider.setValue(target)
            self._update_time_label(target)
            self._pending_seek = False

    def _on_slider_moved(self, position):
        self._update_time_label(position)

        # ВО ВРЕМЯ ВОСПРОИЗВЕДЕНИЯ перемотка идёт штатно через сам плеер.
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.setPosition(position)
            return

        # НА ПАУЗЕ — cv2-СКРАББИНГ. На macOS QMediaPlayer на паузе не
        # перерисовывает кадр по setPosition (ползунок «мёртвый»), поэтому кадр
        # нужного таймкода тащим через cv2 (тот же CompareVideoWorker, что и
        # превью) и рисуем его как статичный preview. Никакого setPosition при
        # скраббинге: реальную позицию плеера выставит _execute_seek по
        # отпусканию ползунка, чтобы Play стартовал ровно отсюда.
        duration = self.player.duration()
        if duration <= 0 or not self._current_path:
            return
        pct = max(0.0, min(100.0, position / duration * 100.0))
        self._thumb_worker.request_frames([self._current_path], pct)

    def _execute_seek(self):
        # По отпусканию ползунка синхронизируем РЕАЛЬНУЮ позицию плеера с тем
        # таймкодом, который показал cv2-скраб, чтобы последующий Play стартовал
        # ровно оттуда, куда домотал пользователь.
        self.player.setPosition(self.slider.value())

    def _update_time_label(self, ms_pos):
        pos = ms_pos // 1000
        self.lbl_time.setText(f"{pos//60:02d}:{pos%60:02d}")