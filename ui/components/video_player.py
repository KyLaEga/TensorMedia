# ============================================================
# MODULE: ui/components/video_player.py
# ============================================================
import os
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLabel, QComboBox, QSlider, QSizePolicy)
from PySide6.QtCore import Qt, QUrl, QTimer, QSize, QElapsedTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink

# Регистрацию SVG image-handler'а (QtSvg) и сборку иконок выполняет
# ThemeManager.make_icon — см. utils/theme_manager.py.
from utils.theme_manager import ThemeManager

# Все векторные глифы плеера (play/pause/volume/volume_muted) живут в едином
# реестре дизайн-системы — ThemeManager.ICON_GLYPHS — и собираются в QIcon
# через ThemeManager.make_icon(name, color). Локальных SVG-литералов здесь нет.


def paint_corrupted_placeholder(painter, rect):
    """Рисует центрированную заглушку «⚠️ Битый файл / Corrupted» поверх уже
    залитого фоном холста.

    Закрывает UX-зазор «слепого квадрата»: когда декодер не отдал валидный растр
    (битый кадр/страница/видео), контур предпросмотра иначе оставался пустой
    заливкой темы, и пользователь не отличал сбой от пустоты. Единый источник
    отрисовки для всех контуров — _RasterView (discrete_preview) импортирует эту
    же функцию, поэтому вид заглушки одинаков для видео и дискретных форматов."""
    painter.save()
    painter.setPen(QColor("#DA3633"))
    font = painter.font()
    # Кегль масштабируем по высоте холста (мелкая карточка сравнения ↔ полноэкранный
    # одиночный просмотр), но в разумных рамках, чтобы текст не вылезал за края.
    font.setPointSize(max(11, min(20, rect.height() // 18)))
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "⚠️  Битый файл / Corrupted")
    painter.restore()


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
        # Гард «битый файл»: ставится mark_corrupted() при known-сбое (отсутствует/
        # не читается файл) и гасится любым валидным кадром/превью. paintEvent
        # рисует заглушку только по этому флагу, а НЕ на каждом «нет кадра» —
        # иначе плейсхолдер мигал бы во время штатной загрузки (cv2-превью ещё в
        # пути), что было бы ложной тревогой.
        self._broken = False
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
        self._broken = False   # пришёл валидный растр — снимаем гард «битый файл»
        self.update()

    def mark_corrupted(self):
        """Пометить контур как «битый файл»: гасит кадр/превью и просит перерисовку,
        чтобы paintEvent нарисовал заглушку «⚠️ Битый файл / Corrupted» вместо
        слепого квадрата заливки. Зовётся на known-сбое декодирования (например,
        отсутствующий/нечитаемый файл в load_video)."""
        self._preview_image = None
        self._current_frame = None
        self._broken = True
        self.update()

    def _on_frame(self, frame):
        # Кадр приходит из потока декодера; кэшируем и просим перерисовку.
        self._current_frame = frame
        # Первый же валидный живой кадр делает статичное превью неактуальным.
        if frame is not None and frame.isValid():
            self._preview_image = None
            self._broken = False
        self.update()

    def _bg_color(self) -> QColor:
        colors = ThemeManager.colors()
        return QColor(colors.get(self._bg_role, colors["surface"]))

    def _draw_image(self, painter, rect, image, smooth=True):
        """Вписывает QImage в rect с сохранением пропорций и центрированием (Retina-aware).

        smooth=False (живые кадры воспроизведения) → FastTransformation: для 4К
        SmoothTransformation масштабировал 8.3 млн пикселей на КАЖДОМ кадре в
        UI-потоке (видимые лаги/«дёрганье» при 4К). Билинейный фаст-даунскейл до
        размера превью визуально неотличим в движении, но кратно дешевле. Статичный
        cv2-превью (smooth=True) оставляем сглаженным — он рисуется один раз."""
        if image is None or image.isNull():
            return
        dpr = self.devicePixelRatioF()
        mode = (Qt.TransformationMode.SmoothTransformation if smooth
                else Qt.TransformationMode.FastTransformation)
        scaled = image.scaled(
            rect.size() * dpr,
            Qt.AspectRatioMode.KeepAspectRatio,
            mode,
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
            # Живой кадр — быстрый даунскейл (см. _draw_image): критично для 4К.
            self._draw_image(painter, rect, frame.toImage(), smooth=False)
        elif self._preview_image is not None:
            self._draw_image(painter, rect, self._preview_image, smooth=True)
        elif self._broken:
            # 3) Нет ни живого кадра, ни превью, и контур помечен битым — рисуем
            #    заглушку вместо слепого квадрата заливки (Corrupted File UX).
            paint_corrupted_placeholder(painter, rect)


class BuiltInVideoPlayer(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        
        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.5)
        self.player.setAudioOutput(self.audio_output)
        self._pending_seek = False
        # Optimistic-UI playback intent: the icon и наши решения следуют ЭТОМУ
        # локальному флагу, а НЕ блокирующему чтению QMediaPlayer.playbackState()
        # — оно сидит за тем же мьютексом libffmpegmediaplugin, что заморозил
        # GUI-поток в stackshot. Источник правды для кнопки Play/Pause.
        self._is_playing_intent = False
        # Drop-to-Seek scrub state.
        #   _scrubbing         — ползунок сейчас тянут (между sliderMoved и Released);
        #   _resume_after_seek — вернуть воспроизведение, когда seek закоммитится;
        #   _seek_in_flight    — затвор конвейера: тяжёлый seek коммитится, нельзя
        #                        стопкой слать play()/pause() на парализованный декодер;
        #   _intent_dirty      — пользователь успел дёрнуть Play/Pause во время затвора;
        #                        отложенный intent применим по снятию блокировки.
        self._scrubbing = False
        self._resume_after_seek = False
        self._seek_in_flight = False
        self._intent_dirty = False
        # Затвор дросселирования живого скраба — паритет с панелью сравнения
        # (multi_compare._SEEK_MIN_INTERVAL). QElapsedTimer вместо time.monotonic:
        # монотонные мс без аллокаций, стартует один раз и переиспользуется.
        # 33 мс ≈ 30 Гц — выше частоты обновления декодера; чаще слать setPosition/
        # cv2-запросы бессмысленно (переполняем нативную очередь seek'ов → фриз).
        self._scrub_clock = QElapsedTimer()
        self._scrub_clock.start()
        self._last_scrub_ms = 0
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
        self.slider.sliderReleased.connect(self._on_slider_released)
        cp_layout.addWidget(self.slider)
        
        self.lbl_time = QLabel("00:00 / 00:00")
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
        # Глиф Play/Pause пересобираем по ЛОКАЛЬНОМУ intent, а не по
        # playbackState() — чтобы перекраска темы не блокировалась на мьютексе
        # медиаплагина (тот же конечный автомат, что завис в stackshot).
        self._sync_play_icon()
        self._set_volume_icon()

    def _icon_color(self) -> str:
        # Цвет глифа зависит от активной темы: почти-белый в Dark, почти-чёрный
        # в Light. _active — это сам словарь DARK/LIGHT, сравниваем по identity.
        return "#F5F5F7" if ThemeManager.colors() is ThemeManager.DARK else "#1D1D1F"

    def _set_play_icon(self):
        self.btn_play.setIcon(ThemeManager.make_icon("play", self._icon_color()))

    def _set_pause_icon(self):
        self.btn_play.setIcon(ThemeManager.make_icon("pause", self._icon_color()))

    def _set_volume_icon(self):
        # Глиф отражает СОСТОЯНИЕ аудиовыхода: перечёркнутый динамик в mute,
        # обычный — при активном звуке. Также учитываем нулевую громкость.
        muted = self.audio_output.isMuted() or self.audio_output.volume() <= 0.0
        glyph = "volume_muted" if muted else "volume"
        self.btn_mute.setIcon(ThemeManager.make_icon(glyph, self._icon_color()))
        is_ru = True
        try:
            from utils.i18n import translator
            is_ru = getattr(translator, "current_lang", "ru") == "ru"
        except Exception:
            pass
        if muted:
            self.btn_mute.setToolTip("Включить звук" if is_ru else "Unmute")
        else:
            self.btn_mute.setToolTip("Выключить звук" if is_ru else "Mute")

    def load_video(self, path: str):
        # БЫЛО self.stop() — синхронный нативный teardown живого источника, тот
        # самый сайт встречного GIL-дедлока ~QAudioOutputWrapper из стекшота.
        # Теперь только сброс флагов; реальный снос старого контекста идёт ниже
        # через change_source_safe (отложенная C++ деструкция вне GIL-кванта).
        self._reset_transport_flags()

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
            # Файл отсутствует/нечитаем — это known-сбой: рисуем заглушку «битый
            # файл» вместо пустого квадрата (раньше тут был молчаливый clear).
            self.video_widget.mark_corrupted()
            return

        abs_path = os.path.abspath(path)
        self._current_path = abs_path

        # 1) МГНОВЕННЫЙ кадр из дискового кэша СИНХРОННО (чтение ~15-30 КБ JPEG —
        #    миллисекунды). Превью появляется СРАЗУ, не дожидаясь ни воркера, ни
        #    тяжёлого пересоздания плеера (шаг 3). Это и есть лечение «долго грузится»:
        #    на уже просканированной библиотеке миниатюры есть у всех файлов.
        shown = self._show_cached_thumb(abs_path)
        # 2) Кэш-промах (файл не сканировали) → добываем кадр воркером, он же
        #    сохранит миниатюру на будущее (cache=True).
        if not shown:
            self._thumb_worker.request_frames([abs_path], 25, cache=True)

        # 3) Пересоздание QMediaPlayer + setSource ДОРОГО и идёт СИНХРОННО на
        #    GUI-потоке (на macOS — заметная пауза на КАЖДЫЙ выбор: это и был тормоз).
        #    Откладываем в event-loop: кадр (шаг 1/2) рисуется первым, плеер
        #    готовится сразу после, не блокируя отрисовку. Старый контекст уходит в
        #    deleteLater (C++ деструкция вне GIL-кванта → нет дедлока ~QAudioOutputWrapper).
        QTimer.singleShot(0, lambda ap=abs_path: self._prepare_player_source(ap))

    def _show_cached_thumb(self, abs_path: str) -> bool:
        """Синхронно показать кадр из дискового кэша миниатюр, если он есть.
        Возвращает True, если кадр показан. Чтение маленького JPEG — миллисекунды,
        безопасно в GUI-потоке (в отличие от открытия видеоконтейнера)."""
        try:
            from utils.thumb_cache import thumb_path_for
            tp = thumb_path_for(abs_path)
            if tp is None or not tp.exists():
                return False
            from PySide6.QtGui import QImageReader
            img = QImageReader(str(tp)).read()
            if img.isNull():
                return False
            self.video_widget.set_preview_image(img)
            return True
        except Exception:
            return False

    def _prepare_player_source(self, abs_path: str):
        """Отложенная (вне горячего пути выбора) подготовка источника плеера.
        Guard: пользователь мог выбрать другое видео, пока ждали тик event-loop."""
        if abs_path != self._current_path:
            return
        self.change_source_safe(QUrl())
        self._apply_source(abs_path)

    def _on_thumb_ready(self, path, qimg):
        # Отбрасываем запоздавший кадр от ранее выбранного видео.
        if path != self._current_path or qimg is None or qimg.isNull():
            return
        self.video_widget.set_preview_image(qimg)

    def _apply_source(self, abs_path: str):
        # self.player — ЧИСТЫЙ пустой инстанс (load_video пересоздал контекст через
        # change_source_safe), поэтому setSource НЕ сносит живой рендер-граф
        # in-place и не встаёт в QThread::wait под GIL. Источник готовим, но не
        # запускаем — на экране уже cv2-превью; позицию ~25% выставит durationChanged.
        self.player.setSource(QUrl.fromLocalFile(abs_path))
        self._pending_seek = True
        # Свежий источник стартует на паузе (Play стартует только по кнопке).
        self._is_playing_intent = False
        self._sync_play_icon()

    def _reset_transport_flags(self):
        """Сброс конвейера скраба/intent. Без него следующий load унаследовал бы
        «битый» затвор (_seek_in_flight) или ложный «играю» (_is_playing_intent).
        Решение всегда по ЛОКАЛЬНОМУ intent, а не по блокирующему
        player.playbackState() (тот сидит за тем же мьютексом медиаплагина)."""
        self._pending_seek = False
        self._is_playing_intent = False
        self._scrubbing = False
        self._resume_after_seek = False
        self._seek_in_flight = False
        self._intent_dirty = False

    def change_source_safe(self, url: QUrl):
        """Отложенное ПЕРЕСОЗДАНИЕ контекста вместо in-place setSource/stop на
        ЖИВОМ инстансе.

        RCA дедлока: смена/снос активного источника заставляет главный поток ждать
        QThread рендера (QFFmpeg::AudioRenderer) в QThread::wait, НЕ отпуская GIL;
        в это же время AudioRenderer финализирует QAudioOutputWrapper и через
        переопределённый PySide6 disconnectNotify → PyGILState_Ensure тянется за
        тем же GIL. Встречный тупик. Связь QAudioOutput↔QAudioOutputWrapper
        внутренняя (её рвёт сам деструктор) — руками disconnect её не разорвать,
        поэтому единственный выход: увести C++ деструкцию в deleteLater, где она
        отработает в чистом C++-проходе event-loop, вне GIL-удерживающего кванта.

        setSource зовём ТОЛЬКО на свежем пустом инстансе — ему нечего ждать (живого
        рендер-графа нет), значит блокирующего QThread::wait под GIL не возникает.

        ВНИМАНИЕ: путь АСИНХРОННЫЙ — старый нативный контекст гаснет лишь на
        следующем проходе event-loop. Перед СИНХРОННОЙ сменой Space используйте
        release_source_safe(on_done=...), который дожидается деструкции по destroyed."""
        old_player, old_audio = self.player, self.audio_output

        # Снимаем НАШИ Python-слоты со старого плеера — их эхо (positionChanged/
        # durationChanged) не должно дёрнуть слоты уже сносимого транспорта.
        try:
            old_player.positionChanged.disconnect(self._on_position_changed)
            old_player.durationChanged.disconnect(self._on_duration_changed)
        except (RuntimeError, TypeError):
            pass
        # Отвязываем синк ДО отложенного сноса: иначе деструктор старого плеера на
        # следующем тике сбросил бы синк, уже занятый новым инстансом (чёрный кадр).
        try:
            old_player.setVideoSink(None)
        except (RuntimeError, TypeError):
            pass

        # Состояние транспорта переносим на новый контекст: громкость/mute/скорость
        # не должны «слетать» при пересоздании. Геттеры дешёвые (читают поле
        # объекта, не лезут в мьютекс декодера) — безопасны и на сносимом плеере.
        vol, muted = old_audio.volume(), old_audio.isMuted()
        rate = old_player.playbackRate()

        old_player.deleteLater()
        old_audio.deleteLater()

        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(vol)
        self.audio_output.setMuted(muted)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoSink(self.video_widget.videoSink())
        self.player.setPlaybackRate(rate)
        # playbackStateChanged НЕ подключаем НАМЕРЕННО: весь класс ведёт UI по
        # ЛОКАЛЬНОМУ _is_playing_intent и не читает playbackState() (тот за мьютексом
        # медиаплагина). Подключаем только реально используемое — позицию/длительность.
        self.player.positionChanged.connect(self._on_position_changed)
        self.player.durationChanged.connect(self._on_duration_changed)

        if not url.isEmpty():
            self.player.setSource(url)

    def release_source_safe(self, on_done=None):
        """Deadlock-safe снос нативного контекста ПЕРЕД переходом в полноэкранное
        сравнение (зовётся из _trigger_grid_compare вместо синхронного stop()).

        change_source_safe(QUrl()) уводит C++ деструкцию старого плеера/аудио в
        event-loop вне GIL-кванта → дедлок ~QAudioOutputWrapper исключён. on_done
        (если задан) вызывается СТРОГО ПОСЛЕ физической деструкции старого контекста
        — по его сигналу destroyed (он эмитится в ~QObject, когда ~QMediaPlayer уже
        отработал и освободил AVFoundation/Metal). Это и есть смещение смены Space
        на квант ПОСЛЕ деструкции: к моменту on_done живого графического контекста
        уже нет, поэтому root_stack.setCurrentIndex(1) не провоцирует Spaces Jump.
        Оба инварианта (No-Deadlock + No-Spaces-Jump) держатся одновременно."""
        old_player = self.player
        self.change_source_safe(QUrl())
        if on_done is not None:
            old_player.destroyed.connect(lambda *_: on_done())

    def stop(self):
        # СИНХРОННЫЙ нативный stop для ИНТЕРАКТИВНЫХ путей (preview_controller.
        # _stop_video при смене превью; старт скана). Здесь смены Space нет, а halt
        # playback живого источника дёшев. ПЕРЕД переходом в сравнение НЕ
        # используется — там нужен deadlock-safe release_source_safe (отложенная
        # деструкция контекста), т.к. синхронный снос живого источника = встречный
        # GIL-дедлок ~QAudioOutputWrapper (см. change_source_safe).
        self._reset_transport_flags()
        self.player.stop()

    def pause(self):
        """Ставит воспроизведение на паузу, НЕ разрушая источник/декодер.
        Безопасная альтернатива release_source() при переходе в полноэкранное
        сравнение на macOS: уничтожение AVPlayerLayer в fullscreen фатально для
        WindowServer и вызывает сброс Space, тогда как спящий (paused) контекст
        скрыть безопасно.

        ЗАДАЧА 2: решение принимаем по ЛОКАЛЬНОМУ intent, без блокирующего
        playbackState(); сам нативный pause() делегируем в следующий квант."""
        if not self._is_playing_intent:
            return
        self._is_playing_intent = False
        self._sync_play_icon()
        QTimer.singleShot(0, self._do_pause)

    def shutdown(self):
        """Синхронная остановка фонового cv2-воркера превью при закрытии окна.

        _thumb_worker (CompareVideoWorker) — единственный СОБСТВЕННЫЙ поток
        плеера: между запросами кадров он висит в cond.wait() и НЕ охвачен
        release_source()/_cleanup_players (те рвут лишь нативный QMediaPlayer-
        контекст). Его stop() уже инкапсулирует флаг остановки + cond.notify_all()
        + ограниченный wait(2000), поэтому здесь просто зовём его. Идемпотентно:
        повторный вызов на уже остановленном/незапущенном потоке безвреден.

        Вызывается из MainController._on_window_closed — синхронно в GUI-потоке
        (DirectConnection) ДО os._exit, поэтому bounded wait успевает отработать."""
        worker = getattr(self, "_thumb_worker", None)
        if worker is not None and worker.isRunning():
            worker.stop()

    def _sync_play_icon(self):
        """Перерисовывает глиф кнопки строго по локальному intent."""
        if self._is_playing_intent:
            self._set_pause_icon()
        else:
            self._set_play_icon()

    # ─── BLOCK 2: Asynchronous Command Delegation ───────────────────────────
    # Изолированные нативные команды QMediaPlayer. Их КАТЕГОРИЧЕСКИ нельзя звать
    # из тела обработчиков напрямую: на тяжёлом файле декодер держит внутренний
    # мьютекс (QBasicMutex), пока ищет ближайший I-фрейм/демультиплексирует
    # контейнер, и синхронный setPosition/play/pause на GUI-потоке встаёт на этот
    # мьютекс — цикл событий замерзает (stackshot: 65.92 с). Поэтому зовём их
    # ТОЛЬКО отложенно — QTimer.singleShot(0, ...): текущий квант GUI завершается
    # и отрисовывает оптимистичный UI, а нативный вызов выполняется в начале
    # следующего кванта на чистом стеке (без реентерабельности из сигналов плеера).
    def _do_seek(self, target):
        self.player.setPosition(target)

    def _do_play(self):
        self.player.play()

    def _do_pause(self):
        self.player.pause()

    def _apply_intent_to_player(self):
        """Транслирует накопленный intent в одну команду плеера (play/pause),
        делегируя нативный вызов в следующий квант (BLOCK 2)."""
        self._intent_dirty = False
        if self._is_playing_intent:
            QTimer.singleShot(0, self._do_play)
        else:
            QTimer.singleShot(0, self._do_pause)

    def _clear_seek_lock(self):
        """Снимает затвор конвейера после посадки seek (positionChanged) или по
        отложенному фолбэку. Если пользователь жал Play/Pause во время затвора —
        досылаем отложенный intent одной командой."""
        if not self._seek_in_flight:
            return
        self._seek_in_flight = False
        if self._intent_dirty:
            self._apply_intent_to_player()

    def _toggle_play(self):
        # OPTIMISTIC UI: переворачиваем intent и ПЕРЕРИСОВЫВАЕМ иконку НЕМЕДЛЕННО —
        # до любой команды парализованному конечному автомату QMediaPlayer. Кнопка
        # отвечает мгновенно, даже если медиаплагин висит на мьютексе.
        self._is_playing_intent = not self._is_playing_intent
        self._sync_play_icon()

        if self._seek_in_flight:
            # Тяжёлый seek коммитится — НЕ кладём play()/pause() поверх занятого
            # декодера. Запоминаем, что intent сместился: применим по снятию затвора.
            self._intent_dirty = True
            return

        self._apply_intent_to_player()

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
        # Seek приземлился (плеер эмитит positionChanged с новой позицией) —
        # снимаем затвор конвейера и досылаем отложенный Play/Pause, если был.
        if self._seek_in_flight:
            self._clear_seek_lock()
        if not self.slider.isSliderDown():
            self.slider.blockSignals(True)
            self.slider.setValue(position)
            self.slider.blockSignals(False)
        self._update_time_label(position)

    def _on_duration_changed(self, duration):
        self.slider.setRange(0, duration)
        if self._pending_seek and duration > 0:
            target = int(duration * 0.25)
            # BLOCK 2: даже стартовый seek на 25% не зовём синхронно — на тяжёлом
            # контейнере он так же встаёт на мьютекс демультиплексора.
            QTimer.singleShot(0, lambda t=target: self._do_seek(t))
            self.slider.setValue(target)
            self._update_time_label(target)
            self._pending_seek = False

    # Затвор живого скраба: ~33 мс ≈ 30 Гц (паритет с multi_compare).
    _SCRUB_MIN_MS = 33

    def _on_slider_moved(self, position):
        # DROP-TO-SEEK: ползунок НИКОГДА не зовёт player.setPosition() — именно
        # синхронный setPosition на GUI-потоке висел 65.92 с на мьютексе
        # libffmpegmediaplugin (stackshot). Здесь только локальный UI: пауза +
        # cv2-кадр. Реальный setPosition — ровно один, по sliderReleased.

        # Вход в скраб: запоминаем, играли ли мы, и ПЕРЕВОДИМ В ПАУЗУ (оптимистично,
        # без чтения playbackState). Пауза один раз на входе, а не на каждый пиксель.
        entering = not self._scrubbing
        if entering:
            self._scrubbing = True
            self._resume_after_seek = self._is_playing_intent
            if self._is_playing_intent:
                self._is_playing_intent = False
                self._sync_play_icon()
                # BLOCK 2: пауза на входе в скраб — тоже отложенно, GUI не ждёт.
                QTimer.singleShot(0, self._do_pause)

        # Таймкод обновляем всегда (дёшево) — он должен идти за пальцем плавно.
        self._update_time_label(position)

        # THROTTLE: cv2-запросы ограничиваем ~30 Гц, но ПЕРВЫЙ кадр свежего скраба
        # пропускаем всегда — иначе одиночный клик по таймлайну не перерисовал бы
        # целевой кадр. Затвор закрывает шторм запросов к декодеру.
        now = self._scrub_clock.elapsed()
        if not entering and (now - self._last_scrub_ms < self._SCRUB_MIN_MS):
            return
        self._last_scrub_ms = now

        # ВИЗУАЛЬНАЯ ОБРАТНАЯ СВЯЗЬ — ИСКЛЮЧИТЕЛЬНО через фоновый cv2-воркер
        # (CompareVideoWorker с wall-clock лимитами, устойчив к долгим пробам).
        # Кадр прилетит в _on_thumb_ready и нарисуется как статичный preview.
        duration = self.player.duration()
        if duration <= 0 or not self._current_path:
            return
        pct = max(0.0, min(100.0, position / duration * 100.0))
        self._thumb_worker.request_frames([self._current_path], pct)

    def _on_slider_released(self):
        # ЕДИНСТВЕННАЯ точка нативного setPosition за весь скраб (Drop-to-Seek
        # commit). Синхронизирует реальную позицию плеера с тем таймкодом, что
        # показал cv2-скраб, чтобы Play стартовал ровно оттуда, куда домотали.
        self._scrubbing = False

        # ЗАДАЧА 2 — ДВОЙНОЙ ЗАТВОР: пока предыдущий асинхронный seek НЕ приземлился,
        # игнорируем новую попытку seek. Без этого клики по таймлайну копятся в
        # очередь блокирующих setPosition — именно их СТОПКА (по одному на каждый
        # пиксель/клик), сериализованная на мьютексе декодера, дала 65.92 с в
        # stackshot. Висящий resume при этом не оставляем (иначе он сработает на
        # следующем — уже валидном — отпускании).
        if self._seek_in_flight:
            self._resume_after_seek = False
            return

        target = self.slider.value()

        # Затвор поднят: один seek «в полёте». _toggle_play теперь не кладёт play()
        # поверх ещё не отработавшего декодера (intent копится в _intent_dirty).
        self._seek_in_flight = True
        # BLOCK 2: ЕДИНСТВЕННЫЙ нативный setPosition за скраб — и тот ОТЛОЖЕННЫЙ.
        # GUI-поток освобождается немедленно; мьютекс декодера берётся в следующем
        # кванте. Цикл отрисовки не замерзает на возврате из нативной функции.
        QTimer.singleShot(0, lambda t=target: self._do_seek(t))
        self._update_time_label(target)

        # Возобновляем воспроизведение, если играли до начала скраба; иначе
        # остаёмся на свежепромотанном кадре. Иконка уже отражает intent.
        if self._resume_after_seek:
            self._resume_after_seek = False
            self._is_playing_intent = True
            self._sync_play_icon()
            QTimer.singleShot(0, self._do_play)

        # Снимаем затвор по приземлению seek (positionChanged) либо, если события
        # не будет (например, остались на паузе), по ограниченному фолбэку.
        QTimer.singleShot(300, self._clear_seek_lock)

    @staticmethod
    def _fmt_clock(ms) -> str:
        """ms → MM:SS (хронометраж). Отрицательное/мусор клампим в 0."""
        s = max(0, int(ms)) // 1000
        return f"{s // 60:02d}:{s % 60:02d}"

    def _update_time_label(self, ms_pos):
        # Унифицированная телеметрия: ВИДЕО показывает хронометраж
        # [текущая / полная], а НЕ проценты — непрерывное время t имеет прямой
        # физический смысл, в отличие от дискретного индекса кадра. Полную
        # длительность берём у самого плеера (duration() валиден после
        # durationChanged); на паузе/скрабе она не меняется.
        self.lbl_time.setText(
            f"{self._fmt_clock(ms_pos)} / {self._fmt_clock(self.player.duration())}"
        )