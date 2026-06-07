# ============================================================
# MODULE: ui/views/multi_compare.py
# ============================================================
import os
import cv2
import time
from collections import OrderedDict
from PySide6.QtWidgets import (QVBoxLayout, QLabel,
                               QPushButton, QWidget, QCheckBox, QHBoxLayout, QFrame, QRadioButton, QSizePolicy)
from PySide6.QtGui import QPixmap, QImageReader, QImage, QPainter, QMovie, QPalette, QColor
from PySide6.QtCore import Qt, Signal, QTimer

from ui.components.video_player import JumpSlider
from ui.workers import CompareVideoWorker
from utils.i18n import translator
from utils.theme_manager import ThemeManager
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
        self._carousel_index = 0
        self._page_index = 0
        self.files_to_delete = []
        self.delete_hard = False
        # Флаг отложенной инициализации медиа: load() лишь помечает набор как
        # ожидающий, а тяжёлые декодеры (AVFoundation/Metal/QMovie) поднимаются
        # позже — из showEvent, когда страница уже стала активным слоем стека.
        self._decoders_pending = False

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

        # Слайдер кадра видео создаётся ВСЕГДА (часть постоянного каркаса), но
        # прячется load()'ом, если в наборе нет видео.
        self.slider_container = QFrame()
        self.slider_container.setFixedHeight(50)
        self.slider_container.setStyleSheet("border-radius: 8px;")
        slider_layout = QHBoxLayout(self.slider_container)
        self.slider = JumpSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(25)
        self.slider.sliderReleased.connect(self._execute_sync_video_frames)
        slider_layout.addWidget(QLabel("⏱️"))
        slider_layout.addWidget(self.slider)
        layout.addWidget(self.slider_container)

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
        self._carousel_index = 0
        self._page_index = 0
        self.rb_trash.setChecked(True)
        self.slider_container.setVisible(self.has_videos)
        self.slider.blockSignals(True)
        self.slider.setValue(25)
        self.slider.blockSignals(False)

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
        """Глушит воркер/гифки и уничтожает карточки предыдущей сессии."""
        # Снимаем ожидание отложенной инициализации: если сессию сворачивают
        # (Назад/Применить или новый load) до того, как showEvent поднял медиа,
        # запланированный _maybe_init_decoders станет no-op по сброшенному флагу.
        self._decoders_pending = False
        if self.worker is not None:
            self._detach_worker()
            self.worker = None
        self._cleanup()
        while self.view_layout.count():
            item = self.view_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
        for card in self.cards.values():
            # hide() ДО setParent(None): орфанинг видимого виджета на macOS
            # выкидывает top-level NSWindow на активный Space (Spaces Jump).
            card.hide()
            card.setParent(None)
            card.deleteLater()
        self.cards = {}

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
            for path, _ in self.file_entries:
                card = self.cards[path]
                if path in page_paths:
                    self.view_layout.addWidget(card, stretch=1)
                    card.show()
                else:
                    card.hide()

        self._update_nav()

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

        cb = QCheckBox(f"🗑  {translator.tr('cmp_delete')}")
        cb.setStyleSheet("font-weight: bold; color: #DA3633;")
        # После отметки «в корзину» автоматически листаем карусель на следующий
        # кадр — пользователю не нужно перелистывать вручную после выбора.
        cb.toggled.connect(lambda checked, p=path: self._on_delete_toggled(checked, p))

        hp_layout.addWidget(info, stretch=1)
        # Pin + Delete pinned to the TOP-RIGHT corner of the card header.
        hp_layout.addWidget(pin_btn, alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        hp_layout.addWidget(cb, alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        l.addWidget(header_panel)

        lbl_img = GridImageLabel()
        # Фон кадра уже задан палитрой 'surface' внутри GridImageLabel; QSS с
        # 'transparent' здесь не ставим, иначе он перебил бы палитру и вернул
        # чёрную подложку под видео/гиф.
        l.addWidget(lbl_img, stretch=1)

        frame.lbl_img = lbl_img
        frame.checkbox = cb
        frame.pin_btn = pin_btn
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
        if self.worker is None:
            return
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
        if self.worker is None:
            return
        if self.worker.isRunning():
            self.worker.setParent(None)
            MultiCompareWidget._orphaned_workers.append(self.worker)
            self.worker.finished.connect(lambda w=self.worker: MultiCompareWidget._orphaned_workers.remove(w) if w in MultiCompareWidget._orphaned_workers else None)
            self.worker.finished.connect(self.worker.deleteLater)
            self.worker.is_running = False
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
