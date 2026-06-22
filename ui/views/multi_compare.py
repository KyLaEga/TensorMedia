# ============================================================
# MODULE: ui/views/multi_compare.py
# ============================================================
import os
import time
from PySide6.QtWidgets import (QVBoxLayout, QLabel, QPushButton, QWidget,
                               QCheckBox, QHBoxLayout, QBoxLayout, QFrame,
                               QRadioButton, QSizePolicy, QSlider, QApplication)
from PySide6.QtGui import QPixmap, QImageReader, QPainter, QPalette, QColor
from PySide6.QtCore import Qt, Signal, QTimer, QUrl, QSize, QElapsedTimer
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput

from ui.components.video_player import JumpSlider, VideoSinkWidget
from ui.components.discrete_preview import make_provider, DISCRETE_EXTS
from ui.workers import CompareVideoWorker
from utils.i18n import translator
from utils.theme_manager import ThemeManager

class GridImageLabel(QLabel):
    def __init__(self):
        super().__init__()
        self.setMinimumSize(50, 50)

        # Карточка свободно растягивается в обе стороны и НЕ навязывает высоту по
        # пропорции изображения (раньше heightForWidth делал портретные кадры выше
        # окна, из-за чего приходилось скроллить и было видно лишь часть). Теперь
        # ячейка занимает выделенное место, а paintEvent вписывает изображение
        # целиком (KeepAspectRatio) с леттербоксингом.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Фон видео/гиф-кадра жёстко задаём цветом 'surface' активной темы через
        # QPalette (Window/Base) вместо прозрачного QSS — прозрачность давала
        # чёрную подложку под кадром в светлой теме.
        surface = ThemeManager.colors()["surface"]
        color = QColor(surface)
        self.setAutoFillBackground(True)
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, color)
        palette.setColor(QPalette.ColorRole.Base, color)
        self.setPalette(palette)
        self._pixmap = None
        self._movie = None

    def setPixmap(self, pixmap):
        self._clear_movie()
        self._pixmap = pixmap
        self.update()

    def setMovie(self, movie):
        self._clear_movie()
        self._movie = movie
        super().setMovie(self._movie)
        self._movie.frameChanged.connect(self._on_frame_update)
        self._movie.start()

    def _on_frame_update(self):
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
            except (RuntimeError, TypeError):
                # осознанное глушение: сигнал мог быть не подключен
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

            # Масштабируем строго по доступному rect() с сохранением пропорций,
            # учитывая devicePixelRatio (иначе на Retina изображение выходит за
            # границы карточки в полноэкранном режиме). KeepAspectRatio исключает
            # обрезку, результат центрируется внутри rect().
            rect = self.rect()
            dpr = self.devicePixelRatioF()
            scaled = pm.scaled(
                rect.size() * dpr,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            scaled.setDevicePixelRatio(dpr)

            w = scaled.width() / dpr
            h = scaled.height() / dpr
            x = rect.x() + (rect.width() - w) / 2
            y = rect.y() + (rect.height() - h) / 2

            painter.drawPixmap(int(x), int(y), scaled)
        else:
            super().paintEvent(event)


class MultiCompareWidget(QWidget):
    # ВСТРОЕННАЯ страница сравнения — обычный QWidget внутри центрального
    # QStackedWidget главного окна (index 1). НЕ диалог и НЕ top-level окно:
    # никаких exec()/open()/QEventLoop, никаких setWindowFlags/setWindowModality.
    # Переключение на эту страницу делает хост через stacked_widget.setCurrentIndex,
    # поэтому macOS не создаёт второго NSWindow и не выбрасывает приложение на
    # другой Space ("Spaces Jump").
    #
    # Виджет ПОСТОЯННЫЙ и переиспользуемый: хост вызывает load(file_paths) перед
    # каждым показом, а результат передаётся наружу кастомными сигналами:
    #   compare_confirmed → «Применить»: читатель берёт self.files_to_delete /
    #                       self.delete_hard и удаляет файлы.
    #   compare_cancelled → «Назад»/Esc: ничего не удаляем, хост возвращает index 0.
    compare_confirmed = Signal()
    compare_cancelled = Signal()

    _orphaned_workers = []

    VIDEO_EXTS = {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("compareWidget")
        # Фон страницы = токен 'bg' (#1E1F22), тот же, которым окрашен левый
        # сайдбар (QWidget#sidebar), поэтому область сравнения визуально
        # сливается с интерфейсом. Скоупим по objectName, чтобы фон не
        # каскадировал на дочерние фреймы (карточки/навигация/футер).
        c = ThemeManager.colors()
        self.setStyleSheet(f"QWidget#compareWidget {{ background-color: {c['bg']}; }}")
        # Страница сравнения не должна перехватывать системный фокус: focusIn при
        # показе слоя провоцировал raise окна и Spaces Jump на macOS. Фокус-
        # политику снимаем и с самой страницы, и рекурсивно с детей в showEvent.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.video_exts = self.VIDEO_EXTS

        # Динамическое состояние одной сессии сравнения (наполняется в load()).
        self.file_entries = []
        self.file_paths = []
        self.show_roles = False
        self.has_videos = False
        self.cards = {}
        self.worker = None
        self.pinned_path = None
        # Путь ведущего медиа текущей раскладки: только его асинхронный размер
        # кадра вправе переставить ось пары (см. _apply_orientation /
        # _on_video_size_changed). Существует с момента конструирования, чтобы
        # реактивный слот никогда не падал с AttributeError на раннем кадре.
        self._orientation_lead = None
        self._carousel_index = 0
        self._page_index = 0
        self.files_to_delete = []
        self.delete_hard = False
        # Флаг отложенной инициализации медиа: load() лишь помечает набор как
        # ожидающий, а тяжёлые декодеры (AVFoundation/Metal/QMovie) поднимаются
        # позже — из showEvent, когда страница уже стала активным слоем стека.
        self._decoders_pending = False
        # Гейт «декодеры подняты»: слайдеры/плееры синхронятся только после
        # _init_decoders (см. _sync_active_players/_sync_discrete_slider). Раньше
        # атрибут рождался лишь в load(); инициализируем здесь явно, чтобы
        # инвариант существовал с момента конструирования (нет окна, где чтение
        # гейта падало бы с AttributeError).
        self._decoders_ready = False

        # Постоянный каркас строится один раз; контент наполняет load() при
        # каждом показе страницы. Экземпляр НЕ одноразовый.
        self._build_chrome()

        # Каркас (футер/навигация) строится единожды и НЕ пересобирается load()'ом,
        # поэтому, в отличие от карточек, он не подхватывает смену языка сам по
        # себе. Подписываемся на глобальный сигнал и синхронно обновляем тексты
        # статических кнопок, чтобы нижняя панель не «застревала» на старой локали.
        translator.language_changed.connect(self._retranslate_ui)

    def _build_chrome(self):
        """Строит ПОСТОЯННЫЙ каркас (layout, слайдер, навигация, футер) один раз.

        Карточки и декодеры — динамические, их пересобирает load(); каркас же
        переиспользуется между сессиями, чтобы не пересоздавать виджет страницы.
        """
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        # Корневой контейнер области карточек. Наполняется/перекладывается
        # методом _rebuild_view() при каждой смене режима/страницы.
        self.view_container = QWidget()
        self.view_layout = QHBoxLayout(self.view_container)
        self.view_layout.setContentsMargins(0, 0, 0, 0)
        self.view_layout.setSpacing(8)
        layout.addWidget(self.view_container, stretch=1)

        # ОБЩИЙ ТРАНСПОРТ (Unified Transport). Постоянная панель: Play/Pause +
        # единый seek-слайдер + таймкод. Управляет ВСЕМИ видимыми видео-плеерами
        # синхронно (кадр-в-кадр), а не вытаскивает статичный кадр через cv2 как
        # прежде. Создаётся всегда, но прячется load()'ом, если в наборе нет видео.
        self.slider_container = QFrame()
        self.slider_container.setFixedHeight(50)
        self.slider_container.setStyleSheet("border-radius: 8px;")
        slider_layout = QHBoxLayout(self.slider_container)

        self.btn_play = QPushButton()
        self.btn_play.setFixedSize(36, 36)
        self.btn_play.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_play.setStyleSheet("QPushButton { background: transparent; border: none; }")
        self.btn_play.clicked.connect(self._toggle_play)

        # Слайдер позиции в пермилле (0..1000) — диапазон НЕ зависит от
        # длительности конкретного файла, поэтому ролики разной длины ведутся по
        # одной дроби прогресса (frame-in-frame относительно своей длительности).
        self.slider = JumpSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.setValue(0)
        # Транспортная шина (Pause-on-Drag + троттл). Раскладка сигналов:
        #   sliderPressed  — пользователь зажал ползунок: запоминаем play-state и
        #                    физически паузим все плееры (разгрузка декодера).
        #   sliderMoved    — живой скраб: дросселированный seek (≤50/с).
        #   sliderReleased — отпускание ИЛИ клик-прыжок JumpSlider (он эмитит
        #                    moved+released на mousePress): финальный seek без
        #                    троттла + возврат к воспроизведению, если играло.
        self.slider.sliderPressed.connect(self._on_scrub_start)
        self.slider.sliderMoved.connect(self._scrub_preview)
        self.slider.sliderReleased.connect(self._on_scrub_end)

        self.lbl_time = QLabel("00:00 / 00:00")
        c = ThemeManager.colors()
        self.lbl_time.setStyleSheet(f"color: {c['text']}; border: none;")

        slider_layout.addWidget(self.btn_play)
        slider_layout.addWidget(self.slider, stretch=1)
        slider_layout.addWidget(self.lbl_time)
        layout.addWidget(self.slider_container)

        # Состояние общего транспорта.
        self._is_playing = False
        self._driver_player = None   # видимый плеер, ведущий слайдер/таймкод
        # Затвор Drop-to-Seek: пока отложенный setPosition не приземлился (нет
        # driver positionChanged), новый коммит не ставим — стопка setPosition на
        # тяжёлом наборе сериализуется на мьютексе декодера и морозит GUI.
        self._seek_in_flight = False
        # Аудио — РАЗДЕЛЬНЫЙ микшер: состояние mute/громкости хранится на каждой
        # карточке (frame.muted / frame.volume), общего «эксклюзивного» источника
        # больше нет. Можно заглушить всё, слушать оба трека, крутить громкость
        # каждого независимо.
        # Метка последнего кадра скраба для дросселирования (time.monotonic).
        self._last_seek_ts = 0.0
        self._set_play_icon()

        # ДИСКРЕТНЫЙ ТРАНСПОРТ (Discrete Timeline Sync). Единый индексатор кадра/
        # страницы для GIF/PDF/CBZ: JumpSlider + строго числовой индикатор «n / N».
        # valueChanged синхронно адресует один и тот же индекс у ВСЕХ видимых
        # дискретных карточек через их провайдеры (_GifProvider/_PdfProvider/
        # _CbzProvider). Создаётся всегда, прячется load()'ом, если в наборе нет
        # дискретных форматов. Это аналог общего видеотранспорта, но для статических
        # многокадровых форматов — отдельная ось от карусельных Prev/Next (те листают
        # РАЗНЫЕ файлы, а этот слайдер — кадры/страницы ВНУТРИ видимой пары).
        self.discrete_panel = QFrame()
        self.discrete_panel.setFixedHeight(45)
        self.discrete_panel.setStyleSheet("border-radius: 8px;")
        d_layout = QHBoxLayout(self.discrete_panel)
        d_layout.setContentsMargins(12, 0, 12, 0)
        d_layout.setSpacing(10)

        self.discrete_slider = JumpSlider(Qt.Orientation.Horizontal)
        self.discrete_slider.setRange(0, 0)
        self.discrete_slider.setTracking(True)
        self.discrete_slider.valueChanged.connect(self._on_discrete_index_changed)

        self.lbl_discrete_index = QLabel("0 / 0")
        self.lbl_discrete_index.setStyleSheet(f"color: {c['text']}; border: none;")
        self.lbl_discrete_index.setAlignment(Qt.AlignmentFlag.AlignCenter)

        d_layout.addWidget(self.discrete_slider, stretch=1)
        d_layout.addWidget(self.lbl_discrete_index)
        layout.addWidget(self.discrete_panel)

        # Кооперативный прогрев RAM-кэша GIF-провайдеров (паритет с одиночным
        # просмотром): тик = малый бюджет декода в UI-потоке, без фоновых потоков —
        # нечего глушить при teardown (см. правила завершения).
        self._discrete_prefetch_timer = QTimer(self)
        self._discrete_prefetch_timer.setInterval(0)
        self._discrete_prefetch_timer.timeout.connect(self._discrete_prefetch_tick)

        # Панель навигации карусели: "< Назад" / индикатор страницы / "Вперед >".
        # Кнопки листают текущий режим (пары или карусель «остальных» при pin);
        # те же действия повторяют стрелки ← / →.
        nav_bar = QFrame()
        nav_bar.setStyleSheet("QFrame { border-radius: 8px; }")
        nav_layout = QHBoxLayout(nav_bar)
        nav_layout.setContentsMargins(12, 6, 12, 6)
        nav_layout.setSpacing(10)

        self.btn_prev = QPushButton(translator.tr("cmp_prev"))
        self.btn_prev.setMinimumHeight(36)
        self.btn_prev.clicked.connect(self._prev_page)

        self.btn_next = QPushButton(translator.tr("cmp_next"))
        self.btn_next.setMinimumHeight(36)
        self.btn_next.clicked.connect(self._next_page)

        c = ThemeManager.colors()
        self.lbl_page = QLabel()
        self.lbl_page.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_page.setStyleSheet(f"color: {c['text']}; font-weight: bold; border: none;")

        nav_layout.addWidget(self.btn_prev)
        nav_layout.addStretch()
        nav_layout.addWidget(self.lbl_page)
        nav_layout.addStretch()
        nav_layout.addWidget(self.btn_next)
        layout.addWidget(nav_bar)

        # Bottom action bar — everything on ONE horizontal line:
        #   LEFT  : only the delete-mode radios ("В корзину" / "Насовсем")
        #   STRETCH
        #   RIGHT : action buttons ("Назад" / "Применить")
        footer = QFrame()
        footer.setStyleSheet(f"QFrame {{ border-radius: 8px; border: 1px solid {c['border']}; }}")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(12, 8, 12, 8)
        footer_layout.setSpacing(10)

        self.rb_trash = QRadioButton(f"🗑️ {translator.tr('cmp_trash')}")
        self.rb_trash.setStyleSheet("border: none;")
        self.rb_trash.setChecked(True)
        self.rb_hard = QRadioButton(f"🔥 {translator.tr('cmp_hard')}")
        self.rb_hard.setStyleSheet("color: #DA3633; font-weight: bold; border: none;")

        # Кнопка «Назад»: отменяет сессию сравнения (compare_cancelled) и
        # возвращает хост на главную страницу стека.
        self.btn_back = QPushButton(translator.tr("cmp_cancel"))
        self.btn_back.setMinimumHeight(ThemeManager.BUTTON_HEIGHT_PRIMARY)
        self.btn_back.clicked.connect(self._go_back)

        self.btn_confirm = QPushButton(translator.tr("cmp_apply"))
        self.btn_confirm.setObjectName("primary")
        self.btn_confirm.setMinimumHeight(ThemeManager.BUTTON_HEIGHT_PRIMARY)
        self.btn_confirm.clicked.connect(self._confirm)

        # Left: delete-mode radios in a single row.
        footer_layout.addWidget(self.rb_trash)
        footer_layout.addWidget(self.rb_hard)
        # Spacer pushes the action buttons to the right edge.
        footer_layout.addStretch()
        # Right: back / apply.
        footer_layout.addWidget(self.btn_back)
        footer_layout.addWidget(self.btn_confirm)
        layout.addWidget(footer)

    def load(self, file_paths):
        """Загружает новый набор файлов в УЖЕ существующую страницу сравнения.

        Полностью сбрасывает предыдущую сессию (воркер, гифки, карточки) и
        пересобирает динамический контент. Вызывается хостом ПЕРЕД каждым
        переключением на страницу сравнения; виджет переиспользуется между
        сессиями (не пересоздаётся).
        """
        self._teardown_session()

        # Each entry may be a bare path (legacy) or a (path, is_ref) tuple.
        # Normalise to (path, is_ref) and keep references first so the visual
        # blocks ("Reference" vs "Inbox") read top-to-bottom.
        self.file_entries = []
        for entry in file_paths:
            if isinstance(entry, (tuple, list)):
                path, is_ref = entry[0], bool(entry[1])
            else:
                path, is_ref = entry, False
            self.file_entries.append((path, is_ref))
        self.file_entries.sort(key=lambda e: not e[1])  # references (True) first
        self.file_paths = [p for p, _ in self.file_entries]
        # Only label/colour cards when both kinds are present — otherwise the
        # badges are just noise (e.g. single-folder duplicate groups).
        self.show_roles = any(r for _, r in self.file_entries) and \
            not all(r for _, r in self.file_entries)
        # Use the normalised flat path list — `file_paths` may hold (path, is_ref)
        # tuples, which would blow up os.path.splitext with a TypeError.
        self.has_videos = any(os.path.splitext(p)[1].lower() in self.video_exts for p in self.file_paths)

        # Сброс состояния сессии + футера/слайдера под новый набор.
        self.files_to_delete = []
        self.delete_hard = False
        self.pinned_path = None
        self._orientation_lead = None   # ось пересчитает _apply_orientation
        self._carousel_index = 0
        self._page_index = 0
        self.rb_trash.setChecked(True)
        self.slider_container.setVisible(self.has_videos)
        self.slider.blockSignals(True)
        self.slider.setValue(0)
        self.slider.blockSignals(False)

        # Сброс дискретного транспорта под новый набор. Диапазон выставит
        # _init_decoders → _sync_discrete_slider, когда провайдеры подняты и
        # известен видимый набор; до тех пор панель скрыта.
        self.discrete_panel.setVisible(False)
        self.discrete_slider.blockSignals(True)
        self.discrete_slider.setRange(0, 0)
        self.discrete_slider.setValue(0)
        self.discrete_slider.blockSignals(False)
        self.lbl_discrete_index.setText("0 / 0")

        # Сброс общего транспорта под новый набор. Воспроизведение стартует на
        # паузе; звук по умолчанию заглушён на ВСЕХ карточках — пользователь сам
        # включает нужные дорожки независимым микшером каждой карточки.
        self._is_playing = False
        self._driver_player = None
        self._seek_in_flight = False
        self.lbl_time.setText("00:00 / 00:00")
        self._set_play_icon()
        self._decoders_ready = False

        # Свежий воркер видеокадров на сессию.
        self.worker = CompareVideoWorker()
        self.worker.frame_ready.connect(self._on_frame_ready)

        # Карточки создаются заново под новый набор и далее переиспользуются
        # _rebuild_view()'ом без пересоздания (чтобы не ломать видео/гиф-вьюшки).
        self.cards = {}
        for path, is_ref in self.file_entries:
            self.cards[path] = self._create_card(path, is_ref)

        self._sync_pin_buttons()
        self._rebuild_view()

        # ОТЛОЖЕННАЯ ИНИЦИАЛИЗАЦИЯ МЕДИА (macOS Spaces Jump). Тяжёлый
        # мультимедийный конвейер (AVFoundation/Metal у видео, QMovie/декодеры у
        # гиф и картинок) здесь НЕ запускаем: на этот момент страница ещё не
        # стала активным слоем QStackedWidget (setCurrentIndex ещё впереди) и
        # физически не отрисована. Поднятие графических контекстов до показа
        # привязывает их к ИСХОДНОМУ Space, и в полноэкранном режиме WindowServer
        # выбрасывает пользователя на другой рабочий стол. Поэтому лишь помечаем
        # набор ожидающим — реальный старт декодеров выполнит showEvent, когда
        # виджет уже выйдет на экран в ТЕКУЩЕМ Space.
        self._decoders_pending = True
        if self.isVisible():
            # Страница уже активна (повторный load на видимом виджете): даём
            # циклу событий завершить перекладку/отрисовку и стартуем на тик позже.
            QTimer.singleShot(0, self._maybe_init_decoders)

    def showEvent(self, event):
        # Виджет стал текущим слоем стека (setCurrentIndex) и выходит на экран.
        # Запуск декодеров откладываем ещё на один тик событий (singleShot(0)),
        # чтобы первый paint успел пройти — только тогда AVFoundation/Metal-
        # контексты гарантированно рождаются в активном (текущем) Space.
        super().showEvent(event)
        # Снимаем фокус-политику со ВСЕХ детей (карточки/кнопки/слайдер): любой из
        # них при показе мог «утащить» фокус и спровоцировать raise окна -> Spaces
        # Jump. Делается на каждом показе, т.к. карточки пересобираются в load().
        for child in self.findChildren(QWidget):
            child.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if self._decoders_pending:
            QTimer.singleShot(0, self._maybe_init_decoders)

    def _maybe_init_decoders(self):
        # Однократный запуск отложенных декодеров. Флаг защищает от повторной
        # инициализации: showEvent может прийти не один раз за сессию, а load()
        # на видимом виджете тоже планирует этот вызов.
        if not self._decoders_pending:
            return
        self._decoders_pending = False
        self._init_decoders()

    def _teardown_session(self):
        """Глушит воркер/гифки и сносит карточки предыдущей сессии.

        BLOCK 1 (Asynchronous Teardown / «гильотина»). РАНЬШЕ снос видео-карточек
        шёл синхронно в ОДНОМ кванте GUI-потока: card.player.stop() +
        setSource(QUrl()) + deleteLater() карточки (а плеер — её ребёнок). На
        macOS setSource синхронно ждёт QThread рендера Qt Multimedia, который для
        финализации Shiboken-оберток тянется за GIL, удерживаемым этим же
        GUI-потоком → GIL-дедлок при закрытии панели сравнения (Назад/Применить).

        Теперь — две фазы:
          ФАЗА 1 (синхронно): глушим сигналы плееров (blockSignals(True)), рвём
            драйвер/воркер, снимаем карточки с раскладки и прячем (hide ДО
            reparent — орфан видимого виджета на macOS = Spaces Jump), отвязываем
            self.cards (load() сразу строит новый набор).
          ФАЗА 2 (QTimer.singleShot(0) → _cleanup_players): на следующем тике,
            когда event-loop прокрутился и потоки Qt Multimedia отпустили GIL,
            deadlock-safe сносим медиа-контекст карточек (отвязка синка +
            deleteLater плеера/аудио, БЕЗ stop()/setSource на живом инстансе) и
            сами карточки (deleteLater).
        """
        # Снимаем ожидание отложенной инициализации: если сессию сворачивают
        # (Назад/Применить или новый load) до того, как showEvent поднял медиа,
        # запланированный _maybe_init_decoders станет no-op по сброшенному флагу.
        self._decoders_pending = False
        self._decoders_ready = False
        # Глушим прогрев кэша GIF до сноса карточек: провайдеры закроет фаза 2
        # (_cleanup_players), а тик не должен дёрнуть уже закрытый поставщик.
        self._discrete_prefetch_timer.stop()
        self._detach_driver()
        self._is_playing = False
        if self.worker is not None:
            self._detach_worker()
            self.worker = None
        self._cleanup()

        # Отвязываем набор сразу: повторный load() строит новые карточки, а старые
        # (захваченные в dead) досносит фаза 2 уже вне критического кванта.
        dead = list(self.cards.values())
        self.cards = {}

        # ФАЗА 1 — синхронный разрыв. Снимаем карточки с раскладки и прячем.
        while self.view_layout.count():
            item = self.view_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
        for card in dead:
            # Глушим сигналы плеера/аудио ДО любого stop()/setSource: их эхо не
            # должно дёрнуть слоты уже сносимого транспорта.
            if getattr(card, 'is_video', False) and card.player is not None:
                card.player.blockSignals(True)
                if card.audio is not None:
                    card.audio.blockSignals(True)
            # hide() ДО setParent(None) (фаза 2): орфанинг видимого виджета на
            # macOS выкидывает top-level NSWindow на активный Space (Spaces Jump).
            card.hide()

        # ФАЗА 2 — отложенная гильотина.
        QTimer.singleShot(0, lambda cards=dead: self._cleanup_players(cards))

    def _cleanup_players(self, cards):
        """ФАЗА 2 BLOCK 1: вне инициировавшего кванта освобождает нативный медиа-
        контекст каждой карточки и сносит сами карточки.

        Снос плеера идёт через ту же deadlock-safe деструкцию, что и карусельное
        листание (_release_card_player): отвязка синка + deleteLater() плеера/аудио,
        БЕЗ stop()/setSource(QUrl()) на живом инстансе. Прежний синхронный
        stop()+setSource(QUrl()) — даже отложенный на тик — оставался встречным
        GIL-дедлоком ~QAudioOutputWrapper, если поток Qt Multimedia в этот момент
        ещё держал источник; deleteLater уводит C++ деструкцию в чистый проход
        event-loop, где GUI-поток уже не удерживает GIL."""
        for card in cards:
            # Закрываем провайдера дискретной карточки — освобождаем pypdfium2/zip/PIL
            # хэндлы и RAM-кэш кадров (паритет с _teardown одиночного просмотра).
            prov = getattr(card, 'provider', None)
            if prov is not None:
                try:
                    prov.close()
                except Exception:
                    pass
                card.provider = None
            if getattr(card, 'is_video', False) and card.player is not None:
                # Тот же безопасный снос, что и при карусельном листании: НИКАКИХ
                # stop()/setSource(QUrl()) на живом инстансе (GIL-дедлок
                # ~QAudioOutputWrapper), а отвязка синка + deleteLater плеера/аудио.
                # Карточка всё равно уходит в deleteLater ниже, но явный отложенный
                # снос её медиа-детей раньше освобождает AVFoundation в том же
                # чистом C++-проходе event-loop, без in-place ожидания рендер-потока.
                self._release_card_player(card)
            card.setParent(None)
            card.deleteLater()

    def _others(self):
        """Остальные карточки (все, кроме закреплённой) в порядке file_entries."""
        return [p for p, _ in self.file_entries if p != self.pinned_path]

    def _page_count(self):
        """Число «страниц» текущего режима."""
        if self.pinned_path is not None:
            # Закреплённый режим: по одному «остальному» файлу на страницу.
            return max(1, len(self._others()))
        # Обычный режим: пары.
        return max(1, (len(self.file_entries) + 1) // 2)

    def _apply_orientation(self, lead_path):
        """Триггерная модель оси пары карточек: AR = W/H ведущего медиа.

        ДВА КОНТУРА расчёта AR — но НИ ОДИН не трогает cv2 в GUI-потоке:
        • РАСТРЫ (фото/гиф) — мгновенная проба заголовка через QImageReader
          (probe_media_ar): без декода пикселей, без блокировки потока;
        • ВИДЕО — синхронный cv2.VideoCapture здесь ЗАПРЕЩЁН. Парс заголовка/
          MOOV-атома гигабайтного ролика занимал секунды и намертво вешал
          главный поток (GUI Thread Lockup). Ставим ДЕФОЛТНУЮ ось сразу, а
          реальную ориентацию применит реактивный слот _on_video_size_changed —
          по нативному QVideoSink.videoSizeChanged либо по cv2-превью из фонового
          CompareVideoWorker (последнее важно на macOS, где плеер на паузе кадр
          в синк не отдаёт, и превью — единственный источник AR до первого Play).

        Политику порога держит direction_for_ar (LANDSCAPE_AR_THRESHOLD = 1.5):
        4:3 и уже → LeftToRight, истинно широкие (>=16:9) → TopToBottom.
        setDirection() меняет ось QBoxLayout на месте — без пересоздания layout и
        без репарентинга карточек (репарентинг в fullscreen — триггер Spaces Jump)."""
        from utils.media_probe import probe_media_ar, direction_for_ar
        # Ведущее медиа набора: ТОЛЬКО его поздний videoSizeChanged/превью вправе
        # переставить ось (запоздавший кадр ушедшей со страницы карточки — нет).
        self._orientation_lead = lead_path
        if os.path.splitext(lead_path)[1].lower() in self.video_exts:
            # Видео: поток не блокируем — дефолтная ось, дальше реактивно.
            self.view_layout.setDirection(QBoxLayout.Direction.LeftToRight)
            return
        self.view_layout.setDirection(direction_for_ar(probe_media_ar(lead_path)))

    def _on_video_size_changed(self, path, size):
        """Реактивная ось по АСИНХРОННОМУ размеру кадра ведущего видео.

        Два источника сигнала, оба вне GUI-блокировки:
          • QVideoSink.videoSizeChanged(QSize) — нативный сигнал плеера, когда
            декодер физически отдал размер кадра (срабатывает на воспроизведении);
          • размер cv2-превью из CompareVideoWorker (см. _on_frame_ready) — важен
            на macOS, где плеер на паузе кадр в синк не рендерит.

        Ось переставляем ТОЛЬКО для текущего ведущего медиа (self._orientation_lead)
        и только при валидном размере — иначе запоздавший кадр уже скрытой карточки
        перекосил бы раскладку видимой пары. setDirection дёргаем лишь при реальной
        смене оси (идемпотентность: повторные кадры того же AR не трогают layout)."""
        if path != self._orientation_lead or size is None:
            return
        w, h = size.width(), size.height()
        if w <= 0 or h <= 0:
            return
        from utils.media_probe import direction_for_ar
        direction = direction_for_ar(w / h)
        if self.view_layout.direction() != direction:
            self.view_layout.setDirection(direction)

    def _rebuild_view(self):
        """Перекладывает уже созданные карточки в layout согласно режиму."""
        # Снимаем карточки с раскладки. НЕ делаем setParent(None): орфанинг
        # виджета на macOS превращает его в top-level NSWindow на активном Space
        # (Spaces Jump в fullscreen). Карточки остаются детьми view_container —
        # takeAt лишь убирает их из layout, а hide() гасит до повторной вставки.
        while self.view_layout.count():
            item = self.view_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()

        if self.pinned_path is not None and self.pinned_path in self.cards:
            # Слева — жёстко закреплённая карточка, справа — один из «остальных».
            others = self._others()
            self._carousel_index = max(0, min(self._carousel_index, len(others) - 1))

            # Ось пары задаёт ЗАКРЕПЛЁННАЯ карточка (эталон сравнения).
            self._apply_orientation(self.pinned_path)

            # ПОРЯДОК ВАЖЕН: сперва addWidget (реальный reparent внутрь layout),
            # только потом show(). show() до перепарентинга = top-level NSWindow.
            pinned_card = self.cards[self.pinned_path]
            self.view_layout.addWidget(pinned_card, stretch=1)
            pinned_card.show()

            for path in others:
                card = self.cards[path]
                if others and path == others[self._carousel_index]:
                    self.view_layout.addWidget(card, stretch=1)
                    card.show()
                else:
                    card.hide()
        else:
            # Обычный режим: пара карточек на странице.
            total_pages = self._page_count()
            self._page_index = max(0, min(self._page_index, total_pages - 1))
            start = self._page_index * 2
            page_paths = [p for p, _ in self.file_entries][start:start + 2]
            if page_paths:
                self._apply_orientation(page_paths[0])
            for path, _ in self.file_entries:
                card = self.cards[path]
                if path in page_paths:
                    self.view_layout.addWidget(card, stretch=1)
                    card.show()
                else:
                    card.hide()

        self._update_nav()
        # Видимый набор сменился (пин/листание) — поднять источники у новых
        # видимых видео и освободить у ушедших со страницы.
        self._sync_active_players()
        # Тот же ресинк для дискретного транспорта: пересчёт диапазона слайдера
        # под новую видимую пару и перерисовка кадра по текущему индексу.
        self._sync_discrete_slider()

    def _toggle_pin(self, path):
        """Закрепить/открепить карточку по нажатию кнопки 📌."""
        if self.pinned_path == path:
            self.pinned_path = None          # снятие закрепления → обычный режим
        else:
            self.pinned_path = path           # новое закрепление
            self._carousel_index = 0
        self._sync_pin_buttons()
        self._rebuild_view()

    def _sync_pin_buttons(self):
        """Обновляет подписи/состояние кнопок Pin на всех карточках."""
        for path, card in self.cards.items():
            btn = getattr(card, 'pin_btn', None)
            if btn is None:
                continue
            pinned = (path == self.pinned_path)
            btn.setChecked(pinned)
            btn.setText(translator.tr("cmp_unpin") if pinned else translator.tr("cmp_pin"))

    def _prev_page(self):
        if self.pinned_path is not None:
            if self._carousel_index > 0:
                self._carousel_index -= 1
                self._rebuild_view()
        elif self._page_index > 0:
            self._page_index -= 1
            self._rebuild_view()

    def _next_page(self):
        last = self._page_count() - 1
        if self.pinned_path is not None:
            if self._carousel_index < last:
                self._carousel_index += 1
                self._rebuild_view()
        elif self._page_index < last:
            self._page_index += 1
            self._rebuild_view()

    def _on_delete_toggled(self, checked, path):
        """Отметка карточки «в корзину» → автопрокрутка карусели вперёд.

        Срабатывает только при установке галочки (checked=True). Если мы уже на
        последней странице, _next_page() просто ничего не делает.
        """
        if not checked:
            return
        self._next_page()

    def _update_nav(self):
        total = self._page_count()
        idx = self._carousel_index if self.pinned_path is not None else self._page_index
        self.btn_prev.setEnabled(idx > 0)
        self.btn_next.setEnabled(idx < total - 1)
        if self.pinned_path is not None:
            self.lbl_page.setText(f"{translator.tr('cmp_pinned_nav')} · {idx + 1} / {total}")
        else:
            self.lbl_page.setText(f"{idx + 1} / {total}" if total else "0 / 0")

    def _retranslate_ui(self):
        """Обновляет тексты СТАТИЧЕСКИХ элементов каркаса из текущей локали.

        Карточки (pin/delete/badge) пересобирает load() при каждом показе, поэтому
        они всегда «свежие». Постоянная навигация и футер строятся один раз — их
        тексты подтягиваем здесь по сигналу translator.language_changed. Читаем
        напрямую из translator.tr(), а не из закэшированных при инициализации строк.
        """
        self.btn_prev.setText(translator.tr("cmp_prev"))
        self.btn_next.setText(translator.tr("cmp_next"))
        self.rb_trash.setText(f"🗑️ {translator.tr('cmp_trash')}")
        self.rb_hard.setText(f"🔥 {translator.tr('cmp_hard')}")
        self.btn_back.setText(translator.tr("cmp_cancel"))
        self.btn_confirm.setText(translator.tr("cmp_apply"))
        # Индикатор страницы тоже содержит локализованный префикс (cmp_pinned_nav).
        self._update_nav()

    def keyPressEvent(self, event):
        # Стрелки клавиатуры дублируют кнопки навигации карусели; Esc — «Назад».
        key = event.key()
        if key == Qt.Key.Key_Left:
            self._prev_page()
            return
        if key == Qt.Key.Key_Right:
            self._next_page()
            return
        if key == Qt.Key.Key_Escape:
            self._go_back()
            return
        super().keyPressEvent(event)

    def _create_card(self, path, is_ref=False):
        c = ThemeManager.colors()
        # Reference cards get a green outline, Inbox cards a blurple one, so the
        # two roles are visually separated even though they share one grid.
        if self.show_roles:
            border_color = "#3BA55D" if is_ref else "#5865F2"
        else:
            border_color = c["border"]

        # Родитель задаётся СРАЗУ (view_container), а не отложенно через
        # addWidget. Parentless-виджет на macOS при первом show()/realize на миг
        # становится top-level NSWindow на активном Space — в нативном fullscreen
        # это и есть «Spaces Jump» (перебрасывание между рабочими столами).
        frame = QFrame(self.view_container)
        frame.setObjectName("compareCard")
        # Visible 2px outline around every card (role-coloured when mixed).
        # Фон НЕ задаём жёстко — карточка наследует цвет палитры активной темы,
        # поэтому в светлой теме не остаётся тёмных островов. Фиксируем только
        # рамку и геометрию.
        frame.setStyleSheet(
            "QFrame#compareCard { background-color: transparent; padding: 2px; "
            f"border-radius: 6px; border: 2px solid {border_color}; }}"
        )
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        l = QVBoxLayout(frame)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(0)

        # Coloured role header strip — the clear "Reference vs Inbox" divider.
        if self.show_roles:
            badge = QLabel(translator.tr("cmp_badge_ref") if is_ref else translator.tr("cmp_badge_inbox"))
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setStyleSheet(
                f"background-color: {border_color}; color: white; "
                "font-weight: bold; padding: 3px; "
                "border-top-left-radius: 4px; border-top-right-radius: 4px;"
            )
            l.addWidget(badge)

        # Header panel pinned to the TOP of the card: filename/path on the left
        # and the "Delete" checkbox on the right, so deleting never requires
        # scrolling past a tall image.
        header_panel = QWidget()
        hp_layout = QHBoxLayout(header_panel)
        hp_layout.setContentsMargins(10, 6, 10, 6)
        hp_layout.setSpacing(8)

        info = QLabel(f"{os.path.basename(path)}")
        info.setStyleSheet(f"color: {c['text']}; font-weight: bold;")
        info.setWordWrap(True)  # long filenames wrap instead of overflowing the card
        info.setToolTip(path)   # full path on hover

        # Кнопка "Закрепить": переводит окно в режим pin (эта карточка слева,
        # остальные листаются справа по одной). Повторное нажатие — открепляет.
        pin_btn = QPushButton(translator.tr("cmp_pin"))
        pin_btn.setCheckable(True)
        pin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        pin_btn.setStyleSheet(
            f"QPushButton {{ font-weight: bold; color: {c['text']}; "
            f"background-color: {c['border']}; border-radius: 4px; padding: 4px 8px; border: none; }}"
            "QPushButton:hover { background-color: #5C5E66; }"
            "QPushButton:checked { background-color: #3BA55D; color: white; }"
        )
        pin_btn.clicked.connect(lambda _=False, p=path: self._toggle_pin(p))

        is_video = os.path.splitext(path)[1].lower() in self.video_exts

        # НЕЗАВИСИМЫЙ микро-микшер карточки (только у видео): безрамочный mute-
        # тумблер (глифы volume/volume_muted) + компактный слайдер громкости.
        # Никакого эксклюзива — каждая карточка управляет ТОЛЬКО своим звуком,
        # поэтому возможны: полный мут всех, два трека одновременно, раздельный
        # уровень каждого. vol_slider создаётся ниже (нужен frame как родитель).
        audio_btn = None
        if is_video:
            audio_btn = QPushButton()
            audio_btn.setFixedSize(28, 28)
            audio_btn.setIconSize(QSize(18, 18))
            audio_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            audio_btn.setStyleSheet("QPushButton { background: transparent; border: none; }")
            audio_btn.clicked.connect(lambda _=False, f=frame: self._toggle_card_mute(f))

            vol_slider = QSlider(Qt.Orientation.Horizontal, frame)
            vol_slider.setRange(0, 100)
            vol_slider.setValue(60)
            vol_slider.setFixedWidth(70)
            vol_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            vol_slider.setCursor(Qt.CursorShape.PointingHandCursor)
            vol_slider.valueChanged.connect(lambda v, f=frame: self._change_card_volume(f, v))

        cb = QCheckBox(f"🗑  {translator.tr('cmp_delete')}")
        cb.setStyleSheet("font-weight: bold; color: #DA3633;")
        # После отметки «в корзину» автоматически листаем карусель на следующий
        # кадр — пользователю не нужно перелистывать вручную после выбора.
        cb.toggled.connect(lambda checked, p=path: self._on_delete_toggled(checked, p))

        hp_layout.addWidget(info, stretch=1)
        # Audio (mute + volume) + Pin + Delete pinned to the TOP-RIGHT corner.
        if audio_btn is not None:
            hp_layout.addWidget(audio_btn, alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            hp_layout.addWidget(vol_slider, alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        hp_layout.addWidget(pin_btn, alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        hp_layout.addWidget(cb, alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        l.addWidget(header_panel)

        frame.path = path
        frame.checkbox = cb
        frame.pin_btn = pin_btn
        frame.is_video = is_video
        # Дискретный формат (GIF/PDF/CBZ): кадр/страница рисуется не локальным
        # QMovie/QImageReader, а провайдером из discrete_preview (Provider
        # Injection). provider поднимается отложенно в _init_decoders и адресуется
        # дискретным слайдером через provider.frame(index).
        frame.is_discrete = (not is_video) and \
            (os.path.splitext(path)[1].lower() in DISCRETE_EXTS)
        frame.provider = None
        frame.audio_btn = audio_btn
        frame.vol_slider = vol_slider if is_video else None
        frame.player = None
        frame.audio = None
        # Состояние независимого микшера карточки. По умолчанию звук заглушён, а
        # уровень = 60% (совпадает со стартовым положением vol_slider). _sync_
        # active_players переприменяет это при поднятии/смене источника плеера.
        frame.muted = True
        frame.volume = 60

        if is_video:
            # ЖИВОЙ ПЛЕЕР: рендер кадров вручную из QVideoSink (VideoSinkWidget),
            # БЕЗ нативного QVideoWidget-оверлея — тот же безопасный для Spaces Jump
            # путь, что и одиночный просмотр. Контекст AVFoundation/ffmpeg реально
            # поднимается отложенно (_activate_video_card из showEvent), здесь лишь
            # каркас. Фон/леттербокс рисует сам VideoSinkWidget (роль 'surface').
            video_widget = VideoSinkWidget(bg_role="surface")
            l.addWidget(video_widget, stretch=1)
            frame.lbl_img = video_widget

            # РЕАКТИВНАЯ ОРИЕНТАЦИЯ: нативный сигнал готового размера кадра.
            # videoSizeChanged идёт БЕЗ аргумента (property-notify) — актуальный
            # QSize читаем геттером sink.videoSize() в слоте. AR вычисляет
            # _on_video_size_changed, cv2 в GUI-потоке не трогаем. Сигнал летит,
            # когда декодер физически отдал кадр (на воспроизведении); пауза-кейс
            # на macOS закрывает cv2-превью воркера (см. _on_frame_ready).
            sink = video_widget.videoSink()
            sink.videoSizeChanged.connect(
                lambda p=path, s=sink: self._on_video_size_changed(p, s.videoSize()))

            player = QMediaPlayer(frame)
            audio = QAudioOutput(frame)
            audio.setVolume(frame.volume / 100.0)
            audio.setMuted(frame.muted)   # независимый микшер карточки
            player.setAudioOutput(audio)
            player.setVideoSink(video_widget.videoSink())
            self._set_infinite_loop(player)
            frame.player = player
            frame.audio = audio
            self._refresh_audio_icon(frame)
        else:
            lbl_img = GridImageLabel()
            # Фон кадра уже задан палитрой 'surface' внутри GridImageLabel; QSS с
            # 'transparent' здесь не ставим, иначе он перебил бы палитру и вернул
            # чёрную подложку под гиф/картинку.
            l.addWidget(lbl_img, stretch=1)
            frame.lbl_img = lbl_img

        return frame

    def _init_decoders(self):
        dpr = self._device_pixel_ratio()
        v_paths = [p for p in self.file_paths if os.path.splitext(p)[1].lower() in self.video_exts]
        for p in self.file_paths:
            ext = os.path.splitext(p)[1].lower()
            card = self.cards[p]
            if ext in DISCRETE_EXTS:
                # PROVIDER INJECTION: ровно тот же поставщик, что и одиночный
                # просмотр — _PdfProvider (Retina-растеризация alpha=False, поэтому
                # белая подложка страницы запекается в растр), _GifProvider (RAM-
                # кэш кадров), _CbzProvider. Локального QMovie/QImageReader-пути
                # для дискретных форматов больше нет: кадр рисует дискретный слайдер
                # через card.provider.frame(index). DPR прокидываем в фабрику —
                # его использует только PDF для Retina-зума, прочим безвреден.
                card.provider = make_provider(p, render_scale=dpr)
            elif ext not in self.video_exts:
                reader = QImageReader(p)
                img = reader.read()
                pm = QPixmap.fromImage(img) if not img.isNull() else None
                if pm is None:
                    # Qt не декодирует формат (типично для HEIC: сборки PySide6 не
                    # всегда несут плагин qheif) — откат на PIL + pillow-heif,
                    # паритет с одиночным превью (image_label) и delete-диалогом.
                    try:
                        from PIL import Image
                        from utils.image_io import register_heif, pil_to_qimage
                        register_heif()
                        with Image.open(p) as im:
                            pm = QPixmap.fromImage(pil_to_qimage(im, max_side=1600))
                    except Exception:
                        pm = None
                if pm is not None and not pm.isNull():
                    card.lbl_img.setPixmap(pm)
        if v_paths:
            # Мгновенный статичный кадр в каждое видео ДО прогрева QMediaPlayer —
            # тот же приём, что в одиночном просмотре: cv2 отдаёт превью, плеер
            # подхватит живые кадры по set_preview_image → первый valid frame.
            self.worker.request_frames(v_paths, 25)
        # Гейт снят: страница уже на экране в текущем Space — поднимать живые
        # медиа-контексты безопасно. Поднимаем источники у видимых видео-карточек,
        # приводим в соответствие аудио-фокус и выставляем дискретный слайдер.
        self._decoders_ready = True
        self._sync_active_players()
        self._sync_discrete_slider()

    def _set_infinite_loop(self, player):
        """Бесконечный луп плеера. API зацикливания между версиями PySide6
        слегка разнится — пробуем enum, затем сырой int, иначе тихо пропускаем
        (драйвер всё равно перезапустит набор по EndOfMedia)."""
        try:
            player.setLoops(QMediaPlayer.Loops.Infinite)
        except (AttributeError, TypeError):
            try:
                player.setLoops(-1)
            except Exception:
                pass

    # ---- Дискретный транспорт (Discrete Timeline Sync) ---------------------

    def _device_pixel_ratio(self) -> float:
        """DPR текущего экрана для Retina-растеризации PDF (см. _PdfProvider).
        Устойчив к ещё не показанному виджету: screen() → primaryScreen → 1.0."""
        scr = self.screen()
        if scr is not None:
            return scr.devicePixelRatio()
        app = QApplication.instance()
        if app is not None and app.primaryScreen() is not None:
            return app.primaryScreen().devicePixelRatio()
        return self.devicePixelRatioF() or 1.0

    def _visible_discrete_cards(self):
        """Видимые сейчас дискретные карточки с уже поднятым провайдером
        (по ним идёт синхронный скраб кадра/страницы общим слайдером)."""
        return [c for c in self.cards.values()
                if getattr(c, 'is_discrete', False) and c.isVisible()
                and getattr(c, 'provider', None) is not None]

    def _sync_discrete_slider(self):
        """Приводит дискретный слайдер к ТЕКУЩЕМУ видимому набору.

        Диапазон = максимум по числу кадров/страниц среди видимых дискретных
        карточек (источник с меньшим N клампится в _render_discrete — его последняя
        страница «застывает»). Для одиночного кадра панель скрыта. Вызывается из
        _init_decoders (после подъёма провайдеров) и из _rebuild_view (карусель
        сменила видимую пару). Текущий индекс сохраняется между перелистываниями,
        лишь клампится под новый диапазон."""
        if not self._decoders_ready:
            return
        cards = self._visible_discrete_cards()
        total = max((c.provider.count for c in cards), default=0)
        self.discrete_panel.setVisible(total > 1)
        self.discrete_slider.blockSignals(True)
        self.discrete_slider.setRange(0, max(0, total - 1))
        idx = max(0, min(self.discrete_slider.value(), max(0, total - 1)))
        self.discrete_slider.setValue(idx)
        self.discrete_slider.blockSignals(False)
        self._render_discrete(idx)
        # RAM-кэш GIF: кооперативный прогрев только если в видимом наборе есть
        # поставщик с prefetch_step (его имеет лишь _GifProvider).
        if any(hasattr(c.provider, "prefetch_step") for c in cards):
            self._discrete_prefetch_timer.start()

    def _on_discrete_index_changed(self, index):
        # Прямой синхронный слот индексатора: один индекс адресует кадр/страницу
        # у ВСЕХ видимых дискретных карточек и рисует СРАЗУ (seek-in-sync).
        self._render_discrete(index)

    def _render_discrete(self, index):
        """Рисует кадр index синхронно во всех видимых дискретных карточках."""
        index = int(index)
        max_count = 0
        for card in self._visible_discrete_cards():
            prov = card.provider
            local = min(index, prov.count - 1)   # короткий источник «застывает»
            card.lbl_img.setPixmap(QPixmap.fromImage(prov.frame(local)))
            max_count = max(max_count, prov.count)
        if max_count:
            shown = min(index, max_count - 1) + 1
            self.lbl_discrete_index.setText(f"{shown} / {max_count}")

    # Бюджет одного тика прогрева кэша GIF, мс (паритет с одиночным просмотром).
    _DISCRETE_PREFETCH_BUDGET_MS = 12

    def _discrete_prefetch_tick(self):
        clock = QElapsedTimer()
        clock.start()
        while clock.elapsed() < self._DISCRETE_PREFETCH_BUDGET_MS:
            pending = False
            # Только ВИДИМЫЕ карточки: офскрин-GIF (другие страницы карусели) в
            # RAM не тащим — их кэш прогреет _sync_discrete_slider при перелистывании.
            for card in self._visible_discrete_cards():
                step = getattr(card.provider, 'prefetch_step', None)
                if step is not None and step():
                    pending = True
            if not pending:
                self._discrete_prefetch_timer.stop()
                return

    # ---- Общий транспорт (Unified Transport) -------------------------------

    def _visible_video_cards(self):
        """Видимые сейчас карточки-видео (по ним идёт синхронное воспроизведение)."""
        return [c for c in self.cards.values()
                if getattr(c, 'is_video', False) and c.isVisible()]

    def _detach_driver(self):
        """Отвязывает текущий драйвер слайдера и обнуляет ссылку.

        Сигналы драйвера (позиция/длительность/статус) могли быть не подключены
        (свежий набор) — глушим RuntimeError/TypeError молча."""
        if self._driver_player is None:
            return
        try:
            self._driver_player.positionChanged.disconnect(self._on_driver_position)
            self._driver_player.durationChanged.disconnect(self._on_driver_duration)
            self._driver_player.mediaStatusChanged.disconnect(self._on_driver_status)
        except (RuntimeError, TypeError):
            pass
        self._driver_player = None

    def _sync_active_players(self):
        """Приводит набор живых плееров к текущему видимому состоянию — В ДВЕ ФАЗЫ.

        АСИНХРОННЫЙ СБРОС МЕДИА-ТРАНСПОРТА (anti GIL-deadlock). Раньше teardown
        ушедших со страницы плееров (stop + setSource(QUrl())) и подъём новых
        источников (setSource(file)) выполнялись в ОДНОМ кванте UI-потока. На
        macOS setSource синхронно ждёт QThread рендерера Qt Multimedia
        (QFFmpeg::AudioRenderer), а тот в момент смены источника пытается
        захватить GIL для финализации Shiboken-оберток — классический встречный
        тупик (UI ждёт поток, поток ждёт GIL).

        Поэтому теперь:
          ФАЗА 1 (синхронно): рвём драйвер и ОСВОБОЖДАЕМ скрытые плееры —
            запускаем деструкцию старых нативных сессий декодирования.
          ФАЗА 2 (QTimer.singleShot(0)): на следующем тике, когда event-loop Qt
            прокрутился и потоки мультимедиа успели финализировать деструкцию и
            отпустить GIL, поднимаем тяжёлые НОВЫЕ источники.
        """
        # PHANTOM SLIDER FIX (Visibility Enforcer): общий видеотранспорт виден
        # ТОЛЬКО когда в ТЕКУЩЕЙ видимой паре реально есть видео. has_videos в
        # load() — КЛАСТЕРНЫЙ флаг (на всю сессию), поэтому в СМЕШАННОМ кластере
        # (видео + gif/pdf) листание карусели на пару без видео оставляло мёртвый
        # видеоползунок. Пересчитываем на КАЖДЫЙ rebuild (метод зовётся из
        # _rebuild_view) — ДО гейта декодеров, чтобы ползунок не висел впустую.
        visible = self._visible_video_cards()
        self.slider_container.setVisible(bool(visible))

        # До отложенного _init_decoders (Spaces Jump) источники НЕ поднимаем:
        # живой AVFoundation-контекст до первого paint привязался бы к исходному
        # Space. _init_decoders снимет гейт и вызовет нас сам.
        if not self._decoders_ready:
            return

        # ФАЗА 1 — синхронный РАЗРЫВ. Драйвер снимаем сразу (его переназначит
        # фаза 2 по реально поднятым источникам), а у ушедших со страницы видео-
        # карточек ПЕРЕСОЗДАЁМ контекст через deleteLater (см. _release_card_player)
        # ВМЕСТО запрещённого stop()+setSource(QUrl()) на ЖИВОМ инстансе.
        #
        # RCA (рецидив при карусельном листании): прежний синхронный
        # card.player.stop()+setSource(QUrl()) на плеере с поднятым AVFoundation-
        # источником заставлял GUI-поток ждать QThread рендера (QFFmpeg::
        # AudioRenderer) в QThread::wait, НЕ отпуская GIL; тот же рендер
        # финализировал QAudioOutputWrapper и через PySide6 disconnectNotify →
        # PyGILState_Ensure тянулся за тем же GIL. Встречный GIL-дедлок, триггером
        # которого выступал mouseReleaseEvent кнопок Prev/Next. Снос живого
        # источника недопустим — только отложенная C++ деструкция (deleteLater).
        self._detach_driver()
        for card in self.cards.values():
            if getattr(card, 'is_video', False) and card not in visible:
                if card.player is not None and not card.player.source().isEmpty():
                    self._release_card_player(card)

        # ФАЗА 2 — отложенный подъём новых источников. Прокрутка event-loop между
        # фазами даёт потокам Qt Multimedia завершить деструкцию и отпустить GIL
        # до инициализации новых сессий декодирования.
        QTimer.singleShot(0, self._activate_visible_players)

    # Шаг лестничного подъёма источников, мс. setSource демультиплексирует/
    # пробит контейнер СИНХРОННО на GUI-потоке (QMediaPlayer привязан к нему и
    # потокобезопасно перенесён в воркер быть не может). На тяжёлом контейнере
    # (1 ГБ MP4 / длинный MOOV) этот парс — секунды; два setSource подряд в ОДНОМ
    # кванте складывают свои паузы, и окно «висит» на открытии пары. Разносим
    # подъём по отдельным тикам event-loop: первый кадр успевает отрисоваться до
    # старта демукса второго файла, а ввод между тиками обрабатывается.
    _SOURCE_STAGGER_MS = 16

    def _activate_visible_players(self):
        """ФАЗА 2 _sync_active_players: поднимает источники видимых видео-карточек.

        ЛЕСТНИЧНЫЙ ПОДЪЁМ (Staggered Source Init): setSource на тяжёлом контейнере
        блокирует GUI-поток на время демукса (см. _SOURCE_STAGGER_MS). Поэтому
        источники поднимаем НЕ пачкой в одном кванте, а по одному на тик —
        _activate_video_card на каждую карточку. Драйвер слайдера и connect его
        сигналов делаем сразу: они не требуют уже загруженного источника (сигналы
        пойдут, когда декодер прочитает контейнер).

        Между планированием и запуском виджет мог быть свёрнут (Назад/новый load)
        или страница перелистана — _activate_video_card сверяется с гейтом и заново
        считает видимый набор, чтобы не поднять источник у скрытой/снесённой карточки."""
        if not self._decoders_ready:
            return
        visible = self._visible_video_cards()

        # Поднимаем СВЕЖИЕ пустые плееры для карточек, чьи прежние инстансы были
        # сняты _release_card_player при уходе со страницы (карусельное листание).
        # Конструирование плеера дёшево — тяжёлый демукс живёт только в setSource
        # ниже, — поэтому строим синхронно ДО назначения драйвера: иначе у пары
        # «обе карточки только что показаны» driver вышел бы None и слайдер/таймкод
        # не велись бы (next(...) отфильтровал бы оба None-плеера).
        for card in visible:
            if card.player is None:
                self._build_card_player(card)

        # Драйвер слайдера снят в фазе 1 — назначаем по первому видимому плееру
        # набело. Источник у него может быть ещё не поднят (лестница ниже) — connect
        # к позиции/длительности/статусу от этого не страдает: сигналы полетят,
        # когда setSource на своём тике загрузит контейнер.
        new_driver = next((c.player for c in visible if c.player is not None), None)
        self._driver_player = new_driver
        if new_driver is not None:
            new_driver.positionChanged.connect(self._on_driver_position)
            new_driver.durationChanged.connect(self._on_driver_duration)
            new_driver.mediaStatusChanged.connect(self._on_driver_status)

        for i, card in enumerate(visible):
            if card.player is None:
                continue
            QTimer.singleShot(i * self._SOURCE_STAGGER_MS,
                              lambda c=card: self._activate_video_card(c))
        self._refresh_audio_icons()

    def _activate_video_card(self, card):
        """Поднимает источник ОДНОЙ видео-карточки и применяет её состояние.

        Один шаг лестницы _activate_visible_players (отдельный тик event-loop на
        карточку, чтобы тяжёлый демукс не складывался с соседним). Между
        планированием и запуском сессия могла свернуться (Назад/новый load) или
        карточку увели со страницы листанием — поэтому ПЕРЕпроверяем гейт декодеров
        и актуальную видимость, прежде чем трогать дорогой setSource."""
        if not self._decoders_ready:
            return
        if card not in self._visible_video_cards():
            return
        player = card.player
        if player is None:
            # Карточку вернули на страницу после карусельного листания: её прежний
            # плеер был снят deleteLater'ом в _release_card_player. Поднимаем СВЕЖИЙ
            # пустой инстанс и только к нему применяем setSource (никогда — к живому).
            # Обычно плеер уже построил _activate_visible_players; это защитный путь
            # на случай повторного release между планированием и запуском тика.
            player = self._build_card_player(card)
        if player.source().isEmpty():
            player.setSource(QUrl.fromLocalFile(os.path.abspath(card.path)))
        # Независимый микшер: переприменяем СОБСТВЕННОЕ состояние карточки
        # (mute/громкость) — источник мог быть только что поднят/пересоздан.
        if card.audio is not None:
            card.audio.setMuted(card.muted)
            card.audio.setVolume(card.volume / 100.0)
        # Текущее намерение play/pause применяем уже ПОСЛЕ подъёма источника
        # (play() до setSource на пустом плеере — no-op). BLOCK 2: делегируем —
        # синхронный play/pause на свежеподнятом тяжёлом источнике встаёт на тот же
        # мьютекс демукса, что и setPosition.
        if self._is_playing:
            self._async_play(player)
        else:
            self._async_pause(player)

    # ─── Safe Context Recreation per card (паритет с video_player.change_source_safe) ─
    # Сырые плееры карточек (frame.player/frame.audio) НИКОГДА не сносятся
    # in-place: ни stop(), ни setSource(QUrl()) на живом инстансе (встречный
    # GIL-дедлок ~QAudioOutputWrapper). Скрытую листанием карточку освобождаем
    # отложенной C++ деструкцией (_release_card_player → deleteLater), а вернувшейся
    # на страницу поднимаем АБСОЛЮТНО НОВЫЕ player+audio (_build_card_player), и
    # только к ним применяем setSource. Это та же модель, что в одиночном плеере.
    def _release_card_player(self, card):
        """Deadlock-safe выгрузка сырого плеера ОДНОЙ карточки (скрытой листанием).

        КАТЕГОРИЧЕСКИ без stop()/setSource(QUrl()) на живом инстансе. Глушим эхо
        сигналов, отвязываем синк (setVideoSink(None) ДО сноса — иначе отложенный
        деструктор плеера на следующем тике сбросил бы VideoSinkWidget карточки,
        который к тому моменту мог бы держать уже пересозданный плеер → чёрный
        кадр), затем deleteLater() и плеера, и аудио: их C++ деструкторы (а с ними
        снос AVFoundation/ffmpeg-графа) отработают в чистом C++-проходе event-loop,
        ВНЕ GIL-удерживающего кванта GUI-потока. card.player/card.audio обнуляем —
        повторный показ карточки поднимет свежий инстанс через _build_card_player."""
        player, audio = card.player, card.audio
        if player is None:
            return
        # Эхо positionChanged/mediaStatusChanged между этим квантом и реальной
        # деструкцией не должно дёрнуть слоты уже сносимого транспорта.
        player.blockSignals(True)
        if audio is not None:
            audio.blockSignals(True)
        # Отвязываем синк ДО deleteLater (см. RCA выше про чёрный кадр).
        try:
            player.setVideoSink(None)
        except (RuntimeError, TypeError):
            pass
        player.deleteLater()
        if audio is not None:
            audio.deleteLater()
        card.player = None
        card.audio = None

    def _build_card_player(self, card):
        """Строит АБСОЛЮТНО НОВЫЕ QMediaPlayer+QAudioOutput для карточки и привязывает
        их к её собственному VideoSinkWidget (card.lbl_img). Источник НЕ ставим —
        его поднимет _activate_video_card на своём тике лестницы (Staggered Source
        Init), причём на этом ЧИСТОМ пустом инстансе setSource не сносит живой
        рендер-граф и не встаёт в QThread::wait под GIL.

        Зеркалит видео-ветку _create_card, но вызывается ПОВТОРНО — каждый раз,
        когда скрытую листанием карточку снова выводят на страницу (её прежний
        плеер снят _release_card_player'ом). Подписку sink.videoSizeChanged НЕ
        трогаем: она висит на самом VideoSinkWidget, который переживает
        пересоздание плеера, поэтому реактивная ориентация продолжает работать."""
        audio = QAudioOutput(card)
        audio.setVolume(card.volume / 100.0)
        audio.setMuted(card.muted)
        player = QMediaPlayer(card)
        player.setAudioOutput(audio)
        player.setVideoSink(card.lbl_img.videoSink())
        self._set_infinite_loop(player)
        card.player = player
        card.audio = audio
        return player

    # ─── Async Command Delegation (BLOCK 2, паритет с video_player) ──────────
    # Нативные play/pause/setPosition на тяжёлом контейнере встают на внутренний
    # мьютекс libffmpegmediaplugin (QBasicMutex::lockInternal) — синхронный вызов
    # ИЗ ОБРАБОТЧИКА события морозит GUI-поток (стекшот: JumpSlider.mousePressEvent
    # → setPosition → lockInternal). Поэтому КАЖДУЮ нативную команду делегируем в
    # следующий квант event-loop: текущий обработчик возвращается мгновенно, а
    # нативный вызов исполняется на чистом стеке. Карточка/плеер могли быть снесены
    # (Назад/новый load/листание карусели) между планированием и запуском —
    # _safe_player_call глушит RuntimeError уже удалённого C++ объекта.
    def _safe_player_call(self, player, method, *args):
        try:
            getattr(player, method)(*args)
        except RuntimeError:
            pass

    def _async_play(self, player):
        # player может быть None в под-тиковом окне: карточка только что выведена
        # на страницу листанием, её свежий плеер ещё не построен фазой 2.
        if player is None:
            return
        QTimer.singleShot(0, lambda p=player: self._safe_player_call(p, "play"))

    def _async_pause(self, player):
        if player is None:
            return
        QTimer.singleShot(0, lambda p=player: self._safe_player_call(p, "pause"))

    def _async_set_position(self, player, pos):
        if player is None:
            return
        QTimer.singleShot(0, lambda p=player, x=int(pos): self._safe_player_call(p, "setPosition", x))

    def _toggle_play(self):
        # OPTIMISTIC UI: переворачиваем намерение и рисуем иконку НЕМЕДЛЕННО, а
        # нативные play/pause делегируем в следующий квант (BLOCK 2) — клик не ждёт
        # мьютекс libffmpegmediaplugin.
        self._is_playing = not self._is_playing
        self._set_play_icon()
        for card in self._visible_video_cards():
            if self._is_playing:
                self._async_play(card.player)
            else:
                self._async_pause(card.player)

    # Затвор дросселирования живого скраба: ~33 мс ≈ 30 Гц. Чаще — кадр
    # пропускаем. Математика: воркер коалесцирует запросы по пути (новый pct
    # затирает старый в self.requests), поэтому при >30 событий/с лишние
    # sliderMoved всё равно схлопнулись бы в один кадр — но затвор отсекает их
    # ДО обращения к воркеру/декодеру, не плодя мусорных wakeup'ов треда.
    _SEEK_MIN_INTERVAL = 0.033

    def _on_scrub_start(self):
        """sliderPressed: пользователь зажал ползунок (начало драг-скраба).

        Pause-on-Drag: физически паузим все видимые плееры — снимаем нагрузку с
        аппаратного декодера и исключаем «гонку» живого воспроизведения с
        кадрами скраба. Намерение воспроизведения (_is_playing) НЕ трогаем: это
        и есть запомненное состояние, по которому _on_scrub_end решит,
        возобновлять ли проигрывание. Сбрасываем затвор, чтобы первый кадр
        скраба прошёл мгновенно."""
        self._last_seek_ts = 0.0
        # BLOCK 2: Pause-on-Drag тоже делегируем — на тяжёлом контейнере pause()
        # встаёт на мьютекс декодера и заморозил бы нажатие ползунка.
        for card in self._visible_video_cards():
            self._async_pause(card.player)

    def _scrub_preview(self, value):
        """sliderMoved: живой скраб в реальном времени (Throttled Real-Time).

        На macOS QMediaPlayer на ПАУЗЕ не перерисовывает кадр по setPosition
        (ровно как в одиночном плеере, см. video_player._on_slider_moved),
        поэтому домотанный кадр тащим тем же cv2-конвейером (CompareVideoWorker)
        и рисуем его как статичный preview в VideoSinkWidget каждой видимой
        карточки. Это даёт обновление кадров «на лету» под мышью, а не по
        отпусканию. Реальную позицию плееров выставит _on_scrub_end по релизу."""
        now = time.monotonic()
        if now - self._last_seek_ts < self._SEEK_MIN_INTERVAL:
            return
        self._last_seek_ts = now

        # Signal Feedback Gating: глушим сигналы слайдера на время трансляции,
        # чтобы эхо positionChanged не дёргало ползунок под мышью.
        self.slider.blockSignals(True)
        try:
            pct = max(0.0, min(100.0, int(value) / 10.0))   # 0..1000‰ → 0..100%
            paths = [c.path for c in self._visible_video_cards()]
            if paths and self.worker is not None:
                self.worker.request_frames(paths, pct)
            # Таймкод ведём по дроби прогресса (плеер на паузе) — по той же
            # МАКСИМАЛЬНОЙ длительности кластера, что и не-скраб лейбл (консистентно).
            mdur = self._max_visible_duration()
            if mdur > 0:
                self._update_time_label(int(value / 1000.0 * mdur), mdur)
        finally:
            self.slider.blockSignals(False)

    def _on_scrub_end(self):
        """sliderReleased: отпускание ползунка ИЛИ клик-прыжок JumpSlider.

        Выставляем РЕАЛЬНУЮ позицию плееров (force, в обход троттла) — чтобы
        последующий Play стартовал ровно отсюда. Затем, если до скраба транспорт
        играл, возвращаем плееры к воспроизведению (живые кадры сами вытеснят
        cv2-превью). Клик-прыжок JumpSlider не шлёт sliderPressed, поэтому seek
        здесь обязателен и для него.

        ЗАТВОР _seek_in_flight: пока предыдущий отложенный setPosition НЕ приземлился
        (нет driver positionChanged), новый коммит НЕ ставим — именно СТОПКА
        setPosition (по одному на клик/релиз), сериализованная на мьютексе декодера,
        давала фриз. Затвор снимается в _on_driver_position или по фолбэку."""
        if self._seek_in_flight:
            return
        self._seek_in_flight = True
        self._seek_permille(self.slider.value(), force=True)
        if self._is_playing:
            for card in self._visible_video_cards():
                self._async_play(card.player)
        # Фолбэк снятия затвора: на паузе driver positionChanged может не прийти.
        QTimer.singleShot(300, self._clear_seek_lock)

    def _clear_seek_lock(self):
        self._seek_in_flight = False

    def _seek_permille(self, value, force=False):
        """Реальная перемотка видимых плееров на дробь прогресса (0..1000‰).

        Дробь, а не абсолютные мс: ролики разной длины встают на одинаковый %
        своей длительности (frame-in-frame). Вызывается из _on_scrub_end по
        релизу; живой скраб идёт через _scrub_preview (cv2), а не здесь."""
        if not force:
            now = time.monotonic()
            if now - self._last_seek_ts < self._SEEK_MIN_INTERVAL:
                return
            self._last_seek_ts = now

        frac = max(0, min(1000, int(value))) / 1000.0
        self.slider.blockSignals(True)
        try:
            for card in self._visible_video_cards():
                if card.player is None:
                    continue   # свежий плеер ещё не построен (под-тиковое окно)
                # duration() — дешёвый геттер (кэш, не лезет в мьютекс декодера),
                # звать синхронно безопасно. А вот setPosition тяжёлого контейнера
                # встаёт на QBasicMutex::lockInternal демукса — именно он в стекшоте
                # морозил GUI прямо в JumpSlider.mousePressEvent. BLOCK 2: делегируем
                # коммит в следующий квант — обработчик клика возвращается мгновенно.
                dur = card.player.duration()
                if dur > 0:
                    self._async_set_position(card.player, int(frac * dur))
        finally:
            self.slider.blockSignals(False)

    def _on_driver_position(self, pos):
        # Seek приземлился у драйвера — снимаем затвор конвейера (см. _on_scrub_end).
        if self._seek_in_flight:
            self._clear_seek_lock()
        # ПОЗИЦИЯ слайдера — по дроби прогресса ДРАЙВЕРА (0..1000‰, frame-in-frame).
        drv_dur = self._driver_player.duration() if self._driver_player else 0
        if drv_dur > 0 and not self.slider.isSliderDown():
            self.slider.blockSignals(True)
            self.slider.setValue(int(pos / drv_dur * 1000))
            self.slider.blockSignals(False)
        # ЛЕЙБЛ — хронометраж по МАКСИМАЛЬНОЙ длительности видимого кластера
        # (ролики разной длины: показываем шкалу самого длинного, не «обрезанную»
        # драйвером). Формат уже MM:SS / MM:SS (см. _fmt) — процентов тут нет.
        self._update_time_label(pos, self._max_visible_duration() or drv_dur)

    def _on_driver_duration(self, dur):
        pos = self._driver_player.position() if self._driver_player else 0
        self._update_time_label(pos, self._max_visible_duration() or dur)

    def _max_visible_duration(self):
        """Макс. длительность среди ВИДИМЫХ видео-плееров (источник шкалы лейбла).
        duration() — дешёвый кэш-геттер, не лезет в мьютекс декодера."""
        return max((c.player.duration() for c in self._visible_video_cards()
                    if c.player is not None), default=0)

    def _on_driver_status(self, status):
        # Конец ролика у драйвера: возвращаем ВЕСЬ видимый набор в начало, чтобы
        # после лупа кадры снова шли синхронно (плееры лупятся независимо и со
        # временем расходятся — этот ресинк выравнивает их по драйверу).
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            # BLOCK 2: ресинк по лупу тоже через делегаты — setPosition(0)/play()
            # на тяжёлом наборе иначе встали бы на мьютекс демукса в слоте сигнала.
            for card in self._visible_video_cards():
                self._async_set_position(card.player, 0)
                if self._is_playing:
                    self._async_play(card.player)

    def _update_time_label(self, pos, dur):
        self.lbl_time.setText(f"{self._fmt(pos)} / {self._fmt(dur)}")

    @staticmethod
    def _fmt(ms):
        s = max(0, int(ms)) // 1000
        return f"{s // 60:02d}:{s % 60:02d}"

    def _set_play_icon(self):
        glyph = "pause" if self._is_playing else "play"
        self.btn_play.setIcon(ThemeManager.make_icon(glyph, self._icon_color()))

    # ---- Независимый аудио-микшер на карточку (Independent Audio Matrix) ----

    def _toggle_card_mute(self, card):
        """Клик по mute-кнопке карточки: переключает ТОЛЬКО её звук.

        Никакого эксклюзива — состояния других карточек не трогаются, поэтому
        возможны полный мут всех, два трека одновременно и любые комбинации."""
        if card.audio is None:
            return
        card.muted = not card.muted
        card.audio.setMuted(card.muted)
        self._refresh_audio_icon(card)

    def _change_card_volume(self, card, value):
        """Слайдер громкости карточки → её собственный QAudioOutput.setVolume.

        Поднятие уровня с нуля автоматически снимает mute (как в одиночном
        плеере), чтобы пользователю не приходилось жать две кнопки."""
        card.volume = value
        if card.audio is None:
            return
        card.audio.setVolume(value / 100.0)
        if value > 0 and card.muted:
            card.muted = False
            card.audio.setMuted(False)
        self._refresh_audio_icon(card)

    def _refresh_audio_icon(self, card):
        """Перерисовывает глиф volume/volume_muted ОДНОЙ карточки под её
        собственное состояние микшера (mute + громкость)."""
        btn = getattr(card, 'audio_btn', None)
        if btn is None:
            return
        audible = card.audio is not None and not card.muted and card.volume > 0
        glyph = "volume" if audible else "volume_muted"
        btn.setIcon(ThemeManager.make_icon(glyph, self._icon_color()))

    def _refresh_audio_icons(self):
        """Обновляет глифы звука на всех видео-карточках (после смены темы/набора)."""
        for card in self.cards.values():
            if getattr(card, 'is_video', False):
                self._refresh_audio_icon(card)

    def _icon_color(self) -> str:
        return "#F5F5F7" if ThemeManager.colors() is ThemeManager.DARK else "#1D1D1F"

    def _on_frame_ready(self, path, qimg):
        # cv2-превью: для живого плеера кладём как статичный кадр (исчезнет с
        # первым valid-кадром декодера), для гиф/картинок — как раньше pixmap.
        card = self.cards.get(path)
        if card is None or qimg is None or qimg.isNull():
            return
        widget = card.lbl_img
        if hasattr(widget, 'set_preview_image'):
            widget.set_preview_image(qimg)
            # cv2-превью несёт реальные (с учётом rotation) размеры кадра и
            # масштабируется воркером с сохранением пропорций — питаем им
            # реактивную ориентацию. На macOS плеер на паузе размер в синк не
            # отдаёт, поэтому до первого Play это единственный источник AR.
            self._on_video_size_changed(path, qimg.size())
        else:
            widget.setPixmap(QPixmap.fromImage(qimg))

    def _cleanup(self):
        for card in self.cards.values():
            if hasattr(card.lbl_img, 'clear_view'):
                card.lbl_img.clear_view()

    def _detach_worker(self):
        if self.worker is None:
            return
        if self.worker.isRunning():
            self.worker.setParent(None)
            MultiCompareWidget._orphaned_workers.append(self.worker)
            self.worker.finished.connect(lambda w=self.worker: MultiCompareWidget._orphaned_workers.remove(w) if w in MultiCompareWidget._orphaned_workers else None)
            self.worker.finished.connect(self.worker.deleteLater)
            # CompareVideoWorker МЕЖДУ запросами кадров спит в cond.wait(). Голый
            # is_running=False его НЕ будит, а quit() здесь no-op: у воркера своя
            # while-петля, а не QThread event loop (quit() рассчитан на exec()).
            # Без notify_all() ПРОСТАИВАЮЩИЙ воркер навсегда виснет в cond.wait():
            #   • QThread.finished НЕ эмитится → он не снимается с _orphaned_workers
            #     и не уходит в deleteLater (вечный orphan-поток, State Leak);
            #   • run() не доходит до finally, где release()'ятся нативные
            #     VideoCapture → утечка до 12 AVFoundation/ffmpeg-дескрипторов
            #     на КАЖДУЮ закрытую сессию сравнения (Native Trap).
            # А teardown зовётся именно когда воркер уже отдал стартовые превью и
            # спит — т.е. дефект срабатывал на типичном закрытии «Назад»/«Применить».
            # Будим под тем же локом, что и штатный stop(), но БЕЗ блокирующего
            # wait() (воркер может стоять в нативной cv2-пробе — GUI ждать не должен;
            # ради этого здесь и orphan-модель, а не synchronous stop()).
            self.worker.is_running = False
            with self.worker.cond:
                self.worker.cond.notify_all()
            self.worker.quit()
        else:
            self.worker.deleteLater()

    def _go_back(self):
        # «Назад»/Esc — глушим воркер/гифки сессии ДО выхода (чтобы не оставить
        # живых нативных потоков), затем сигналим хосту вернуться на index 0.
        self._teardown_session()
        self.compare_cancelled.emit()

    def _confirm(self):
        # «Применить»: фиксируем выбор в self.files_to_delete/self.delete_hard
        # (их прочитает хост по сигналу), глушим сессию и сигналим подтверждение.
        self.files_to_delete = [c.path for c in self.cards.values() if c.checkbox.isChecked()]
        self.delete_hard = self.rb_hard.isChecked()
        self._teardown_session()
        self.compare_confirmed.emit()
