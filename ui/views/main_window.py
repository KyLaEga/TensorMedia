# ============================================================
# MODULE: ui/views/main_window.py
# ============================================================
import os
from pathlib import Path
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QLabel, QProgressBar, QComboBox, QLineEdit, QRadioButton, 
                             QButtonGroup, QHeaderView, QSplitter, QScrollArea, 
                             QApplication, QCheckBox, QFrame, QStackedWidget, QGridLayout, 
                             QSizePolicy, QSpacerItem, QAbstractItemView, QMessageBox,
                             QStyle, QStyleOptionComboBox, QStylePainter)
from PySide6.QtCore import Qt, QSettings, Signal, QSortFilterProxyModel, QModelIndex, QSize
from PySide6.QtGui import (QKeySequence, QAction, QFontMetrics,
                           QIntValidator, QIcon)

from utils.theme_manager import ThemeManager
from utils.i18n import translator
from utils.logger import auditor
from core.ml.faiss_manager import FaissManager

from ui.workers import PurgeWorker
from ui.components.video_player import JumpSlider
from ui.components.media_tree import MediaTreeView, LazyClusterModel
from ui.components.image_label import ScalableImageLabel
from ui.views.multi_compare import MultiCompareWidget

class ElidingLabel(QLabel):
    """QLabel that truncates with an ellipsis instead of clipping or forcing
    its parent wider. Stores the full string and re-elides on every resize, so
    it has a *defined* shrink behaviour (unlike setWordWrap, which converts
    horizontal pressure into vertical overlap). SizePolicy is Ignored on the
    horizontal axis so the layout is free to take it below its sizeHint."""

    def __init__(self, text="", parent=None, mode=Qt.TextElideMode.ElideRight):
        super().__init__(text, parent)
        self._mode = mode
        self._full_text = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setText(self, text):
        self._full_text = text or ""
        self._apply_elision()

    def text(self):
        return self._full_text

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_elision()

    def _apply_elision(self):
        fm = QFontMetrics(self.font())
        # contentsRect() respects margins; fall back to width() if unset.
        avail = max(0, self.contentsRect().width() or self.width())
        super().setText(fm.elidedText(self._full_text, self._mode, avail))
        self.setToolTip(self._full_text if super().text() != self._full_text else "")


class ThresholdEdit(QLineEdit):
    """Пин порога сходства: QLineEdit, мимикрирующий под текстовую метку.

    Заменяет статичный QLabel "88%". Точное значение [0-100] вводится с
    клавиатуры (строгий QIntValidator); НИКАКОЙ кнопки «Применить» — фиксация
    идёт по сигнальной архитектуре фокуса: editingFinished (Enter или потеря
    фокуса) → value_committed(int) → пересчёт FAISS-выборки в контроллере.
    Вне режима редактирования показывает "NN%", в фокусе — голое число."""

    value_committed = Signal(int)

    def __init__(self, value: int = 88, parent=None):
        super().__init__(parent)
        self._value = max(0, min(100, int(value)))
        self.setValidator(QIntValidator(0, 100, self))
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        # Визуально это всё ещё «метка»: без рамки и фона, цвет — из QLabel-палитры.
        self.setFrame(False)
        self.setStyleSheet(
            "QLineEdit { background: transparent; border: none; padding: 0; }"
            "QLineEdit:focus { background: transparent; border: none; }"
        )
        self.editingFinished.connect(self._commit)
        self._refresh_display()

    def value(self) -> int:
        return self._value

    def set_value(self, value: int):
        """Программная установка (ползунок/пресеты): БЕЗ эмиссии value_committed,
        чтобы не зациклить recluster."""
        self._value = max(0, min(100, int(value)))
        if not self.hasFocus():
            self._refresh_display()
        else:
            self.setText(str(self._value))

    def _refresh_display(self):
        self.setText(f"{self._value}%")

    def focusInEvent(self, event):
        # В фокусе остаются только цифры — валидатор работает по чистому числу.
        self.setText(str(self._value))
        super().focusInEvent(event)
        self.selectAll()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self._refresh_display()

    def _commit(self):
        text = self.text().rstrip("%").strip()
        try:
            value = max(0, min(100, int(text)))
        except ValueError:
            self._refresh_display()
            return
        changed = value != self._value
        self._value = value
        self._refresh_display()
        self.clearFocus()
        if changed:
            self.value_committed.emit(value)


class FluidComboBox(QComboBox):
    """A combo whose CLOSED body is free to shrink with its container, while
    its DROPDOWN popup always opens wide enough to show the full text of the
    widest item — overflowing neighbouring widgets instead of forcing the whole
    row (and the sidebar) wider.

    Stock QComboBox keeps these two widths coupled, which is what produced the
    competing requirements before (a hard sidebar floor just so the closed box
    could paint its text). Here they are decoupled:

      * closed box  -> Ignored horizontal policy + AdjustToMinimumContentsLength:
                       the layout may take it below its content width and the
                       visible label is ELIDED with "…" (no mid-glyph clipping).
      * popup view  -> minimum width recomputed on every showPopup() from the
                       widest item, so the list itself never clips.

    Height stays on the design-system rhythm via setFixedHeight() at the call
    site (a fixed HEIGHT does not break horizontal fluidity)."""

    _POPUP_PADDING = 44  # item h-padding (20) + frame + scrollbar headroom

    def __init__(self, parent=None):
        super().__init__(parent)
        # Body yields to the layout; only the popup honours content width.
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.setMinimumContentsLength(0)

    def _widest_item_px(self) -> int:
        fm = self.view().fontMetrics()
        widest = 0
        for i in range(self.count()):
            widest = max(widest, fm.horizontalAdvance(self.itemText(i)))
        return widest

    def showPopup(self):
        # ОТВЯЗКА POPUP от свёрнутого тела. Только setMinimumWidth(view) ненадёжно:
        # к моменту showPopup геометрия контейнера (QComboBoxPrivateContainer) уже
        # вычислена по ширине свёрнутого селектора, поэтому список оставался зажат
        # шириной QComboBox. Чиним в два такта:
        #   1) до показа задаём минимум самому view (виджету списка);
        #   2) после показа — ПРИНУДИТЕЛЬНО расширяем окно-контейнер popup до
        #      ширины самого широкого пункта, игнорируя ширину тела.
        target = self._widest_item_px() + self._POPUP_PADDING
        self.view().setMinimumWidth(target)
        super().showPopup()
        container = self.view().window()  # QComboBoxPrivateContainer (top-level)
        if container is not None and container.width() < target:
            container.setMinimumWidth(target)
            container.resize(target, container.height())

    def paintEvent(self, event):
        # Replicate QComboBox::paintEvent but with an ELIDED label so a shrunk
        # box reads "Визуаль…" instead of clipping a glyph in half.
        painter = QStylePainter(self)
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        field = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, opt,
            QStyle.SubControl.SC_ComboBoxEditField, self,
        )
        opt.currentText = self.fontMetrics().elidedText(
            opt.currentText, Qt.TextElideMode.ElideRight, field.width()
        )
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, opt)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, opt)


class ArbitrageSortFilterProxyModel(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.filter_type = 0 
        self.search_text = ""

    def set_filters(self, f_type: int, text: str):
        text_lower = text.lower()
        if self.filter_type == f_type and self.search_text == text_lower:
            return
            
        self.filter_type = f_type
        self.search_text = text_lower
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        
        if not source_parent.isValid():
            group_index = model.index(source_row, 0, source_parent)
            
            if self.search_text:
                group_item = model.itemFromIndex(group_index)
                if group_item:
                    group_text = str(group_item.data(0)).lower()
                    if self.search_text in group_text:
                        return True

            for i in range(model.rowCount(group_index)):
                if self.filterAcceptsRow(i, group_index):
                    return True
            return False

        index = model.index(source_row, 0, source_parent)
        data = model.data(index, Qt.ItemDataRole.UserRole)
        
        if not data or not isinstance(data, dict):
            return True

        ext = Path(data.get('path', '')).suffix.lower()
        name = Path(data.get('path', '')).name.lower()
        ocr = data.get('ocr', '').lower()

        if self.filter_type == 1 and ext not in {'.jpg', '.png', '.webp', '.bmp', '.heic', '.jpeg'}: return False
        if self.filter_type == 2 and ext not in {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'}: return False
        if self.filter_type == 3 and ext not in {'.pdf', '.cbz', '.gif'}: return False

        if self.search_text and self.search_text not in name and self.search_text not in ocr:
            return False

        return True

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        model = self.sourceModel()
        col = left.column()

        if not left.parent().isValid() or not right.parent().isValid():
            l_child = model.index(0, 0, left)
            r_child = model.index(0, 0, right)
            if l_child.isValid() and r_child.isValid():
                return self.lessThan(l_child, r_child)
            return left.row() < right.row() 

        left_data = model.data(left, Qt.ItemDataRole.UserRole)
        right_data = model.data(right, Qt.ItemDataRole.UserRole)

        if not isinstance(left_data, dict) or not isinstance(right_data, dict):
            return super().lessThan(left, right)

        try:
            if col == 0: 
                return left_data.get('path', '') < right_data.get('path', '')
            elif col == 1: 
                return Path(left_data.get('path', '')).suffix < Path(right_data.get('path', '')).suffix
            elif col == 2: 
                return float(left_data.get('similarity', 0.0)) < float(right_data.get('similarity', 0.0))
            elif col == 3: 
                return int(left_data.get('size', 0)) < int(right_data.get('size', 0))
            elif col == 4: 
                def get_area(r):
                    try: 
                        w, h = map(int, str(r).split('x'))
                        return w * h
                    except (ValueError, TypeError): 
                        return 0
                return get_area(left_data.get('res', '')) < get_area(right_data.get('res', ''))
            elif col == 5:
                return float(left_data.get('mtime', 0.0)) < float(right_data.get('mtime', 0.0))
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            # Fall back to the base comparator on malformed row data; log at
            # debug so the silent swallow is at least diagnosable.
            auditor.debug(f"Sort comparator fallback (col {col}): {e}", exc_info=True)

        return super().lessThan(left, right)


class MainWindow(QMainWindow):
    directory_dropped = Signal(str)
    window_closed = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Tensor Media Arbitrage v1.0")
        self.resize(1450, 900)
        self.settings = QSettings("TensorMedia", "Arbitrage")
        
        self._setup_ui()
        self._setup_menubar()
        ThemeManager.apply_modern_dark(QApplication.instance())
        self._restore_state()
        self.setAcceptDrops(True)

        translator.language_changed.connect(self._retranslate_ui)
        self._retranslate_ui()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'tree'):
            self.tree._trigger_stretch()
        
        if hasattr(self, 'scan_controls_layout'):
            self._update_scan_controls_mode()

        margin = max(4, int(self.width() * 0.01))
        if hasattr(self, 'single_prev_layout'):
            self.single_prev_layout.setContentsMargins(margin, margin, margin, margin)
            self.single_prev_layout.setSpacing(margin)
        if hasattr(self, 'multi_grid'):
            self.multi_grid.setContentsMargins(margin, margin, margin, margin)
            self.multi_grid.setSpacing(margin)

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            self.directory_dropped.emit(urls[0].toLocalFile())

    def closeEvent(self, event):
        # Cmd-Q / закрытие окна. ПОРЯДОК ВАЖЕН:
        # 1) window_closed.emit() синхронно (DirectConnection в GUI-потоке)
        #    прогоняет MainController._on_window_closed и MLOrchestrator.stop_all —
        #    они глушат QThread-воркеры и КОРРЕКТНО гасят multiprocessing-пул
        #    (terminate/join), освобождая семафоры; иначе в консоль падает
        #    'leaked semaphore objects'.
        # 2) ТОЛЬКО ПОСЛЕ этого валим процесс os._exit(0) — немедленный выход без
        #    C++-деструкторов живого Qt-дерева (иначе Abort trap: 6 / SIGABRT на
        #    висящих фоновых пулах и потоках).
        #
        # ВНИМАНИЕ: здесь НЕЛЬЗЯ слать SIGKILL. Сигнал 9 заставляет zsh печатать
        # "zsh: killed" и прилетает АСИНХРОННО — он обгоняет resource_tracker и
        # оставляет 'leaked semaphore objects'. Семафоры пула/Value снимаются
        # синхронно внутри stop_all (terminate/join + gc.collect → sem_unlink),
        # после чего процесс штатно завершается os._exit(0): код возврата 0 (нет
        # "zsh: killed") и никаких C++-деструкторов (нет SIGABRT).
        # STATE PERSISTENCE. saveGeometry() кодирует позицию/размер/экран/режим
        # окна; saveState() — пропорции сплиттеров. КРИТИЧНО вызвать sync() ЯВНО:
        # ниже мы валим процесс os._exit(0), который пропускает Qt-деструкторы и
        # штатный flush QSettings в конце event-loop. Без sync() значения остаются
        # только в памяти процесса и теряются — именно поэтому окно «не помнило»
        # пропорции между сессиями, хотя setValue вызывался.
        self.settings.setValue('geometry', self.saveGeometry())
        self.settings.setValue('master_splitter', self.master_splitter.saveState())
        self.settings.setValue('inner_splitter', self.inner_splitter.saveState())
        self.settings.sync()  # синхронный сброс на диск ДО os._exit
        self.window_closed.emit()  # Синхронно вызывает stop_all и освобождает семафоры
        os._exit(0)

    def _switch_tab(self, index: int):
        self.tabs.setCurrentIndex(index)
        self.btn_tab_scan.setChecked(index == 0)
        self.btn_tab_analytics.setChecked(index == 1)

    def _setup_ui(self):
        # Корневой QStackedWidget центрального виджета:
        #   index 0 — основной интерфейс (дерево + инспектор);
        #   index 1 — встроенная страница сравнения (MultiCompareWidget).
        # Сравнение — НЕ диалог: это обычная страница стека, переключаемая через
        # setCurrentIndex(1). Так macOS не создаёт второго NSWindow и не уводит
        # приложение на другой Space ("Spaces Jump").
        self.root_stack = QStackedWidget()
        self.setCentralWidget(self.root_stack)

        central = QWidget()
        self.root_stack.addWidget(central)  # index 0

        # ПОСТОЯННАЯ страница сравнения создаётся один раз при старте и живёт
        # под index 1 стека (отказ от паттерна Destroy & Rebuild). Резкое
        # удаление/пересоздание виджета и скрытие QVideoWidget в полноэкранном
        # режиме вызывало панику macOS WindowServer ("Spaces Jump"). Теперь
        # виджет переиспользуется: контроллер вызывает load(file_paths) перед
        # каждым показом, а тяжёлые медиа-декодеры поднимаются отложенно из
        # showEvent — уже в активном Space. См. MultiCompareWidget.
        self.compare_widget = MultiCompareWidget(self)
        self.root_stack.addWidget(self.compare_widget)  # index 1
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.master_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.master_splitter.setHandleWidth(1)
        main_layout.addWidget(self.master_splitter)

        self.sidebar_widget = QWidget()
        self.sidebar_widget.setObjectName("sidebar")
        # HARD minimum width — НЕ fluid-подсказка. Это абсолютный физический пол,
        # который QSplitter не имеет права нарушить ни перетаскиванием ручки, ни
        # перераспределением места под давлением правой панели (см. RCA).
        # Связующее ограничение — ряд движка ("Режим:" + "Визуальный (SigLIP)") в
        # cal_card:
        #   label 64 + spacing 6 + combo.sizeHint 190 = 260 (содержимое ряда)
        #   + поля cal_card (14+14)               =  28
        #   + поля sidebar_layout (12+12)         =  24
        #   ----------------------------------------------------
        #   = 312px ровно; SIDEBAR_MIN_WIDTH=350 даёт ~38px запаса. Этого хватает,
        #   чтобы поглотить даже непрозрачный 17px-скроллбар (Win/Linux) БЕЗ
        #   резерва правого поля — на macOS скроллбар overlay (ширина 0), поэтому
        #   поля симметричны. RU — худшая локаль ("Режим:"=64 > EN "Engine:"=60;
        #   оба пункта движка ≈159px), поэтому 350 покрывает и EN.
        #   Ниже этого порога QComboBox перестаёт вмещать текст и обрезался до
        #   "Визуальный (Sig…", а фиксированные по высоте ряды HUD/кнопок
        #   наслаивались (Z-collision). Пол гарантирует, что текст всегда влезает.
        self.SIDEBAR_MIN_WIDTH = 350
        self.sidebar_widget.setMinimumWidth(self.SIDEBAR_MIN_WIDTH)

        # Overflow protection. The shell added to the splitter holds ONLY a
        # QScrollArea; the real content lives inside it. Horizontal fit is now
        # guaranteed by the hard minimum width above (the splitter cannot make
        # the panel narrower than the widest control row), so horizontal scroll
        # stays OFF — it would only hide controls off-screen. The scroll area
        # exists for the VERTICAL axis: short windows shed height as scroll
        # instead of letting blocks overlap.
        _sidebar_shell = QVBoxLayout(self.sidebar_widget)
        _sidebar_shell.setContentsMargins(0, 0, 0, 0)
        _sidebar_shell.setSpacing(0)

        self.sidebar_scroll = QScrollArea()
        self.sidebar_scroll.setObjectName("sidebar_scroll")
        self.sidebar_scroll.setWidgetResizable(True)
        self.sidebar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # Let the panel's "sidebar" background show through the viewport.
        self.sidebar_scroll.setStyleSheet(
            "QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }"
        )
        _sidebar_shell.addWidget(self.sidebar_scroll)

        sidebar_content = QWidget()
        self.sidebar_scroll.setWidget(sidebar_content)

        sidebar_layout = QVBoxLayout(sidebar_content)
        # СИММЕТРИЧНЫЕ поля — карточки центрируются по оси панели. Резерв под
        # скроллбар убран: на macOS вертикальный скроллбар overlay (рисуется
        # поверх, ширины в раскладке не занимает), а на Win/Linux непрозрачный
        # скроллбар сжимает viewport QScrollArea — Qt сам уводит контент левее,
        # ничего не перекрывая. Горизонтальный запас уже гарантирован жёстким
        # SIDEBAR_MIN_WIDTH=350 (~38px форы). Асимметричный правый отступ лишь
        # дублировал эту защиту и ломал центральную ось карточек.
        sidebar_layout.setContentsMargins(12, 12, 12, 12)
        sidebar_layout.setSpacing(12)

        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(0, 0, 0, 0)
        top_bar.setSpacing(8)
        
        self.combo_theme = QComboBox()
        self.combo_theme.addItems(["Dark", "Light", "System"])
        self.combo_theme.setFixedHeight(34) 
        self.combo_theme.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        
        self.combo_lang = QComboBox()
        self.combo_lang.addItems(["EN", "RU"])
        
        self.combo_lang.blockSignals(True)
        self.combo_lang.setCurrentIndex(0 if translator.current_lang == "en" else 1)
        self.combo_lang.blockSignals(False)
        
        self.combo_lang.setFixedHeight(34) 
        self.combo_lang.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        
        self.btn_help = QPushButton()
        self.btn_help.setObjectName("secondary")
        self.btn_help.setFixedHeight(34) 
        self.btn_help.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        
        top_bar.addWidget(self.combo_theme, 1)
        top_bar.addWidget(self.combo_lang, 1)
        top_bar.addWidget(self.btn_help, 1)
        
        sidebar_layout.addLayout(top_bar)

        tab_nav_widget = QWidget()
        tab_nav_layout = QHBoxLayout(tab_nav_widget)
        tab_nav_layout.setContentsMargins(0, 0, 0, 8)
        tab_nav_layout.setSpacing(6)

        tab_css = """
        QPushButton {
            background-color: transparent; color: #949BA4;
            border: 1px solid #4E5058; border-radius: 6px;
            font-weight: bold;
        }
        QPushButton:checked {
            background-color: #5865F2; color: white; border: 1px solid #5865F2;
        }
        QPushButton:hover:!checked { background-color: #3F4147; color: #DBDEE1; }
        """
        self.btn_tab_scan = QPushButton()
        self.btn_tab_analytics = QPushButton()
        
        self.btn_tab_scan.setFixedHeight(38)
        self.btn_tab_analytics.setFixedHeight(38)
        self.btn_tab_scan.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.btn_tab_analytics.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.btn_tab_scan.setStyleSheet(tab_css)
        self.btn_tab_analytics.setStyleSheet(tab_css)
        self.btn_tab_scan.setCheckable(True)
        self.btn_tab_analytics.setCheckable(True)
        self.btn_tab_scan.setChecked(True)

        tab_nav_layout.addWidget(self.btn_tab_scan)
        tab_nav_layout.addWidget(self.btn_tab_analytics)
        
        sidebar_layout.addWidget(tab_nav_widget)

        self.tabs = QStackedWidget()
        self.tabs.setObjectName("main_tabs")
        
        tab_config = QWidget()
        config_l = QVBoxLayout(tab_config)
        config_l.setContentsMargins(0, 0, 0, 0)
        config_l.setSpacing(14) 
        
        dir_card = QWidget()
        dir_card.setObjectName("card")
        dir_l = QVBoxLayout(dir_card)
        dir_l.setContentsMargins(14, 14, 14, 14) 
        dir_l.setSpacing(12) 
        
        mode_l = QVBoxLayout()
        mode_l.setSpacing(6)
        self.rb_single = QRadioButton()
        self.rb_dual = QRadioButton()
        for rb in (self.rb_single, self.rb_dual):
            # Expand horizontally so each radio gets its share of the row and the
            # (translated) label is never clipped — no brittle fixed minimum width.
            rb.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.rb_single.setChecked(True)
        mode_l.addWidget(self.rb_single)
        mode_l.addWidget(self.rb_dual)
        dir_l.addLayout(mode_l)

        dir_a_l = QHBoxLayout()
        self.btn_select_a = QPushButton()
        self.btn_select_a.setObjectName("secondary")
        # Fluid: size to its own label, let the elided path label take the slack.
        self.btn_select_a.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        dir_a_l.addWidget(self.btn_select_a)
        self.lbl_path_a = QLabel()
        self.lbl_path_a.setObjectName("elide_label")
        self.lbl_path_a.setWordWrap(True)
        self.lbl_path_a.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        dir_a_l.addWidget(self.lbl_path_a, stretch=1)
        dir_l.addLayout(dir_a_l)

        self.dir_b_widget = QWidget()
        dir_b_l = QHBoxLayout(self.dir_b_widget)
        dir_b_l.setContentsMargins(0, 0, 0, 0)
        self.btn_select_b = QPushButton()
        self.btn_select_b.setObjectName("secondary")
        # Fluid: size to its own label, let the elided path label take the slack.
        self.btn_select_b.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        dir_b_l.addWidget(self.btn_select_b)
        self.lbl_path_b = QLabel()
        self.lbl_path_b.setObjectName("elide_label")
        self.lbl_path_b.setWordWrap(True)
        self.lbl_path_b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        dir_b_l.addWidget(self.lbl_path_b, stretch=1)
        dir_l.addWidget(self.dir_b_widget)
        self.dir_b_widget.hide()
        
        # Сетка вместо одного ряда: при фиксированной ширине сайдбара (310px)
        # три чекбокса в одну строку не помещались и "Документы" обрезались.
        # Раскладываем 2 колонки x 2 строки: Фото/Видео сверху, Документы снизу.
        # Колонки тянутся равномерно, а сами чекбоксы расширяются, поэтому текст
        # любой длины гарантированно влезает.
        type_l = QGridLayout()
        type_l.setSpacing(8)
        type_l.setContentsMargins(0, 0, 0, 0)
        type_l.setColumnStretch(0, 1)
        type_l.setColumnStretch(1, 1)
        self.chk_img = QCheckBox()
        self.chk_img.setChecked(True)
        self.chk_vid = QCheckBox()
        self.chk_vid.setChecked(True)
        self.chk_doc = QCheckBox()
        self.chk_doc.setChecked(True)
        for _chk in (self.chk_img, self.chk_vid, self.chk_doc):
            _chk.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        type_l.addWidget(self.chk_img, 0, 0)
        type_l.addWidget(self.chk_vid, 0, 1)
        type_l.addWidget(self.chk_doc, 1, 0, 1, 2)  # на всю ширину второй строки
        dir_l.addLayout(type_l)
        config_l.addWidget(dir_card)

        cal_card = QWidget()
        cal_card.setObjectName("card")
        cal_l = QVBoxLayout(cal_card)
        cal_l.setContentsMargins(14, 14, 14, 14) 
        cal_l.setSpacing(12) 
        
        engine_mode_l = QHBoxLayout()
        # Нулевые поля + детерминированный зазор: ряд не наследует никаких
        # неявных отступов под-раскладки, гэп между меткой и селектором фиксирован.
        engine_mode_l.setContentsMargins(0, 0, 0, 0)
        # Плотный зазор метка↔селектор: 6px вместо 8px отдаёт 2px обратно combo.
        engine_mode_l.setSpacing(6)

        self.lbl_engine_mode = QLabel()
        # МЕТКА = РОВНО ШИРИНА ТЕКСТА. Fixed по горизонтали + stretch 0: метка
        # резервирует только пиксели своего слова ("Режим:"), а не долю ряда.
        # Так исчезает искусственная пустая зона, из-за которой жёсткая пропорция
        # 1:2 отдавала метке треть контейнера и сжимала селектор.
        self.lbl_engine_mode.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred
        )
        engine_mode_l.addWidget(self.lbl_engine_mode, 0)

        self.combo_engine = FluidComboBox()
        self.combo_engine.setFixedHeight(34)
        # СЕЛЕКТОР ЗАБИРАЕТ ВСЁ ОСТАВШЕЕСЯ. Expanding + stretch 1: combo вплотную
        # прижимается к метке и тянется до правого края карточки, поэтому
        # "Визуальный (SigLIP)" помещается целиком (elide из FluidComboBox
        # остаётся лишь страховкой на экстремально узких окнах).
        self.combo_engine.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.combo_engine.addItems(["", ""])
        engine_mode_l.addWidget(self.combo_engine, 1)
        cal_l.addLayout(engine_mode_l)

        self.lbl_threshold_title = QLabel()
        self.lbl_threshold_title.setProperty("txt", "h2")
        cal_l.addWidget(self.lbl_threshold_title)
        
        self.mode_btn_group = QButtonGroup(self)
        
        modes_grid_widget = QWidget()
        modes_grid_layout = QGridLayout(modes_grid_widget)
        modes_grid_layout.setContentsMargins(0, 0, 0, 0)
        modes_grid_layout.setSpacing(10)
        
        self.rb_strict = QRadioButton()
        self.rb_balanced = QRadioButton()
        self.rb_semantic = QRadioButton()
        self.radio_custom = QRadioButton()
        
        buttons = [
            (self.rb_strict, 0, 0, 0),
            (self.rb_balanced, 1, 0, 1),
            (self.rb_semantic, 2, 1, 0),
            (self.radio_custom, 3, 1, 1)
        ]
        
        for rb, m_id, row, col in buttons:
            rb.setCursor(Qt.CursorShape.PointingHandCursor)
            self.mode_btn_group.addButton(rb, m_id)
            modes_grid_layout.addWidget(rb, row, col)
            
        self.rb_balanced.setChecked(True)
            
        cal_l.addWidget(modes_grid_widget)
        
        slider_l = QHBoxLayout()
        # Симметрия ряда: нулевые поля под-раскладки → левый край ползунка и
        # правый край пина упираются ровно в поля cal_l (14 слева / 14 справа).
        # Никаких QSpacerItem справа от пина: пин — последний элемент ряда.
        slider_l.setContentsMargins(0, 0, 0, 0)
        slider_l.setSpacing(8)
        self.slider_threshold = JumpSlider(Qt.Orientation.Horizontal)
        # Полный диапазон [0-100] — синхронно со строгим QIntValidator пина:
        # клавиатурный ввод и ползунок обязаны принимать одно и то же множество.
        self.slider_threshold.setRange(0, 100)
        self.slider_threshold.setValue(88)
        # ИНТЕРАКТИВНЫЙ ПИН (TASK 2): QLineEdit-мимикрия под метку. Точное
        # значение (например, ровно 96%) вводится с клавиатуры; применение —
        # по Enter/потере фокуса (editingFinished), без кнопки «Применить».
        self.lbl_threshold = ThresholdEdit(88)
        # СТАТИЧНЫЙ ОТСТУП. Ширину пина жёстко фиксируем по самому широкому
        # значению ("100%"), а НЕ по живому тексту. Иначе при 88% ↔ 100% (и при
        # смене локали, меняющей метрики шрифта) ширина пина плавала, и правый
        # зазор от ползунка до края карточки «дышал». Фикс-ширина изолирует
        # геометрию ряда от длины подписи: правый край ползунка теперь
        # детерминирован и одинаков в EN и RU.
        _thr_fm = QFontMetrics(self.lbl_threshold.font())
        self.lbl_threshold.setFixedWidth(_thr_fm.horizontalAdvance("100%") + 8)
        self.lbl_threshold.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.lbl_threshold.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        # stretch=1 ползунку, 0 пину: весь свободный ход забирает ползунок, пин
        # держит свою фикс-ширину как жёсткую распорку у правого края карточки.
        # РАЗДЕЛЕНИЕ КОНТУРОВ (TASK 1): живой визуальный фидбек при движении мыши
        # замкнут ВНУТРИ представления — valueChanged обновляет только текст пина.
        # Эта связь НЕ касается контроллера, бэкенда и активного экрана: тяжёлый
        # FAISS-пересчёт триггерит исключительно sliderReleased (см. контроллер).
        # set_value не эмитит value_committed, поэтому цикла recluster не возникает.
        self.slider_threshold.valueChanged.connect(self.lbl_threshold.set_value)
        slider_l.addWidget(self.slider_threshold, 1)
        slider_l.addWidget(self.lbl_threshold, 0)
        cal_l.addLayout(slider_l)
        config_l.addWidget(cal_card)

        self.btn_toggle_db = QPushButton()
        self.btn_toggle_db.setObjectName("secondary")
        self.btn_toggle_db.setCheckable(True)
        self.btn_toggle_db.setFixedHeight(34)
        # Тянем на всю ширину config_l (поля 0): левый/правый края кнопки
        # совпадают с внешними краями dir_card/cal_card — ровный прямоугольник
        # контента без визуальных выступов.
        self.btn_toggle_db.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        config_l.addWidget(self.btn_toggle_db)

        self.db_card = QWidget()
        self.db_card.setObjectName("card")
        db_l = QVBoxLayout(self.db_card)
        db_l.setContentsMargins(14, 14, 14, 14)
        db_l.setSpacing(12)
        
        self.lbl_db_info = QLabel()
        self.lbl_db_info.setWordWrap(True)
        self.lbl_db_info.setStyleSheet("color: #949BA4;")
        db_l.addWidget(self.lbl_db_info)
        
        db_btns_l = QHBoxLayout()
        db_btns_l.setSpacing(8)
        db_btns_l.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.btn_clear_faiss = QPushButton()
        self.btn_clear_faiss.setObjectName("secondary")
        self.btn_clear_faiss.setFixedHeight(34) 
        
        self.btn_purge_db = QPushButton()
        self.btn_purge_db.setStyleSheet("""
            QPushButton { 
                background-color: #DA3633; 
                color: white; 
                border: none; 
                font-weight: bold; 
                border-radius: 4px;
                padding: 0 10px; 
            }
            QPushButton:hover { background-color: #f33a37; }
        """)
        self.btn_purge_db.setFixedHeight(34)

        # TASK 3 — Paired buttons in one row must shrink in lockstep, never one
        # truncating before the other. Equal stretch (1/1) splits width evenly;
        # MinimumExpanding keeps both growable. The shared min-width floor is
        # derived from the longest *localized* label and applied in
        # _retranslate_ui (text is empty here), so it also re-fits on lang switch.
        for _btn in (self.btn_clear_faiss, self.btn_purge_db):
            _btn.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)

        db_btns_l.addWidget(self.btn_clear_faiss, 1)
        db_btns_l.addWidget(self.btn_purge_db, 1)
        db_l.addLayout(db_btns_l)
        
        config_l.addWidget(self.db_card)
        self.db_card.hide()
        
        self.btn_toggle_db.toggled.connect(self.db_card.setVisible)
        self.btn_clear_faiss.clicked.connect(self._purge_faiss)
        self.btn_purge_db.clicked.connect(self._purge_all_data)

        config_l.addStretch()
        self.tabs.addWidget(tab_config)

        tab_analytics = QWidget()
        analytics_l = QVBoxLayout(tab_analytics)
        analytics_l.setContentsMargins(0, 0, 0, 0)
        analytics_l.setSpacing(14)

        stat_card = QWidget()
        stat_card.setObjectName("card")
        stat_l = QGridLayout(stat_card)
        stat_l.setContentsMargins(14, 14, 14, 14)
        stat_l.setSpacing(8)
        
        self.lbl_stat_title = QLabel()
        self.lbl_stat_title.setProperty("txt", "h2")
        self.lbl_stat_title_time = QLabel()
        self.lbl_stat_title_files = QLabel()
        self.lbl_stat_title_dups = QLabel()
        self.lbl_stat_title_sel = QLabel()
        self.lbl_stat_title_del = QLabel()
        
        stat_l.addWidget(self.lbl_stat_title, 0, 0, 1, 2)
        stat_l.addWidget(self.lbl_stat_title_time, 1, 0)
        self.lbl_stat_time = QLabel("00:00:00.0")
        self.lbl_stat_time.setObjectName("stat_val")
        stat_l.addWidget(self.lbl_stat_time, 1, 1, alignment=Qt.AlignmentFlag.AlignRight)
        stat_l.addWidget(self.lbl_stat_title_files, 2, 0)
        self.lbl_stat_files = QLabel("0")
        self.lbl_stat_files.setObjectName("stat_val")
        stat_l.addWidget(self.lbl_stat_files, 2, 1, alignment=Qt.AlignmentFlag.AlignRight)
        stat_l.addWidget(self.lbl_stat_title_dups, 3, 0)
        self.lbl_stat_dups = QLabel("0")
        self.lbl_stat_dups.setObjectName("stat_val")
        stat_l.addWidget(self.lbl_stat_dups, 3, 1, alignment=Qt.AlignmentFlag.AlignRight)
        stat_l.addWidget(self.lbl_stat_title_sel, 4, 0)
        self.lbl_stat_selected = QLabel("0")
        self.lbl_stat_selected.setObjectName("stat_val")
        stat_l.addWidget(self.lbl_stat_selected, 4, 1, alignment=Qt.AlignmentFlag.AlignRight)
        stat_l.addWidget(self.lbl_stat_title_del, 5, 0)
        self.lbl_stat_saved = QLabel("0.0 MB")
        self.lbl_stat_saved.setObjectName("stat_val")
        stat_l.addWidget(self.lbl_stat_saved, 5, 1, alignment=Qt.AlignmentFlag.AlignRight)
        
        self.dist_container = QWidget()
        self.dist_container.setFixedHeight(8)
        self.dist_container.setStyleSheet("border-radius: 4px; background-color: #1E1F22; margin-top: 5px;")
        dist_layout = QHBoxLayout(self.dist_container)
        dist_layout.setContentsMargins(0, 0, 0, 0)
        dist_layout.setSpacing(0)
        
        self.bar_img = QFrame()
        self.bar_img.setStyleSheet("background-color: #5865F2; border-top-left-radius: 4px; border-bottom-left-radius: 4px;")
        self.bar_vid = QFrame()
        self.bar_vid.setStyleSheet("background-color: #DA3633;")
        self.bar_doc = QFrame()
        self.bar_doc.setStyleSheet("background-color: #FEE75C; border-top-right-radius: 4px; border-bottom-right-radius: 4px;")
        
        dist_layout.addWidget(self.bar_img, stretch=0)
        dist_layout.addWidget(self.bar_vid, stretch=0)
        dist_layout.addWidget(self.bar_doc, stretch=0)
        
        stat_l.addWidget(self.dist_container, 6, 0, 1, 2)
        
        self.legend_widget = QWidget()
        legend_l = QGridLayout(self.legend_widget)
        legend_l.setContentsMargins(0, 8, 0, 0)
        legend_l.setSpacing(8)
        
        def make_legend_column(color, col_idx):
            hdr_w = QWidget()
            hdr_l = QHBoxLayout(hdr_w)
            hdr_l.setContentsMargins(0, 0, 0, 0)
            hdr_l.setSpacing(4)
            c = QFrame()
            c.setFixedSize(10, 10)
            c.setStyleSheet(f"background-color: {color}; border-radius: 2px;")
            lbl_title = QLabel()
            lbl_title.setStyleSheet("font-weight: bold; color: #DCDDDE;")
            hdr_l.addWidget(c)
            hdr_l.addWidget(lbl_title)
            hdr_l.addStretch()
            
            lbl_pct = QLabel("0%")
            lbl_pct.setStyleSheet("color: #949BA4;")
            lbl_dup = QLabel("0")
            lbl_dup.setStyleSheet("color: #949BA4;")
            lbl_sz = QLabel("0.0 MB")
            lbl_sz.setStyleSheet("color: #949BA4;")

            # The "Total files: N" text is long and lives in a narrow column of
            # the fixed-width sidebar. Let the value labels wrap and expand to
            # fill their cell instead of being clipped — no hard width caps.
            for lbl in (lbl_title, lbl_pct, lbl_dup, lbl_sz):
                lbl.setWordWrap(True)
                lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

            legend_l.addWidget(hdr_w, 0, col_idx)
            legend_l.addWidget(lbl_pct, 1, col_idx)
            legend_l.addWidget(lbl_dup, 2, col_idx)
            legend_l.addWidget(lbl_sz, 3, col_idx)
            # Give every legend column an equal share of the available width.
            legend_l.setColumnStretch(col_idx, 1)

            return lbl_title, lbl_pct, lbl_dup, lbl_sz

        self.leg_img_title, self.leg_img_pct, self.leg_img_dup, self.leg_img_sz = make_legend_column("#5865F2", 0)
        self.leg_vid_title, self.leg_vid_pct, self.leg_vid_dup, self.leg_vid_sz = make_legend_column("#DA3633", 1)
        self.leg_doc_title, self.leg_doc_pct, self.leg_doc_dup, self.leg_doc_sz = make_legend_column("#FEE75C", 2)
        
        stat_l.addWidget(self.legend_widget, 7, 0, 1, 2)
        analytics_l.addWidget(stat_card)

        filter_card = QWidget()
        filter_card.setObjectName("card")
        filter_l = QVBoxLayout(filter_card)
        filter_l.setContentsMargins(14, 14, 14, 14)
        filter_l.setSpacing(10)
        self.lbl_filter_title = QLabel()
        self.lbl_filter_title.setProperty("txt", "h2")
        filter_l.addWidget(self.lbl_filter_title)
        
        self.search_input = QLineEdit()
        self.search_input.setFixedHeight(34)
        filter_l.addWidget(self.search_input)

        f_box = QHBoxLayout()
        self.lbl_filter_type = QLabel()
        f_box.addWidget(self.lbl_filter_type)
        self.combo_view_filter = FluidComboBox()
        self.combo_view_filter.setFixedHeight(34)
        # Fluid: closed box shrinks/elides, popup opens to the widest filter name.
        self.combo_view_filter.addItems(["", "", "", ""])
        f_box.addWidget(self.combo_view_filter)
        filter_l.addLayout(f_box)
        analytics_l.addWidget(filter_card)

        mark_card = QWidget()
        mark_card.setObjectName("card")
        mark_l = QVBoxLayout(mark_card)
        mark_l.setContentsMargins(14, 14, 14, 14)
        mark_l.setSpacing(10)
        self.lbl_mark_title = QLabel()
        self.lbl_mark_title.setProperty("txt", "h2")
        mark_l.addWidget(self.lbl_mark_title)
        
        self.combo_strategy = FluidComboBox()
        self.combo_strategy.setFixedHeight(34)
        # Fluid: closed box shrinks/elides, popup opens to "Самые старые (По дате)".
        self.combo_strategy.addItems(["", "", "", ""])
        self.chk_keep_clean = QCheckBox()
        self.chk_keep_clean.setChecked(True)
        mark_l.addWidget(self.combo_strategy)
        mark_l.addWidget(self.chk_keep_clean)
        
        auto_tools_l = QHBoxLayout()
        auto_tools_l.setSpacing(10)
        self.btn_auto_select = QPushButton()
        self.btn_auto_select.setObjectName("action")
        self.btn_auto_select.setFixedHeight(34)
        self.btn_clear_select = QPushButton()
        self.btn_clear_select.setObjectName("secondary")
        self.btn_clear_select.setFixedHeight(34)
        # Ctrl/Cmd+D больше НЕ висит на этой кнопке: шорткат переехал в нативный
        # QMenuBar ("Правка" → "Снять выделение", см. _setup_menubar), чтобы не
        # было двойного срабатывания. Кнопка остаётся как обычный сброс галочек.
        auto_tools_l.addWidget(self.btn_auto_select)
        auto_tools_l.addWidget(self.btn_clear_select)
        mark_l.addLayout(auto_tools_l)
        analytics_l.addWidget(mark_card)
        analytics_l.addStretch()
        self.tabs.addWidget(tab_analytics)

        sidebar_layout.addWidget(self.tabs)

        scan_status_layout = QVBoxLayout()
        scan_status_layout.setSpacing(6)
        
        status_hud = QHBoxLayout()
        status_hud.setSpacing(8)
        # TASK 2 — Status text now elides (ElideRight) instead of word-wrapping.
        # Word-wrap kept full width then overlapped the right-aligned telemetry
        # ("Done" over "[NPU: 0.00s]"); elision gives a defined truncation and a
        # tooltip with the full string. Ignored h-policy lets it shrink first.
        self.lbl_status = ElidingLabel()
        self.lbl_status.setObjectName("status")

        self.lbl_telemetry = QLabel()
        self.lbl_telemetry.setObjectName("telemetry_hud")
        self.lbl_telemetry.setStyleSheet("color: #5865F2; font-weight: bold;")
        self.lbl_telemetry.setAlignment(Qt.AlignmentFlag.AlignRight)
        # NPU/RAM peak is a fixed-format readout: it must win the width race and
        # never be clipped. Maximum/Fixed pins it to its sizeHint.
        self.lbl_telemetry.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

        # Expanding spacer guarantees a non-negative gap between the two labels
        # so they can never paint over one another, even mid-transition.
        status_hud.addWidget(self.lbl_status, stretch=1)
        status_hud.addSpacerItem(
            QSpacerItem(0, 0, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        )
        status_hud.addWidget(self.lbl_telemetry, stretch=0)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(6) 
        self.progress_bar.setTextVisible(False)
        
        scan_status_layout.addLayout(status_hud)
        scan_status_layout.addWidget(self.progress_bar)
        sidebar_layout.addLayout(scan_status_layout)
        
        scan_controls = QHBoxLayout()
        scan_controls.setSpacing(10)

        # Крупная верхняя панель управления: увеличенная высота + жирный шрифт и
        # щедрые отступы, чтобы 'Сканировать'/'Пауза'/'Стоп' читались как основные
        # действия. Размер шрифта НЕ задаём — он наследуется из глобального
        # app-шрифта; локально оставляем только вес и отступы.
        big_btn_qss = "QPushButton { font-weight: bold; padding: 12px 22px; }"

        self.btn_scan = QPushButton()
        self.btn_scan.setObjectName("primary")
        self.btn_scan.setMinimumHeight(ThemeManager.BUTTON_HEIGHT_PRIMARY)
        self.btn_scan.setStyleSheet(big_btn_qss)
        self.btn_scan.setEnabled(False)

        self.btn_pause = QPushButton()
        self.btn_pause.setObjectName("secondary")
        self.btn_pause.setMinimumHeight(ThemeManager.BUTTON_HEIGHT_PRIMARY)
        self.btn_pause.setStyleSheet(big_btn_qss)
        self.btn_pause.hide()

        self.btn_stop = QPushButton()
        self.btn_stop.setObjectName("secondary")
        self.btn_stop.setMinimumHeight(ThemeManager.BUTTON_HEIGHT_PRIMARY)
        self.btn_stop.setStyleSheet(
            "QPushButton { background-color: #DA3633; border: none; color: white; "
            "font-weight: bold; padding: 12px 22px; }"
        )
        self.btn_stop.hide()
        
        self.scan_controls_layout = scan_controls
        self.scan_controls_layout.addWidget(self.btn_scan)
        self.scan_controls_layout.addWidget(self.btn_pause)
        self.scan_controls_layout.addWidget(self.btn_stop)
        sidebar_layout.addLayout(self.scan_controls_layout)

        # TASK 4 — адаптивная панель управления сканированием. Полные локализо-
        # ванные подписи кнопок хранятся здесь (НЕ читаются обратно из text(),
        # который в компактном режиме пуст); _update_scan_controls_mode решает,
        # помещается ли текстовый ряд в текущую ширину сайдбара, и при нехватке
        # места переводит кнопки в иконочный режим с QToolTip.
        self._scan_labels = {"scan": "", "pause": "", "stop": ""}

        self.master_splitter.addWidget(self.sidebar_widget)

        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        self.inner_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.inner_splitter.setHandleWidth(1)
        content_layout.addWidget(self.inner_splitter)
        
        tree_container = QWidget()
        tree_layout = QVBoxLayout(tree_container)
        tree_layout.setContentsMargins(8, 8, 8, 8)
        tree_layout.setSpacing(5)
        
        top_toolbar = QWidget()
        top_toolbar.setObjectName("toolbar_flat")
        tt_layout = QHBoxLayout(top_toolbar)
        tt_layout.setContentsMargins(5, 5, 5, 5)
        
        self.btn_toggle_sidebar = QPushButton()
        self.btn_toggle_sidebar.setFlat(True)
        self.btn_toggle_sidebar.setObjectName("collapser")
        self.btn_toggle_sidebar.clicked.connect(self._toggle_sidebar)
        tt_layout.addWidget(self.btn_toggle_sidebar)
        tt_layout.addStretch()
        
        self.btn_expand = QPushButton()
        self.btn_expand.setObjectName("collapser")
        self.btn_collapse = QPushButton()
        self.btn_collapse.setObjectName("collapser")
        tt_layout.addWidget(self.btn_expand)
        tt_layout.addWidget(self.btn_collapse)
        tt_layout.addStretch()
        
        self.btn_toggle_inspector = QPushButton()
        self.btn_toggle_inspector.setObjectName("collapser")
        self.btn_toggle_inspector.clicked.connect(self._toggle_inspector)
        tt_layout.addWidget(self.btn_toggle_inspector)
        tree_layout.addWidget(top_toolbar)

        self.tree = MediaTreeView()
        self.tree.setObjectName("tree")
        self.tree.setSortingEnabled(True) 
        
        self.model = LazyClusterModel()
        self.proxy_model = ArbitrageSortFilterProxyModel(self)
        self.proxy_model.setSourceModel(self.model)
        self.tree.setModel(self.proxy_model)
        
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        
        tree_layout.addWidget(self.tree)
                
        self.inspector_frame = QWidget()
        self.inspector_frame.setObjectName("inspector")
        # Fluid: width is driven by the inner_splitter's stretch (4:6) and its
        # initial setSizes(), not a hard 300px floor.
        self.inspector_frame.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        inspector_layout = QVBoxLayout(self.inspector_frame)
        inspector_layout.setContentsMargins(0, 0, 0, 0)
        inspector_layout.setSpacing(0)
        
        self.preview_stack = QStackedWidget()
        
        single_prev_card = QWidget()
        self.single_prev_layout = QVBoxLayout(single_prev_card)
        self.single_preview_label = ScalableImageLabel()
        self.single_prev_layout.addWidget(self.single_preview_label)
        self.preview_stack.addWidget(single_prev_card)
        
        # LAZY INIT (страницы тяжёлых движков предпросмотра). При старте в стек
        # кладутся ПУСТЫЕ хосты-страницы — индексы (0 single / 1 video / 2 multi /
        # 3 discrete) стабильны, но сами движки (QMediaPlayer+QVideoSink с
        # ffmpeg-плагином; PIL/fitz дискретного контура) конструируются ТОЛЬКО при
        # первом реальном обращении пользователя через ensure_video_player() /
        # ensure_discrete_preview(). Хост-страница создаётся с родителем сразу
        # (никаких parentless show() — см. правило против Spaces Jump).
        self.video_player = None
        self._video_host = QWidget()
        _vh_layout = QVBoxLayout(self._video_host)
        _vh_layout.setContentsMargins(0, 0, 0, 0)
        _vh_layout.setSpacing(0)
        self.preview_stack.addWidget(self._video_host)

        multi_widget = QWidget()
        multi_layout = QVBoxLayout(multi_widget)
        multi_layout.setContentsMargins(0, 0, 0, 0)
        multi_layout.setSpacing(0)
        
        scroll_multi = QScrollArea()
        scroll_multi.setWidgetResizable(True)
        scroll_multi.setFrameShape(QFrame.Shape.NoFrame)
        scroll_multi.setStyleSheet("background-color: transparent;")
        
        self.multi_grid_container = QWidget()
        self.multi_grid = QGridLayout(self.multi_grid_container)
        self.multi_grid.setContentsMargins(0, 1, 0, 1)
        self.multi_grid.setSpacing(1)
        
        scroll_multi.setWidget(self.multi_grid_container)
        multi_layout.addWidget(scroll_multi, stretch=1)
        
        # Транспорт мультипревью — зеркально панели «Сравнения»: JumpSlider в
        # ПЕРМИЛЛЕ (0..1000, как slider страницы сравнения) для точной адресации
        # длинных роликов + индикатор позиции. Живой дросселированный скраб
        # подключает контроллер (sliderMoved → троттл 33 мс → cv2-кадры).
        self.multi_slider_panel = QWidget()
        self.multi_slider_panel.setObjectName("multi_slider_panel")
        self.multi_slider_panel.setFixedHeight(45)
        ms_layout = QHBoxLayout(self.multi_slider_panel)
        ms_layout.setContentsMargins(15, 0, 12, 0)
        ms_layout.setSpacing(10)
        self.multi_sync_slider = JumpSlider(Qt.Orientation.Horizontal)
        self.multi_sync_slider.setRange(0, 1000)
        self.multi_sync_slider.setValue(250)
        self.multi_sync_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        ms_layout.addWidget(self.multi_sync_slider, stretch=1)
        self.multi_pos_label = QLabel("25%")
        self.multi_pos_label.setObjectName("multi_pos_label")
        self.multi_pos_label.setStyleSheet(
            f"color: {ThemeManager.colors()['text']}; border: none;"
        )
        self.multi_pos_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Фикс ширины метки: «100%» не должен толкать слайдер при скрабе.
        self.multi_pos_label.setFixedWidth(44)
        ms_layout.addWidget(self.multi_pos_label)
        multi_layout.addWidget(self.multi_slider_panel)
        self.multi_slider_panel.hide()
        
        self.preview_stack.addWidget(multi_widget)

        # index 3 — дискретный контур (GIF/PDF/CBZ): пустой хост до первого вызова.
        self.discrete_preview = None
        self._discrete_host = QWidget()
        _dh_layout = QVBoxLayout(self._discrete_host)
        _dh_layout.setContentsMargins(0, 0, 0, 0)
        _dh_layout.setSpacing(0)
        self.preview_stack.addWidget(self._discrete_host)

        inspector_layout.addWidget(self.preview_stack, stretch=1)

        bottom_btns = QWidget()
        bottom_btns.setFixedHeight(64) 
        bottom_btns.setObjectName("bottom_btns")
        bb_layout = QHBoxLayout(bottom_btns)
        bb_layout.setContentsMargins(12, 12, 12, 12)
        bb_layout.setSpacing(10)
        
        self.btn_grid = QPushButton()
        self.btn_grid.setMinimumHeight(ThemeManager.BUTTON_HEIGHT_PRIMARY)
        self.btn_grid.setObjectName("secondary")

        self.btn_move = QPushButton()
        self.btn_move.setMinimumHeight(ThemeManager.BUTTON_HEIGHT_PRIMARY)
        self.btn_move.setObjectName("action")

        # Кнопка удаления — только иконка корзины (без текстовой подписи "Удалить").
        # Идеальный квадрат BUTTON_HEIGHT_ICON × BUTTON_HEIGHT_ICON: выравнивается
        # по горизонтальной оси с Compare/Move и не растягивается.
        self.btn_delete = QPushButton("🗑")
        self.btn_delete.setFixedSize(
            ThemeManager.BUTTON_HEIGHT_ICON, ThemeManager.BUTTON_HEIGHT_ICON
        )
        self.btn_delete.setObjectName("primary")
        self.btn_delete.setStyleSheet("QPushButton { background-color: #DA3633; }")

        # Compare и Move делят доступную ширину поровну (равный stretch=1); корзина
        # — фиксированный квадрат (stretch=0), чтобы не было оптического перевеса.
        bb_layout.addWidget(self.btn_grid, 1)
        bb_layout.addWidget(self.btn_move, 1)
        bb_layout.addWidget(self.btn_delete, 0)
        inspector_layout.addWidget(bottom_btns)

        self.inner_splitter.addWidget(tree_container)
        self.inner_splitter.addWidget(self.inspector_frame)
        self.inner_splitter.setStretchFactor(0, 4)
        self.inner_splitter.setStretchFactor(1, 6)
        
        self.master_splitter.addWidget(content_widget)
        self.master_splitter.setStretchFactor(0, 0)
        self.master_splitter.setStretchFactor(1, 1)
        # Двойная защита сайдбара: stretch=0 отдаёт ВСЁ лишнее место правой панели
        # (сайдбар не растёт при расширении окна), а setCollapsible(0, False) +
        # minimumWidth не дают ручке утащить его в ноль. Под любым давлением правой
        # панели уступает индекс 1, а не сайдбар (см. RCA: кто несёт пол, тот не
        # сжимается). Правый пейн остаётся collapsible — это легитимный layout.
        self.master_splitter.setCollapsible(0, False)
        self.master_splitter.setCollapsible(1, True)

        self.master_splitter.splitterMoved.connect(lambda: self.tree._trigger_stretch())
        self.inner_splitter.splitterMoved.connect(lambda: self.tree._trigger_stretch())
        # Перетаскивание ручки сплиттера меняет ширину сайдбара без resizeEvent
        # окна — пересчитываем режим панели сканирования и здесь.
        self.master_splitter.splitterMoved.connect(self._update_scan_controls_mode)

    def ensure_video_player(self):
        """Ленивая фабрика Контура А (видеопоток). BuiltInVideoPlayer тянет за
        собой QMediaPlayer (прогрев ffmpeg-плагина) и cv2-воркер превью — при
        старте приложения это мёртвый груз, поэтому плеер рождается только при
        первом выборе видеофайла. Родитель задаётся при конструировании, виджет
        добавляется в layout ДО показа страницы (анти-Spaces-Jump инвариант)."""
        if self.video_player is None:
            from ui.components.video_player import BuiltInVideoPlayer
            self.video_player = BuiltInVideoPlayer(self._video_host)
            self._video_host.layout().addWidget(self.video_player)
        return self.video_player

    def ensure_discrete_preview(self):
        """Ленивая фабрика Контура Б (дискретный скрэббинг GIF/PDF/CBZ).
        Модуль импортируется здесь же: PIL/fitz не грузятся при старте."""
        if self.discrete_preview is None:
            from ui.components.discrete_preview import DiscreteScrubbingWidget
            self.discrete_preview = DiscreteScrubbingWidget(self._discrete_host)
            self._discrete_host.layout().addWidget(self.discrete_preview)
        return self.discrete_preview

    def _setup_menubar(self):
        # Cmd+D живёт в нативном меню macOS как единственный владелец шортката.
        # QKeySequence("Ctrl+D") на macOS автоматически маппится Qt в Cmd-D.
        # Это снимает конфликт двойного срабатывания: раньше тот же шорткат
        # дублировался на кнопке btn_clear_select / eventFilter'ах — теперь его
        # держит ровно один QAction в системном QMenuBar.
        self.menu_edit = self.menuBar().addMenu(translator.tr("menu_edit"))

        # "Выбрать дубликаты" (Cmd/Ctrl+A) — авто-выбор дубликатов по текущей
        # стратегии. Живёт в нативном QMenuBar единственным владельцем шортката.
        # Маршрутизируем через клик btn_auto_select (та же логика, что у
        # action_deselect → btn_clear_select), чтобы не дублировать привязку.
        self.action_select_dups = QAction(translator.tr("action_select_dups"), self)
        self.action_select_dups.setShortcut(QKeySequence.StandardKey.SelectAll)
        self.action_select_dups.triggered.connect(self.btn_auto_select.click)
        self.menu_edit.addAction(self.action_select_dups)

        # "Выбрать все" (Cmd/Ctrl+R) — обычное выделение всех строк дерева.
        # Намеренно НЕ на Cmd+A (его держит "Выбрать дубликаты") и НЕ на Cmd+C
        # (исторически висел тут, но переехал на Cmd+R, чтобы освободить Cmd+C
        # и убрать любую путаницу с системным «копировать»).
        self.action_select_all = QAction(translator.tr("action_select_all"), self)
        self.action_select_all.setShortcut(QKeySequence("Ctrl+R"))
        self.action_select_all.triggered.connect(self.tree.selectAll)
        self.menu_edit.addAction(self.action_select_all)

        self.action_deselect = QAction(translator.tr("action_deselect"), self)
        self.action_deselect.setShortcut(QKeySequence("Ctrl+D"))
        # Cmd-D должен снимать галочки (checkmarks) с файлов, а не фокус строки.
        # btn_clear_select подключена в контроллере к selection_controller.clear_selection,
        # поэтому маршрутизируем сигнал через клик этой кнопки.
        self.action_deselect.triggered.connect(self.btn_clear_select.click)
        self.menu_edit.addAction(self.action_deselect)

        # Cmd+F / Cmd+O / Cmd+P переехали из QShortcut в нативный QMenuBar.
        # На macOS QShortcut ненадёжен и не отображается в меню/строке статуса;
        # QAction в системном меню — единственный надёжный владелец шортката
        # (QKeySequence("Ctrl+...") Qt маппит в Cmd). Сигналы подключает
        # контроллер в _bind_signals, чтобы не дублировать привязку.
        self.action_clear_sel = QAction(translator.tr("btn_clear"), self)
        self.action_clear_sel.setShortcut(QKeySequence("Ctrl+F"))
        self.menu_edit.addAction(self.action_clear_sel)

        self.action_move = QAction(translator.tr("btn_move"), self)
        self.action_move.setShortcut(QKeySequence("Ctrl+O"))
        self.menu_edit.addAction(self.action_move)

        self.action_compare = QAction(translator.tr("btn_compare"), self)
        self.action_compare.setShortcut(QKeySequence("Ctrl+P"))
        self.menu_edit.addAction(self.action_compare)

    def _scan_button_specs(self):
        """(кнопка, полная подпись, глиф ThemeManager, цвет иконки|None=цвет темы)."""
        return [
            (self.btn_scan, self._scan_labels["scan"], "scan", "#FFFFFF"),
            (self.btn_pause, self._scan_labels["pause"], "pause", None),
            (self.btn_stop, self._scan_labels["stop"], "stop", "#FFFFFF"),
        ]

    def update_pause_label(self, text: str):
        """Контроллер меняет подпись Пауза↔Продолжить через этот метод (прямой
        setText сломал бы компактный иконочный режим, где text() пуст)."""
        self._scan_labels["pause"] = text
        self._update_scan_controls_mode()

    def _update_scan_controls_mode(self):
        """TASK 4: если видимый ряд кнопок управления сканированием не уклады-
        вается в текущую ширину сайдбара (включая минимальные 350px на Windows-
        метриках шрифта), кнопки сбрасывают текст и превращаются в компактный
        ряд векторных иконок с подсказками — горизонтальный вылет за сплиттер
        исключён по построению."""
        if not hasattr(self, "scan_controls_layout") or not self._scan_labels["scan"]:
            return

        specs = self._scan_button_specs()
        visible = [s for s in specs if s[0].isVisibleTo(self)]
        if not visible:
            return

        fm = self.btn_scan.fontMetrics()
        spacing = self.scan_controls_layout.spacing()
        # 22px паддинги big_btn_qss с двух сторон + рамка/запас ≈ 48px на кнопку;
        # 24 — поля sidebar_layout (12+12).
        needed = (
            sum(fm.horizontalAdvance(s[1]) + 48 for s in visible)
            + spacing * (len(visible) - 1) + 24
        )
        available = self.sidebar_widget.width() or self.SIDEBAR_MIN_WIDTH
        compact = needed > available

        theme_text = ThemeManager.colors()["text"]
        for btn, label, glyph, color in specs:
            if compact:
                btn.setText("")
                btn.setIcon(ThemeManager.make_icon(glyph, color or theme_text))
                btn.setIconSize(QSize(20, 20))
                btn.setToolTip(label)
            else:
                btn.setIcon(QIcon())
                btn.setText(label)
                btn.setToolTip("")

    def _toggle_sidebar(self):
        is_visible = self.sidebar_widget.isVisible()
        self.sidebar_widget.setVisible(not is_visible)
        if is_visible:
            self.btn_toggle_sidebar.setText(translator.tr("btn_sidebar_show"))
        else:
            self.master_splitter.setSizes([290, self.width() - 290])
            self.btn_toggle_sidebar.setText(translator.tr("btn_sidebar_hide"))
        self.tree._trigger_stretch()

    def _toggle_inspector(self):
        is_visible = self.inspector_frame.isVisible()
        self.inspector_frame.setVisible(not is_visible)
        if is_visible:
            self.btn_toggle_inspector.setText(translator.tr("btn_inspector_show"))
        else:
            w = self.width()
            self.inner_splitter.setSizes([int(w * 0.4), int(w * 0.6)])
            self.btn_toggle_inspector.setText(translator.tr("btn_inspector_hide"))
        self.tree._trigger_stretch()

    def _restore_state(self):
        # Геометрия окна восстанавливается ПЕРВОЙ: задаёт реальную self.width(),
        # от которой считаются дефолтные setSizes() сплиттеров ниже.
        geom = self.settings.value("geometry")
        if geom is not None:
            self.restoreGeometry(geom)

        sp_state = self.settings.value("master_splitter")
        if sp_state:
            self.master_splitter.restoreState(sp_state)
        else:
            # Дефолт первого запуска: сайдбар на своём полу + остаток правой панели.
            # minimumWidth всё равно не даст уйти ниже SIDEBAR_MIN_WIDTH.
            self.master_splitter.setSizes(
                [self.SIDEBAR_MIN_WIDTH, max(1, self.width() - self.SIDEBAR_MIN_WIDTH)]
            )

        isp_state = self.settings.value("inner_splitter")
        if isp_state:
            self.inner_splitter.restoreState(isp_state)
        else:
            w = self.width()
            self.inner_splitter.setSizes([int(w * 0.4), int(w * 0.6)])

    def _retranslate_ui(self):
        if hasattr(self, 'menu_edit'):
            self.menu_edit.setTitle(translator.tr("menu_edit"))
            self.action_select_all.setText(translator.tr("action_select_all"))
            self.action_select_dups.setText(translator.tr("action_select_dups"))
            self.action_deselect.setText(translator.tr("action_deselect"))
            self.action_clear_sel.setText(translator.tr("btn_clear"))
            self.action_move.setText(translator.tr("btn_move"))
            self.action_compare.setText(translator.tr("btn_compare"))
        self.btn_help.setText(translator.tr("help"))
        self.btn_tab_scan.setText(translator.tr("tab_scan"))
        self.btn_tab_analytics.setText(translator.tr("tab_analytics"))
        self.rb_single.setText(translator.tr("mode_single"))
        self.rb_dual.setText(translator.tr("mode_dual"))
        self.btn_select_a.setText(translator.tr("btn_select_a"))
        self.btn_select_b.setText(translator.tr("btn_select_b"))
        
        default_paths = {
            self.dictionaries["en"]["lbl_not_selected"] if hasattr(self, 'dictionaries') and "en" in self.dictionaries else "Directory not selected",
            self.dictionaries["ru"]["lbl_not_selected"] if hasattr(self, 'dictionaries') and "ru" in self.dictionaries else "Папка не выбрана",
            "Directory not selected", "Папка не выбрана", ""
        } 
        
        if not hasattr(self, 'lbl_path_a') or self.lbl_path_a.text() in default_paths: 
            self.lbl_path_a.setText(translator.tr("lbl_not_selected"))
        if not hasattr(self, 'lbl_path_b') or self.lbl_path_b.text() in default_paths: 
            self.lbl_path_b.setText(translator.tr("lbl_not_selected"))
        
        self.chk_img.setText(translator.tr("chk_img"))
        self.chk_vid.setText(translator.tr("chk_vid"))
        self.chk_doc.setText(translator.tr("chk_doc"))
        
        self.lbl_engine_mode.setText(translator.tr("engine_mode"))
        self.combo_engine.setItemText(0, translator.tr("engine_visual"))
        self.combo_engine.setItemText(1, translator.tr("engine_faces"))
        self.lbl_threshold_title.setText(translator.tr("threshold"))
        
        self.rb_strict.setText(translator.tr("mode_strict"))
        self.rb_balanced.setText(translator.tr("mode_balanced"))
        self.rb_semantic.setText(translator.tr("mode_semantic"))
        self.radio_custom.setText(translator.tr("mode_custom"))
        
        # Подписи панели сканирования идут через адаптивный слой (TASK 4):
        # в широком сайдбаре — текст, в сжатом — иконки с QToolTip.
        self._scan_labels["scan"] = translator.tr("btn_scan")
        self._scan_labels["pause"] = translator.tr("btn_pause")
        self._scan_labels["stop"] = translator.tr("btn_stop")
        self._update_scan_controls_mode()
        
        self.lbl_stat_title.setText(translator.tr("telemetry"))
        self.lbl_stat_title_time.setText(translator.tr("stat_time"))
        self.lbl_stat_title_files.setText(translator.tr("stat_files"))
        self.lbl_stat_title_dups.setText(translator.tr("stat_dups"))
        self.lbl_stat_title_sel.setText(translator.tr("stat_sel"))
        self.lbl_stat_title_del.setText(translator.tr("stat_del"))
        
        self.lbl_filter_title.setText(translator.tr("filters"))
        self.search_input.setPlaceholderText(translator.tr("search_ph"))
        self.lbl_filter_type.setText(translator.tr("type"))
        
        self.combo_view_filter.blockSignals(True)
        self.combo_view_filter.setItemText(0, translator.tr("filter_all"))
        self.combo_view_filter.setItemText(1, translator.tr("filter_img"))
        self.combo_view_filter.setItemText(2, translator.tr("filter_vid"))
        self.combo_view_filter.setItemText(3, translator.tr("filter_doc"))
        self.combo_view_filter.blockSignals(False)
        
        self.lbl_mark_title.setText(translator.tr("marking"))
        self.combo_strategy.blockSignals(True)
        self.combo_strategy.setItemText(0, translator.tr("strat_smart"))
        self.combo_strategy.setItemText(1, translator.tr("strat_quality"))
        self.combo_strategy.setItemText(2, translator.tr("strat_size"))
        self.combo_strategy.setItemText(3, translator.tr("strat_date"))
        self.combo_strategy.blockSignals(False)
        self.chk_keep_clean.setText(translator.tr("chk_clean"))
        self.btn_auto_select.setText(translator.tr("btn_auto"))
        # Метла-иконка делает кнопку "Сброс" визуально явной (и это же — носитель
        # нативного шортката Ctrl/Cmd+D).
        self.btn_clear_select.setText(f"🧹 {translator.tr('btn_clear')}")
        
        if self.sidebar_widget.isVisible(): self.btn_toggle_sidebar.setText(translator.tr("btn_sidebar_hide"))
        else: self.btn_toggle_sidebar.setText(translator.tr("btn_sidebar_show"))
        
        self.btn_expand.setText(translator.tr("btn_expand"))
        self.btn_collapse.setText(translator.tr("btn_collapse"))
        
        if self.inspector_frame.isVisible(): self.btn_toggle_inspector.setText(translator.tr("btn_inspector_hide"))
        else: self.btn_toggle_inspector.setText(translator.tr("btn_inspector_show"))
        
        self.btn_grid.setText(translator.tr("btn_compare"))
        self.btn_move.setText(translator.tr("btn_move"))
        # Иконка-корзина: текст не задаём, переводим только всплывающую подсказку
        self.btn_delete.setToolTip(translator.tr("btn_del"))

        self.model.setHorizontalHeaderLabels([
            translator.tr("col_file"), translator.tr("col_fmt"), 
            translator.tr("col_sim"), translator.tr("col_size"), 
            translator.tr("col_res"), translator.tr("col_time")
        ])
        
        header = self.tree.header()
        header.setStretchLastSection(False) 
        header.setMinimumSectionSize(50)
        
        for i in range(6):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)

        lang = getattr(translator, 'current_lang', 'en')
        is_ru = (lang == 'ru')

        self.btn_toggle_db.setText("Управление данными" if is_ru else "Data Management")
        self.lbl_db_info.setText(
            "Очистка матриц FAISS и SQLite-векторов. Потребуется полный рескан файлов." if is_ru
            else "Clears FAISS matrices and SQLite vectors. Requires full rescan."
        )
        self.btn_clear_faiss.setText("Очистить кэш" if is_ru else "Clear cache")
        self.btn_clear_faiss.setToolTip(
            "Очищает индекс быстрого поиска (кэш FAISS) на диске" if is_ru
            else "Clears the fast-search index (FAISS cache) on disk"
        )
        self.btn_purge_db.setText("Сбросить БД" if is_ru else "Purge DB")
        self.btn_purge_db.setToolTip(
            "Внимание: безвозвратно удаляет все проиндексированные данные (векторы, журналы, кэш FAISS). Потребуется полный рескан." if is_ru
            else "Warning: permanently deletes all indexed data (vectors, logs, FAISS cache). A full rescan is required."
        )
        # Fluid: the paired DB buttons already share MinimumExpanding policy, so
        # the layout gives them equal width from the available row space — no
        # per-locale hard floor needed (that was the old setMinimumWidth hack).

        self.tree._trigger_stretch()
        self.single_preview_label.update()
        self.single_preview_label.repaint()
        
        if self.lbl_status.text() in {"✅ Done", "✅ Готово"}:
            self.lbl_status.setText(translator.tr("status_done"))
        elif self.lbl_status.text() in {"Scanning...", "Сканирование...", "Ожидание...", "Waiting..."}:
            self.lbl_status.setText(translator.tr("status_wait"))
        elif self.model.rowCount() == 0 and self.lbl_status.text() == translator.tr("status_done"):
            self.lbl_status.setText(translator.tr("status_npu_ready"))

    def _purge_faiss(self):
        FaissManager.purge_disk_cache()
        msg = "Матрицы FAISS успешно очищены" if translator.current_lang == "ru" else "FAISS matrices successfully cleared"
        self.lbl_status.setText(msg)
        auditor.info("UI: FAISS cache manually purged")

    def _purge_all_data(self):
        # DESTRUCTIVE: wipes every indexed record (SQLite vectors + FAISS graphs)
        # off disk. Guarded by a strict warning dialog so it can never fire on a
        # stray click.
        is_ru = (getattr(translator, 'current_lang', 'en') == 'ru')
        title = "Сбросить базу данных" if is_ru else "Purge Database"
        question = (
            "Вы уверены, что хотите удалить все проиндексированные данные?" if is_ru
            else "Are you sure you want to delete all indexed data?"
        )
        reply = QMessageBox.warning(
            self, title, question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.btn_purge_db.setEnabled(False)
        self.lbl_status.setText(
            "Сброс базы данных..." if is_ru else "Purging database..."
        )
        auditor.warning("UI: FULL data purge confirmed by user.")

        self._purge_worker = PurgeWorker()
        self._purge_worker.finished.connect(self._on_purge_done)
        self._purge_worker.finished.connect(self._purge_worker.deleteLater)
        self._purge_worker.start()

    def _on_purge_done(self):
        is_ru = (getattr(translator, 'current_lang', 'en') == 'ru')
        self.btn_purge_db.setEnabled(True)
        self.lbl_status.setText(
            "База данных сброшена" if is_ru else "Database purged"
        )
        auditor.info("UI: Full data purge completed.")

    def update_telemetry_hud(self, elapsed_seconds: float, ram_mb: float):
        try:
            m, s = divmod(int(elapsed_seconds), 60)
            h, m = divmod(m, 60)
            sub_s = int((elapsed_seconds - int(elapsed_seconds)) * 10)
            time_str = f"{h:02d}:{m:02d}:{s:02d}.{sub_s}"
            self.lbl_telemetry.setText(f"[ {time_str} | {ram_mb:.1f} MB ]")
        except Exception as e:
            auditor.warning(f"HUD format error: {e}")

    def update_analytics_dashboard(self, elapsed_seconds: float, files: int, dups: int, selected: int, saved_mb: float):
        try:
            m, s = divmod(int(elapsed_seconds), 60)
            h, m = divmod(m, 60)
            sub_s = int((elapsed_seconds - int(elapsed_seconds)) * 10)
            self.lbl_stat_time.setText(f"{h:02d}:{m:02d}:{s:02d}.{sub_s}")
            self.lbl_stat_files.setText(str(files))
            self.lbl_stat_dups.setText(str(dups))
            self.lbl_stat_selected.setText(str(selected))
            self.lbl_stat_saved.setText(f"{saved_mb:.1f} MB")
        except Exception as e:
            auditor.warning(f"Analytics format error: {e}")