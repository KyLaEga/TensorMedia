import os
import subprocess
from pathlib import Path

from PyQt6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QLabel, QProgressBar, QFileDialog, 
                             QMessageBox, QComboBox, QLineEdit, QRadioButton, 
                             QButtonGroup, QHeaderView, QSplitter, QScrollArea, 
                             QApplication, QCheckBox, QFrame, QMenu, QStackedWidget, 
                             QAbstractItemView, QGridLayout, QSizePolicy, QDialog, 
                             QTabWidget)
from PyQt6.QtCore import Qt, QSettings, QTimer
from PyQt6.QtGui import QPixmap, QShortcut, QKeySequence, QStandardItemModel, QStandardItem, QColor, QIcon

from utils.batch_operations import BatchOperations
from utils.theme_manager import ThemeManager
from utils.i18n import translator
from ui.views.multi_compare import MultiCompareDialog 

from ui.workers import MultiVideoWorker, ScannerBridge, ClusterWorker, EngineWarmupWorker
from ui.components.video_player import BuiltInVideoPlayer, JumpSlider
from ui.components.media_tree import MediaTreeView, SortableStandardItem
from ui.components.image_label import ScalableImageLabel
from ui.components.dialogs import VisualDeleteDialog

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Tensor Media Arbitrage v64.0 (MVC Framework)")
        self.resize(1450, 900)
        self.target_dir_a = None
        self.target_dir_b = None
        self.last_hovered_path = None
        
        self.multi_preview_lbls = {}
        self.video_worker = MultiVideoWorker()
        self.video_worker.frame_ready.connect(self._on_worker_frame_ready)
        
        self.selection_timer = QTimer(self)
        self.selection_timer.setSingleShot(True)
        self.selection_timer.timeout.connect(self._process_selection)
        
        self.engine = None 
        self.settings = QSettings("TensorMedia", "ArbitrageConfig")
        
        self.scan_seconds = 0
        self.scan_timer = QTimer(self)
        self.scan_timer.timeout.connect(self._update_timer_label)
        
        self._setup_ui()
        self._init_hotkeys() 
        
        ThemeManager.apply_modern_dark(QApplication.instance()) 
        self._restore_state() 
        self.setAcceptDrops(True)

        self.btn_scan.setEnabled(False)
        self.lbl_status.setText(translator.tr("status_wait"))
        
        translator.language_changed.connect(self._retranslate_ui)
        self._retranslate_ui()

        self.warmup_worker = EngineWarmupWorker()
        self.warmup_worker.engine_ready.connect(self._on_engine_ready)
        self.warmup_worker.start()

    def _on_engine_ready(self, engine):
        self.engine = engine
        self.lbl_status.setText(translator.tr("status_npu_ready"))
        self._check_ready()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if not urls: return
        
        path = urls[0].toLocalFile()
        if os.path.isdir(path):
            self.target_dir_a = path
            self.lbl_path_a.setText(str(path))
            self._check_ready()
            if self.btn_scan.isEnabled():
                self._start_scan()

    def closeEvent(self, event):
        self.video_worker.stop() 
        self.settings.setValue("master_splitter", self.master_splitter.saveState())
        self.settings.setValue("inner_splitter", self.inner_splitter.saveState())
        super().closeEvent(event)

    def _init_hotkeys(self):
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, self._manual_check_selected, context=Qt.ShortcutContext.ApplicationShortcut)
        QShortcut(QKeySequence(Qt.Key.Key_Return), self, self._manual_check_selected, context=Qt.ShortcutContext.ApplicationShortcut)
        QShortcut(QKeySequence(Qt.Key.Key_Backspace), self, self._soft_delete_trigger, context=Qt.ShortcutContext.ApplicationShortcut)
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self, self._soft_delete_trigger, context=Qt.ShortcutContext.ApplicationShortcut)
        QShortcut(QKeySequence("Shift+Backspace"), self, self._hard_delete_trigger, context=Qt.ShortcutContext.ApplicationShortcut)
        QShortcut(QKeySequence("Shift+Delete"), self, self._hard_delete_trigger, context=Qt.ShortcutContext.ApplicationShortcut)
        QShortcut(QKeySequence("Ctrl+A"), self, self._apply_auto_selection, context=Qt.ShortcutContext.ApplicationShortcut)
        QShortcut(QKeySequence("Ctrl+D"), self, self._clear_selection, context=Qt.ShortcutContext.ApplicationShortcut)
        QShortcut(QKeySequence(Qt.Key.Key_F1), self, self._show_hotkeys_help, context=Qt.ShortcutContext.ApplicationShortcut)

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.master_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.master_splitter.setHandleWidth(1)
        main_layout.addWidget(self.master_splitter)

        self.sidebar_widget = QWidget()
        self.sidebar_widget.setObjectName("sidebar")
        self.sidebar_widget.setMinimumWidth(295)
        self.sidebar_widget.setMaximumWidth(340)
        sidebar_layout = QVBoxLayout(self.sidebar_widget)
        # Делаем симметричные отступы 10px со всех сторон
        sidebar_layout.setContentsMargins(10, 10, 10, 10)
        sidebar_layout.setSpacing(10)

        top_bar = QHBoxLayout()
        top_bar.setSpacing(5)
        
        self.combo_theme = QComboBox()
        self.combo_theme.addItems(["🌙 Dark", "☀️ Light", "💻 Sys"])
        self.combo_theme.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.combo_theme.currentIndexChanged.connect(self._change_theme)
        
        self.combo_lang = QComboBox()
        self.combo_lang.addItems(["🇬🇧 EN", "🇷🇺 RU"])
        self.combo_lang.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        from utils.i18n import translator
        self.combo_lang.setCurrentIndex(0 if translator.current_lang == "en" else 1)
        self.combo_lang.currentIndexChanged.connect(self._change_language)
        
        self.btn_help = QPushButton()
        self.btn_help.setObjectName("secondary")
        self.btn_help.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.btn_help.clicked.connect(self._show_hotkeys_help)
        
        top_bar.addWidget(self.combo_theme)
        top_bar.addWidget(self.combo_lang)
        top_bar.addStretch() # Пружина строго между языком и кнопкой
        top_bar.addWidget(self.btn_help)
        
        sidebar_layout.addLayout(top_bar)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("main_tabs")
        self.tabs.setDocumentMode(True)
        self.tabs.setUsesScrollButtons(False) 
        # Форсируем растягивание вкладок на 100% доступной ширины
        self.tabs.tabBar().setExpanding(True) 

        # --- ПАТЧ: Раздельные независимые кнопки ---
        self.tabs.setStyleSheet("""
            QTabWidget::pane {
                border: none;
                padding: 0px;
                margin: 0px;
            }
            QTabBar::tab {
                height: 30px;
                border-radius: 6px; /* Каждая кнопка теперь самостоятельная */
                margin-bottom: 8px; /* Отступ до нижних карточек */
            }
            /* Блокируем системные прыжки при переключении */
            QTabBar::tab:selected, QTabBar::tab:!selected {
                margin-top: 0px; 
            }
            /* Распределяем зазор ровно по центру, чтобы кнопки были симметричны */
            QTabBar::tab:first {
                margin-right: 4px; 
            }
            QTabBar::tab:last {
                margin-left: 4px; 
            }
        """)
        # --- КОНЕЦ ПАТЧА ---
        
        tab_config = QWidget()
        config_l = QVBoxLayout(tab_config)
        # Левые/правые отступы 0, чтобы карточки шли вровень с верхним баром
        config_l.setContentsMargins(0, 8, 0, 0)
        config_l.setSpacing(10)
        
        dir_card = QWidget()
        dir_card.setObjectName("card")
        dir_l = QVBoxLayout(dir_card)
        dir_l.setContentsMargins(8, 8, 8, 8)
        
        mode_l = QHBoxLayout()
        self.rb_single = QRadioButton()
        self.rb_dual = QRadioButton()
        self.rb_single.setChecked(True)
        self.rb_single.toggled.connect(self._toggle_scan_mode)
        mode_l.addWidget(self.rb_single)
        mode_l.addWidget(self.rb_dual)
        dir_l.addLayout(mode_l)

        dir_a_l = QHBoxLayout()
        self.btn_select_a = QPushButton()
        self.btn_select_a.setObjectName("secondary")
        self.btn_select_a.setFixedWidth(100)
        self.btn_select_a.clicked.connect(lambda: self._select_directory('a'))
        dir_a_l.addWidget(self.btn_select_a)
        self.lbl_path_a = QLabel()
        self.lbl_path_a.setObjectName("elide_label")
        dir_a_l.addWidget(self.lbl_path_a, stretch=1)
        dir_l.addLayout(dir_a_l)

        self.dir_b_widget = QWidget()
        dir_b_l = QHBoxLayout(self.dir_b_widget)
        dir_b_l.setContentsMargins(0, 0, 0, 0)
        self.btn_select_b = QPushButton()
        self.btn_select_b.setObjectName("secondary")
        self.btn_select_b.setFixedWidth(100)
        self.btn_select_b.clicked.connect(lambda: self._select_directory('b'))
        dir_b_l.addWidget(self.btn_select_b)
        self.lbl_path_b = QLabel()
        self.lbl_path_b.setObjectName("elide_label")
        dir_b_l.addWidget(self.lbl_path_b, stretch=1)
        dir_l.addWidget(self.dir_b_widget)
        self.dir_b_widget.hide()
        
        type_l = QHBoxLayout()
        self.chk_img = QCheckBox()
        self.chk_img.setChecked(True)
        self.chk_vid = QCheckBox()
        self.chk_vid.setChecked(True)
        self.chk_doc = QCheckBox()
        self.chk_doc.setChecked(True)
        type_l.addWidget(self.chk_img)
        type_l.addWidget(self.chk_vid)
        type_l.addWidget(self.chk_doc)
        dir_l.addLayout(type_l)
        config_l.addWidget(dir_card)

        cal_card = QWidget()
        cal_card.setObjectName("card")
        cal_l = QVBoxLayout(cal_card)
        cal_l.setContentsMargins(8, 8, 8, 8)
        
        engine_mode_l = QHBoxLayout()
        self.lbl_engine_mode = QLabel()
        engine_mode_l.addWidget(self.lbl_engine_mode)
        self.combo_engine = QComboBox()
        self.combo_engine.addItems(["", ""]) 
        self.combo_engine.currentIndexChanged.connect(self._trigger_recluster_if_engine_changes)
        engine_mode_l.addWidget(self.combo_engine)
        cal_l.addLayout(engine_mode_l)

        self.lbl_threshold_title = QLabel()
        cal_l.addWidget(self.lbl_threshold_title)
        
        self.mode_btn_group = QButtonGroup()
        modes_grid = QGridLayout()
        modes_grid.setSpacing(5)
        presets = [("Strict (96%)", 0), ("Balanced (88%)", 1), ("Semantic (81%)", 2), ("Custom", 3)]
        for i, (text, m_id) in enumerate(presets):
            rb = QRadioButton(text)
            rb.setCursor(Qt.CursorShape.PointingHandCursor)
            if m_id == 1: rb.setChecked(True)
            if m_id == 3: self.radio_custom = rb
            self.mode_btn_group.addButton(rb, m_id)
            modes_grid.addWidget(rb, i // 2, i % 2) 
        cal_l.addLayout(modes_grid)
        self.mode_btn_group.idClicked.connect(self._sync_radio_to_slider)
        
        slider_l = QHBoxLayout()
        self.slider_threshold = JumpSlider(Qt.Orientation.Horizontal)
        self.slider_threshold.setRange(50, 100)
        self.slider_threshold.setValue(88)
        self.slider_threshold.valueChanged.connect(self._on_slider_change)
        self.slider_threshold.sliderReleased.connect(self._trigger_recluster)
        self.lbl_threshold = QLabel("88%")
        self.lbl_threshold.setFixedWidth(35)
        self.lbl_threshold.setAlignment(Qt.AlignmentFlag.AlignRight)
        slider_l.addWidget(self.slider_threshold)
        slider_l.addWidget(self.lbl_threshold)
        cal_l.addLayout(slider_l)
        config_l.addWidget(cal_card)
        config_l.addStretch()
        self.tabs.addTab(tab_config, "")

        tab_analytics = QWidget()
        analytics_l = QVBoxLayout(tab_analytics)
        analytics_l.setContentsMargins(0, 8, 0, 0)
        analytics_l.setSpacing(10)

        stat_card = QWidget()
        stat_card.setObjectName("card")
        stat_l = QGridLayout(stat_card)
        stat_l.setContentsMargins(8, 8, 8, 8)
        stat_l.setSpacing(4)
        
        self.lbl_stat_title = QLabel()
        self.lbl_stat_title_time = QLabel()
        self.lbl_stat_title_files = QLabel()
        self.lbl_stat_title_dups = QLabel()
        self.lbl_stat_title_sel = QLabel()
        self.lbl_stat_title_del = QLabel()
        
        stat_l.addWidget(self.lbl_stat_title, 0, 0, 1, 2)
        stat_l.addWidget(self.lbl_stat_title_time, 1, 0)
        self.lbl_stat_time = QLabel("00:00:00")
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
        analytics_l.addWidget(stat_card)

        filter_card = QWidget()
        filter_card.setObjectName("card")
        filter_l = QVBoxLayout(filter_card)
        filter_l.setContentsMargins(8, 8, 8, 8)
        self.lbl_filter_title = QLabel()
        filter_l.addWidget(self.lbl_filter_title)
        
        self.search_input = QLineEdit()
        self.search_input.textChanged.connect(self._apply_view_filter)
        filter_l.addWidget(self.search_input)

        f_box = QHBoxLayout()
        self.lbl_filter_type = QLabel()
        f_box.addWidget(self.lbl_filter_type)
        self.combo_view_filter = QComboBox()
        self.combo_view_filter.addItems(["", "", "", ""])
        self.combo_view_filter.currentIndexChanged.connect(self._apply_view_filter)
        f_box.addWidget(self.combo_view_filter)
        filter_l.addLayout(f_box)
        analytics_l.addWidget(filter_card)

        mark_card = QWidget()
        mark_card.setObjectName("card")
        mark_l = QVBoxLayout(mark_card)
        mark_l.setContentsMargins(8, 8, 8, 8)
        self.lbl_mark_title = QLabel()
        mark_l.addWidget(self.lbl_mark_title)
        
        self.combo_strategy = QComboBox()
        self.combo_strategy.addItems(["", "", "", ""])
        self.chk_keep_clean = QCheckBox()
        self.chk_keep_clean.setChecked(True)
        mark_l.addWidget(self.combo_strategy)
        mark_l.addWidget(self.chk_keep_clean)
        
        auto_tools_l = QHBoxLayout()
        auto_tools_l.setSpacing(5)
        self.btn_auto_select = QPushButton()
        self.btn_auto_select.setObjectName("action")
        self.btn_auto_select.clicked.connect(self._apply_auto_selection)
        self.btn_clear_select = QPushButton()
        self.btn_clear_select.setObjectName("secondary")
        self.btn_clear_select.clicked.connect(self._clear_selection)
        auto_tools_l.addWidget(self.btn_auto_select)
        auto_tools_l.addWidget(self.btn_clear_select)
        mark_l.addLayout(auto_tools_l)
        analytics_l.addWidget(mark_card)
        analytics_l.addStretch()
        self.tabs.addTab(tab_analytics, "")

        sidebar_layout.addWidget(self.tabs)

        scan_status_layout = QVBoxLayout()
        self.lbl_status = QLabel()
        self.lbl_status.setObjectName("status")
        self.lbl_status.setWordWrap(True)
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(4)
        self.progress_bar.setTextVisible(False)
        scan_status_layout.addWidget(self.lbl_status)
        scan_status_layout.addWidget(self.progress_bar)
        sidebar_layout.addLayout(scan_status_layout)
        
        scan_controls = QHBoxLayout()
        self.btn_scan = QPushButton()
        self.btn_scan.setObjectName("primary")
        self.btn_scan.setMinimumHeight(40)
        self.btn_scan.setEnabled(False)
        self.btn_scan.clicked.connect(self._start_scan)
        
        self.btn_pause = QPushButton()
        self.btn_pause.setObjectName("secondary")
        self.btn_pause.setMinimumHeight(40)
        self.btn_pause.clicked.connect(self._toggle_pause)
        self.btn_pause.hide()
        
        self.btn_stop = QPushButton()
        self.btn_stop.setObjectName("secondary")
        self.btn_stop.setMinimumHeight(40)
        self.btn_stop.setStyleSheet("QPushButton { background-color: #DA3633; border: none; color: white; }") 
        self.btn_stop.clicked.connect(self._stop_scan)
        self.btn_stop.hide()
        
        scan_controls.addWidget(self.btn_scan)
        scan_controls.addWidget(self.btn_pause)
        scan_controls.addWidget(self.btn_stop)
        sidebar_layout.addLayout(scan_controls)
        
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
        self.btn_expand.clicked.connect(self._expand_all_safely)
        self.btn_collapse = QPushButton()
        self.btn_collapse.setObjectName("collapser")
        self.btn_collapse.clicked.connect(self._collapse_all_safely)
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
        
        self.model = QStandardItemModel()
        self.tree.setModel(self.model)
        
        header = self.tree.header()
        header.setStretchLastSection(False) 
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.tree.setColumnWidth(1, 60)   
        self.tree.setColumnWidth(2, 75)   
        self.tree.setColumnWidth(3, 70)   
        self.tree.setColumnWidth(4, 95)   
        self.tree.setColumnWidth(5, 60)   
        self.tree.hideColumn(6) 
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.selectionModel().selectionChanged.connect(lambda: self.selection_timer.start(150))
        self.tree.doubleClicked.connect(self._on_item_double_clicked)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        
        tree_layout.addWidget(self.tree)
        
        self.model.itemChanged.connect(self._update_savings)
                
        self.inspector_frame = QWidget()
        self.inspector_frame.setObjectName("inspector")
        self.inspector_frame.setMinimumWidth(300) 
        inspector_layout = QVBoxLayout(self.inspector_frame)
        inspector_layout.setContentsMargins(0, 0, 0, 0)
        inspector_layout.setSpacing(0)
        
        self.preview_stack = QStackedWidget()
        
        single_prev_card = QWidget()
        single_prev_prev = QVBoxLayout(single_prev_card)
        single_prev_prev.setContentsMargins(5, 5, 5, 5)
        self.single_preview_label = ScalableImageLabel()
        single_prev_prev.addWidget(self.single_preview_label)
        self.preview_stack.addWidget(single_prev_card)
        
        self.video_player = BuiltInVideoPlayer()
        self.preview_stack.addWidget(self.video_player)
        
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
        self.multi_grid.setContentsMargins(0, 0, 0, 0)
        self.multi_grid.setSpacing(8)
        
        scroll_multi.setWidget(self.multi_grid_container)
        multi_layout.addWidget(scroll_multi, stretch=1)
        
        self.multi_slider_panel = QWidget()
        self.multi_slider_panel.setObjectName("multi_slider_panel")
        self.multi_slider_panel.setFixedHeight(45)
        ms_layout = QHBoxLayout(self.multi_slider_panel)
        ms_layout.setContentsMargins(15, 0, 15, 0)
        self.multi_sync_slider = JumpSlider(Qt.Orientation.Horizontal)
        self.multi_sync_slider.setRange(0, 100)
        self.multi_sync_slider.setValue(25)
        self.multi_sync_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.multi_sync_slider.sliderReleased.connect(self._execute_multi_video_frames)
        ms_layout.addWidget(QLabel("🎥"))
        ms_layout.addWidget(self.multi_sync_slider)
        multi_layout.addWidget(self.multi_slider_panel)
        self.multi_slider_panel.hide()
        
        self.preview_stack.addWidget(multi_widget)
        inspector_layout.addWidget(self.preview_stack, stretch=1)

        bottom_btns = QWidget()
        bottom_btns.setFixedHeight(60)
        bottom_btns.setObjectName("bottom_btns")
        bb_layout = QHBoxLayout(bottom_btns)
        bb_layout.setContentsMargins(10, 10, 10, 10)
        
        self.btn_grid = QPushButton()
        self.btn_grid.setMinimumHeight(40)
        self.btn_grid.setObjectName("secondary")
        self.btn_grid.clicked.connect(self._trigger_grid_compare)
        
        self.btn_move = QPushButton()
        self.btn_move.setMinimumHeight(40)
        self.btn_move.setObjectName("action")
        self.btn_move.clicked.connect(self._move_trigger)
        
        self.btn_delete = QPushButton()
        self.btn_delete.setMinimumHeight(40)
        self.btn_delete.setObjectName("primary")
        self.btn_delete.setStyleSheet("QPushButton { background-color: #DA3633; }") 
        self.btn_delete.clicked.connect(self._soft_delete_trigger)
        
        bb_layout.addWidget(self.btn_grid)
        bb_layout.addWidget(self.btn_move)
        bb_layout.addWidget(self.btn_delete)
        inspector_layout.addWidget(bottom_btns)

        self.inner_splitter.addWidget(tree_container)
        self.inner_splitter.addWidget(self.inspector_frame)
        self.inner_splitter.setStretchFactor(0, 4)
        self.inner_splitter.setStretchFactor(1, 6)
        
        self.master_splitter.addWidget(content_widget)

    def _change_language(self, idx):
        lang_code = "en" if idx == 0 else "ru"
        translator.set_language(lang_code)

    def _retranslate_ui(self):
        """Интеграция локализованных матриц во все статические узлы интерфейса."""
        self.btn_help.setText(translator.tr("help"))
        self.tabs.setTabText(0, translator.tr("tab_scan"))
        self.tabs.setTabText(1, translator.tr("tab_analytics"))
        
        self.rb_single.setText(translator.tr("mode_single"))
        self.rb_dual.setText(translator.tr("mode_dual"))
        self.btn_select_a.setText(translator.tr("btn_select_a"))
        self.btn_select_b.setText(translator.tr("btn_select_b"))
        if not self.target_dir_a: self.lbl_path_a.setText(translator.tr("lbl_not_selected"))
        if not self.target_dir_b: self.lbl_path_b.setText(translator.tr("lbl_not_selected"))
        
        self.chk_img.setText(translator.tr("chk_img"))
        self.chk_vid.setText(translator.tr("chk_vid"))
        self.chk_doc.setText(translator.tr("chk_doc"))
        
        self.lbl_engine_mode.setText(translator.tr("engine_mode"))
        self.combo_engine.setItemText(0, translator.tr("engine_visual"))
        self.combo_engine.setItemText(1, translator.tr("engine_faces"))
        self.lbl_threshold_title.setText(translator.tr("threshold"))
        
        self.btn_scan.setText(translator.tr("btn_scan"))
        self.btn_pause.setText(translator.tr("btn_pause"))
        self.btn_stop.setText(translator.tr("btn_stop"))
        
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
        self.btn_clear_select.setText(translator.tr("btn_clear"))
        
        if self.sidebar_widget.isVisible(): self.btn_toggle_sidebar.setText(translator.tr("btn_sidebar_hide"))
        else: self.btn_toggle_sidebar.setText(translator.tr("btn_sidebar_show"))
        
        self.btn_expand.setText(translator.tr("btn_expand"))
        self.btn_collapse.setText(translator.tr("btn_collapse"))
        
        if self.inspector_frame.isVisible(): self.btn_toggle_inspector.setText(translator.tr("btn_inspector_hide"))
        else: self.btn_toggle_inspector.setText(translator.tr("btn_inspector_show"))
        
        self.btn_grid.setText(translator.tr("btn_compare"))
        self.btn_move.setText(translator.tr("btn_move"))
        self.btn_delete.setText(translator.tr("btn_del"))

        self.model.setHorizontalHeaderLabels([
            translator.tr("col_file"), translator.tr("col_fmt"), 
            translator.tr("col_sim"), translator.tr("col_size"), 
            translator.tr("col_res"), translator.tr("col_time"), "Path"
        ])
        
        for i in range(self.model.rowCount()):
            group = self.model.item(i, 0)
            vis_children = 0
            for j in range(group.rowCount()):
                if not self.tree.isRowHidden(j, group.index()): vis_children += 1
                child_node = group.child(j, 0)
                data = child_node.data(Qt.ItemDataRole.UserRole)
                if data:
                    is_ref = data.get('is_ref', False)
                    display_name = f"{translator.tr('ref_prefix')} {Path(data['path']).name}" if is_ref else Path(data['path']).name
                    child_node.setText(display_name)
            
            group.setText(f"{translator.tr('cluster_prefix')} #{i+1} ({vis_children} {translator.tr('cluster_files')})")

        if self.engine is not None and not self.btn_pause.isVisible():
            self.lbl_status.setText(translator.tr("status_npu_ready"))
        elif self.engine is None:
            self.lbl_status.setText(translator.tr("status_wait"))

    def _trigger_recluster_if_engine_changes(self):
        if hasattr(self, 'engine') and self.engine is not None and self.target_dir_a:
            if self.model.rowCount() > 0:
                self._start_scan()

    def _toggle_scan_mode(self):
        if self.rb_dual.isChecked():
            self.dir_b_widget.show()
        else:
            self.dir_b_widget.hide()
            self.target_dir_b = None
            self.lbl_path_b.setText(translator.tr("lbl_not_selected"))
        self._check_ready()

    def _select_directory(self, mode):
        folder = QFileDialog.getExistingDirectory(self, "Выбор папки" if translator.current_lang == "ru" else "Select Directory")
        if folder: 
            if mode == 'a':
                self.target_dir_a = folder
                self.lbl_path_a.setText(str(folder))
            else:
                self.target_dir_b = folder
                self.lbl_path_b.setText(str(folder))
            self._check_ready()

    def _check_ready(self):
        if self.engine is None:
            self.btn_scan.setEnabled(False)
            return
        if self.rb_single.isChecked() and self.target_dir_a:
            self.btn_scan.setEnabled(True)
        elif self.rb_dual.isChecked() and self.target_dir_a and self.target_dir_b:
            self.btn_scan.setEnabled(True)
        else:
            self.btn_scan.setEnabled(False)

    def _show_hotkeys_help(self):
        QMessageBox.information(self, translator.tr("help_title"), translator.tr("help_text"))

    def _update_timer_label(self):
        if hasattr(self, 'engine') and getattr(self.engine, 'is_paused', False): return 
        self.scan_seconds += 1
        hrs = self.scan_seconds // 3600
        mins = (self.scan_seconds % 3600) // 60
        secs = self.scan_seconds % 60
        self.lbl_stat_time.setText(f"{hrs:02d}:{mins:02d}:{secs:02d}")

    def _update_statistics_panel(self):
        total_files = 0
        dup_count = 0
        for i in range(self.model.rowCount()):
            group = self.model.item(i, 0)
            if self.tree.isRowHidden(i, self.tree.rootIndex()): continue
            vis_children = 0
            for j in range(group.rowCount()):
                if not self.tree.isRowHidden(j, group.index()): vis_children += 1
            total_files += vis_children
            if vis_children > 1: dup_count += (vis_children - 1)
            
        self.lbl_stat_files.setText(str(total_files))
        self.lbl_stat_dups.setText(str(dup_count))
        self._update_savings()

    def _update_savings(self, item=None):
        if item is not None and item.column() != 0: return 
        saved_bytes = 0
        selected_count = 0 
        for i in range(self.model.rowCount()):
            group = self.model.item(i, 0)
            if self.tree.isRowHidden(i, self.tree.rootIndex()): continue
            for j in range(group.rowCount()):
                if self.tree.isRowHidden(j, group.index()): continue
                child = group.child(j, 0)
                if child.checkState() == Qt.CheckState.Checked:
                    selected_count += 1
                    data = child.data(Qt.ItemDataRole.UserRole)
                    if data and 'size' in data: saved_bytes += data['size']
                        
        self.lbl_stat_selected.setText(str(selected_count))
        self.lbl_stat_saved.setText(f"{saved_bytes / (1024*1024):.1f} MB")

    def _toggle_sidebar(self):
        sizes = self.master_splitter.sizes()
        if sizes[0] == 0 or not self.sidebar_widget.isVisible():
            self.sidebar_widget.setVisible(True)
            total = sum(sizes) if sum(sizes) > 0 else self.width()
            self.master_splitter.setSizes([300, total - 300])
            self.btn_toggle_sidebar.setText(translator.tr("btn_sidebar_hide"))
        else:
            self.sidebar_widget.setVisible(False)
            self.btn_toggle_sidebar.setText(translator.tr("btn_sidebar_show"))

    def _toggle_inspector(self):
        sizes = self.inner_splitter.sizes()
        if sizes[1] == 0 or not self.inspector_frame.isVisible():
            self.inspector_frame.setVisible(True)
            total = sum(sizes) if sum(sizes) > 0 else self.width()
            self.inner_splitter.setSizes([int(total*0.4), int(total*0.6)]) 
            self.btn_toggle_inspector.setText(translator.tr("btn_inspector_hide"))
        else:
            self.inspector_frame.setVisible(False)
            self.btn_toggle_inspector.setText(translator.tr("btn_inspector_show"))
            
    def _expand_all_safely(self):
        if hasattr(self, 'tree') and self.model.rowCount() > 0: self.tree.expandAll()
            
    def _collapse_all_safely(self):
        if hasattr(self, 'tree') and self.model.rowCount() > 0: self.tree.collapseAll()

    def _restore_state(self):
        sp_state = self.settings.value("master_splitter")
        if sp_state: self.master_splitter.restoreState(sp_state)
        isp_state = self.settings.value("inner_splitter")
        if isp_state: self.inner_splitter.restoreState(isp_state)

    def _change_theme(self, idx):
        app = QApplication.instance()
        if idx == 0: ThemeManager.apply_modern_dark(app)
        elif idx == 1: ThemeManager.apply_modern_light(app)
        else: ThemeManager.apply_system_theme(app)

    def _apply_view_filter(self):
        f_idx = self.combo_view_filter.currentIndex()
        s_text = self.search_input.text().strip().lower()
        
        for i in range(self.model.rowCount()):
            group = self.model.item(i, 0)
            visible_children = 0
            for j in range(group.rowCount()):
                child_node = group.child(j, 0)
                ext = group.child(j, 1).text().lower()
                name = child_node.text().lower()
                
                data = child_node.data(Qt.ItemDataRole.UserRole)
                ocr = data.get('ocr', '').lower() if data else ""
                
                is_vis = True
                if f_idx == 1 and ext not in {'.jpg', '.png', '.webp', '.bmp', '.heic', '.jpeg'}: is_vis = False
                elif f_idx == 2 and ext not in {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'}: is_vis = False
                elif f_idx == 3 and ext not in {'.pdf', '.cbz', '.gif'}: is_vis = False
                
                if s_text and s_text not in name and s_text not in ocr:
                    is_vis = False
                    
                self.tree.setRowHidden(j, group.index(), not is_vis)
                if is_vis: visible_children += 1
            
            self.tree.setRowHidden(i, self.tree.rootIndex(), visible_children == 0)
            if visible_children > 0:
                group.setText(f"{translator.tr('cluster_prefix')} #{i+1} ({visible_children} {translator.tr('cluster_files')})")
                
        self._update_statistics_panel()

    def _manual_check_selected(self):
        indexes = self.tree.selectionModel().selectedRows(0)
        if not indexes: return
        
        files = [self.model.itemFromIndex(idx) for idx in indexes if idx.parent().isValid() and not self.tree.isRowHidden(idx.row(), idx.parent())]
        if not files: return
        
        files = [f for f in files if f.flags() & Qt.ItemFlag.ItemIsUserCheckable]
        if not files: return

        new_state = Qt.CheckState.Checked if files[0].checkState() == Qt.CheckState.Unchecked else Qt.CheckState.Unchecked
        for child in files: 
            child.setCheckState(new_state)

    def _sync_radio_to_slider(self, idx):
        mapping = {0: 96, 1: 88, 2: 81}
        if idx in mapping:
            self.slider_threshold.blockSignals(True)
            self.slider_threshold.setValue(mapping[idx])
            self.slider_threshold.blockSignals(False)
            self.lbl_threshold.setText(f"{mapping[idx]}%")
            self._trigger_recluster()

    def _on_slider_change(self, v):
        self.lbl_threshold.setText(f"{v}%")
        if hasattr(self, 'radio_custom') and not self.radio_custom.isChecked():
            self.mode_btn_group.blockSignals(True)
            self.radio_custom.setChecked(True)
            self.mode_btn_group.blockSignals(False)

    def _trigger_recluster(self):
        if not self.engine.current_file_data: 
            self.lbl_status.setText(translator.tr("status_npu_ready"))
            self.btn_scan.setEnabled(True)
            self.progress_bar.setValue(100)
            return
            
        self.lbl_status.setText("⏳ Re-clustering matrix..." if translator.current_lang == "en" else "⏳ Пересчет матрицы...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        
        threshold = 1.0 - (self.slider_threshold.value() / 100.0)
        self.cluster_worker = ClusterWorker(self.engine, threshold)
        self.cluster_worker.finished.connect(self._on_clustering_finished)
        self.cluster_worker.start()

    def _on_clustering_finished(self, clusters):
        self._render_tree(clusters)
        self._update_statistics_panel()
        QApplication.restoreOverrideCursor()
        self.lbl_status.setText("✅ Done" if translator.current_lang == "en" else "✅ Готово")
        self.tabs.setCurrentIndex(1) 

    def _clear_selection(self):
        for i in range(self.model.rowCount()):
            group = self.model.item(i, 0)
            for j in range(group.rowCount()): 
                child = group.child(j, 0)
                if child.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                    child.setCheckState(Qt.CheckState.Unchecked)

    def _on_item_double_clicked(self, index):
        if index.parent().isValid():
            item = self.model.itemFromIndex(index.siblingAtColumn(0))
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                state = Qt.CheckState.Checked if item.checkState() == Qt.CheckState.Unchecked else Qt.CheckState.Unchecked
                item.setCheckState(state)

    def _start_scan(self):
        if self.engine is None: return
        
        dirs_to_scan = []
        if self.target_dir_a: dirs_to_scan.append(self.target_dir_a)
        if self.rb_dual.isChecked() and self.target_dir_b: dirs_to_scan.append(self.target_dir_b)
        if not dirs_to_scan: return

        exts = set()
        if self.chk_img.isChecked(): exts.update({'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.heic', '.JPG', '.JPEG', '.PNG', '.WEBP', '.BMP', '.HEIC'})
        if self.chk_vid.isChecked(): exts.update({'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v', '.MP4', '.MOV', '.MKV', '.WEBM', '.AVI', '.M4V'})
        if self.chk_doc.isChecked(): exts.update({'.pdf', '.cbz', '.gif', '.PDF', '.CBZ', '.GIF'})
        
        selected_mode = "visual" if self.combo_engine.currentIndex() == 0 else "faces"
        
        self.btn_scan.hide()
        self.btn_pause.show()
        self.btn_stop.show()
        self.model.removeRows(0, self.model.rowCount())
        self.video_player.stop()
        
        self.scan_seconds = 0
        self.lbl_stat_time.setText("00:00:00")
        self.scan_timer.start(1000) 
        
        self.scanner = ScannerBridge(self.engine, dirs_to_scan, exts, selected_mode)
        self.scanner.progress.connect(self._on_scan_progress)
        self.scanner.finished.connect(self._on_scan_finished)
        self.scanner.start()

    def _on_scan_progress(self, current, total, msg):
        pct = int(current / total * 100) if total > 0 else 0
        self.lbl_status.setText(f"[{pct}%] {msg}")
        self.progress_bar.setValue(pct)

    def _toggle_pause(self):
        if not hasattr(self, 'scanner') or not self.scanner.isRunning(): return
        self.engine.is_paused = not self.engine.is_paused
        if self.engine.is_paused:
            self.btn_pause.setText("▶️ Resume" if translator.current_lang == "en" else "▶️ Продолжить")
            self.lbl_status.setText("PAUSED" if translator.current_lang == "en" else "ПАУЗА: Ожидание возобновления")
        else:
            self.btn_pause.setText(translator.tr("btn_pause"))
            self.lbl_status.setText("Scanning..." if translator.current_lang == "en" else "Сканирование...")

    def _stop_scan(self):
        self.engine.is_stopped = True
        self.btn_pause.hide()
        self.btn_stop.hide()
        self.lbl_status.setText("Stopping I/O..." if translator.current_lang == "en" else "Остановка потоков I/O (ожидание)...")

    def _on_scan_finished(self):
        self.scan_timer.stop()
        self.btn_pause.hide()
        self.btn_stop.hide()
        self.btn_scan.show()
        
        if self.engine.is_stopped:
            self.btn_scan.setEnabled(True)
            self.lbl_status.setText("Scan Aborted." if translator.current_lang == "en" else "Сканирование прервано.")
            self.progress_bar.setValue(0)
        else:
            self._trigger_recluster()

    def _render_tree(self, clusters):
        self.model.itemChanged.disconnect(self._update_savings)
        self.model.removeRows(0, self.model.rowCount())
        self.progress_bar.setValue(100)
        self.btn_scan.setEnabled(True)
        
        valid_clusters = []
        for cluster in clusters:
            if self.rb_dual.isChecked() and self.target_dir_a:
                has_inbox = any(not os.path.abspath(it['path']).startswith(os.path.abspath(self.target_dir_a)) for it in cluster)
                if not has_inbox:
                    continue 
            valid_clusters.append(cluster)

        batch_size = 50
        
        for i, cluster in enumerate(valid_clusters):
            total_size = sum(it['size'] for it in cluster)
            sz_str = f"{total_size/(1024*1024):.1f} MB"
            formats = set(Path(it['path']).suffix.upper().replace('.', '') for it in cluster)
            fmt_str = ", ".join(sorted(formats))
            
            max_res_area = 0
            max_dur = 0.0
            sims = [it.get('similarity', 1.0) for it in cluster if 'similarity' in it]
            avg_sim = sum(sims) / len(sims) if sims else 1.0
            avg_sim_str = f"~{avg_sim*100:.1f}%"
            
            for it in cluster:
                res = it.get('resolution', "")
                if res and "x" in res:
                    try: 
                        w, h = res.split("x")
                        area = int(w) * int(h)
                        if area > max_res_area: max_res_area = area
                    except: pass
                dur = it.get('duration', 0.0)
                if dur > max_dur: max_dur = dur

            parent_name = f"{translator.tr('cluster_prefix')} #{i+1} ({len(cluster)} {translator.tr('cluster_files')})"
            parent_cols = [
                SortableStandardItem(parent_name, 0),
                SortableStandardItem(fmt_str, fmt_str),
                SortableStandardItem(avg_sim_str, avg_sim),
                SortableStandardItem(sz_str, total_size),
                SortableStandardItem("", max_res_area),
                SortableStandardItem("", max_dur),
                QStandardItem("")
            ]
            parent_node = parent_cols[0]
            
            for c in parent_cols:
                c.setEditable(False)
                c.setBackground(QColor(128, 128, 128, 40)) 
                font = c.font()
                font.setBold(True)
                c.setFont(font)
            
            for it in cluster:
                p = it['path']
                ext = Path(p).suffix.upper()
                sim_str = f"{it['similarity']*100:.1f}%" if 'similarity' in it else "Base"
                i_sz_str = f"{it['size']/(1024*1024):.1f} MB"
                res = it.get('resolution', "")
                dur = f"{int(it['duration'])//60:02d}:{int(it['duration'])%60:02d}" if it.get('duration') else ""
                
                is_ref = False
                if self.rb_dual.isChecked() and self.target_dir_a:
                    if os.path.abspath(p).startswith(os.path.abspath(self.target_dir_a)):
                        is_ref = True

                display_name = f"{translator.tr('ref_prefix')} {Path(p).name}" if is_ref else Path(p).name

                area = 0
                if res and "x" in res:
                    try: 
                        w, h = res.split("x")
                        area = int(w) * int(h)
                    except: pass
                
                child_cols = [
                    SortableStandardItem(display_name, Path(p).name),
                    SortableStandardItem(ext, ext),
                    SortableStandardItem(sim_str, it.get('similarity', 0.0)),
                    SortableStandardItem(i_sz_str, it['size']),
                    SortableStandardItem(res, area),
                    SortableStandardItem(dur, it.get('duration', 0.0)),
                    QStandardItem(p)
                ]
                
                for c in child_cols:
                    c.setEditable(False)
                    if is_ref:
                        c.setForeground(QColor("#5865F2")) 
                    
                c_node = child_cols[0]
                
                it['is_ref'] = is_ref 
                c_node.setData(it, Qt.ItemDataRole.UserRole) 

                if is_ref:
                    c_node.setCheckable(False) 
                else:
                    c_node.setCheckable(True)
                    c_node.setCheckState(Qt.CheckState.Unchecked)
                
                parent_node.appendRow(child_cols)
                
            self.model.appendRow(parent_cols)
            self.tree.expand(parent_node.index())
            
            if i > 0 and i % batch_size == 0:
                QApplication.processEvents()
                
        self._apply_view_filter()
        self.model.itemChanged.connect(self._update_savings)

    def _process_selection(self):
        indexes = self.tree.selectionModel().selectedRows(0)
        
        if not indexes:
            self.preview_stack.setCurrentIndex(0)
            self.single_preview_label.setPixmap(QPixmap()) 
            return

        if len(indexes) == 1 and not indexes[0].parent().isValid():
            group = self.model.itemFromIndex(indexes[0])
            paths = [group.child(i, 6).text() for i in range(group.rowCount()) if not self.tree.isRowHidden(i, group.index())]
            
            if len(paths) > 1 or (len(paths) == 1 and Path(paths[0]).suffix.lower() in {'.cbz', '.pdf'}):
                self._render_multi_preview(paths)
            elif len(paths) == 1:
                self._render_preview(paths[0])
            return

        sel = [idx for idx in indexes if idx.parent().isValid()]
        
        if len(sel) > 1: 
            self._render_multi_preview([self.model.itemFromIndex(idx.siblingAtColumn(6)).text() for idx in sel]) 
        elif len(sel) == 1: 
            p = self.model.itemFromIndex(sel[0].siblingAtColumn(6)).text()
            if Path(p).suffix.lower() in {'.cbz', '.pdf'}:
                self._render_multi_preview([p])
            else:
                self._render_preview(p)
        else: 
            self.preview_stack.setCurrentIndex(0)
            self.single_preview_label.setPixmap(QPixmap()) 

    def _on_context_menu(self, pos):
        index = self.tree.indexAt(pos)
        if not index.isValid(): return
        
        item = self.model.itemFromIndex(index.siblingAtColumn(0))
        menu = QMenu(self)
        if not index.parent().isValid():
            menu.addAction("✓ Select Inbox" if translator.current_lang == "en" else "✓ Выделить Inbox", lambda: self._set_group_check_state(item, Qt.CheckState.Checked))
            menu.addAction(translator.tr("btn_compare"), self._trigger_grid_compare)
        else:
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                menu.addAction("✅ Toggle" if translator.current_lang == "en" else "✅ Выбрать/Снять", self._manual_check_selected)
            menu.addAction(translator.tr("btn_compare"), self._trigger_grid_compare)
            path = self.model.itemFromIndex(index.siblingAtColumn(6)).text()
            menu.addAction("📁 Reveal in Finder" if translator.current_lang == "en" else "📁 В Finder", lambda: subprocess.run(['open', '-R', path]))
            
        menu.setStyleSheet("QMenu { background-color: #2B2D31; color: white; border: 1px solid #4E5058; } QMenu::item:selected { background-color: #5865F2; }")
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _set_group_check_state(self, item, state):
        for i in range(item.rowCount()):
            if not self.tree.isRowHidden(i, item.index()):
                child = item.child(i, 0)
                if child.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                    child.setCheckState(state)

    def _render_multi_preview(self, paths):
        self.video_player.stop()
        self.preview_stack.setCurrentIndex(2)
        
        while self.multi_grid.count():
            w = self.multi_grid.takeAt(0).widget()
            if w: w.deleteLater()
            
        for r in range(50):
            self.multi_grid.setRowStretch(r, 0)
            
        count = len(paths)
        if count == 0: return
        
        if count <= 2: cols = 1
        elif count <= 4: cols = 2
        elif count <= 9: cols = 3
        elif count <= 16: cols = 4
        else: cols = 5
            
        self.multi_preview_lbls.clear()
        video_paths = []
        
        has_v = False
        for i, p in enumerate(paths):
            lbl = ScalableImageLabel() 
            ext = Path(p).suffix.lower()
            if ext in {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v', '.gif', '.cbz', '.pdf'}:
                has_v = True
                video_paths.append(p)
                self.multi_preview_lbls[p] = lbl
            else:
                try: 
                    lbl.setPixmap(QPixmap(p))
                except: 
                    pass
            
            row_index = i // cols
            col_index = i % cols
            
            self.multi_grid.addWidget(lbl, row_index, col_index)
            
        if has_v: 
            self.multi_slider_panel.show()
            self._execute_multi_video_frames()
        else: 
            self.multi_slider_panel.hide()

    def _execute_multi_video_frames(self):
        if self.multi_preview_lbls:
            self.video_worker.request_frames(list(self.multi_preview_lbls.keys()), self.multi_sync_slider.value())
            
    def _on_worker_frame_ready(self, path, qimg):
        if path in self.multi_preview_lbls:
            lbl = self.multi_preview_lbls[path]
            lbl.setPixmap(QPixmap.fromImage(qimg))

    def _render_preview(self, p):
        self.video_player.stop()
        if not os.path.exists(p): 
            self.preview_stack.setCurrentIndex(0)
            return
        ext = Path(p).suffix.lower()
        if ext in {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'}:
            self.preview_stack.setCurrentIndex(1)
            self.video_player.load_video(p)
        else:
            self.preview_stack.setCurrentIndex(0)
            if ext == '.gif':
                from PyQt6.QtGui import QMovie
                movie = QMovie(p)
                movie.setCacheMode(QMovie.CacheMode.CacheAll)
                self.single_preview_label.setMovie(movie)
            else:
                self.single_preview_label.setPixmap(QPixmap(p))

    def _trigger_grid_compare(self):
        indexes = self.tree.selectionModel().selectedRows(0)
        sel = [idx for idx in indexes if idx.parent().isValid()]
        
        if len(sel) <= 1:
            idx = self.tree.currentIndex()
            if not idx.isValid(): return
            item = self.model.itemFromIndex(idx.siblingAtColumn(0))
            gr = item if item.parent() is None else item.parent()
            pts = [gr.child(i, 6).text() for i in range(gr.rowCount())]
        else:
            pts = [self.model.itemFromIndex(idx.siblingAtColumn(6)).text() for idx in sel]
            
        if len(pts) < 2: return
        dlg = MultiCompareDialog(pts, self)
        if dlg.exec():
            if dlg.files_to_delete:
                if dlg.delete_hard: BatchOperations.hard_delete(dlg.files_to_delete)
                else: BatchOperations.safe_delete(dlg.files_to_delete)
                self._trigger_recluster()

    def _move_trigger(self):
        to_move = []
        for i in range(self.model.rowCount()):
            group = self.model.item(i, 0)
            for j in range(group.rowCount()):
                child = group.child(j, 0)
                if child.checkState() == Qt.CheckState.Checked:
                    to_move.append(group.child(j, 6).text())
                    
        if not to_move: return
        msg = "Куда переместить файлы?" if translator.current_lang == "ru" else "Where to move files?"
        dest = QFileDialog.getExistingDirectory(self, msg)
        if dest:
            res = BatchOperations.move_files(to_move, dest)
            report = f"Перемещено: {res['moved']}\nОшибок: {res['failed']}" if translator.current_lang == "ru" else f"Moved: {res['moved']}\nFailed: {res['failed']}"
            title = "Готово" if translator.current_lang == "ru" else "Done"
            QMessageBox.information(self, title, report)
            self._trigger_recluster()

    def _apply_auto_selection(self):
        s_idx = self.combo_strategy.currentIndex()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.model.blockSignals(True) 
        
        for i in range(self.model.rowCount()):
            group = self.model.item(i, 0)
            if self.tree.isRowHidden(i, self.tree.rootIndex()): continue
            files = []
            for j in range(group.rowCount()):
                if self.tree.isRowHidden(j, group.index()): continue 
                child = group.child(j, 0)
                data = child.data(Qt.ItemDataRole.UserRole)
                if data: 
                    files.append({
                        "node": child, 
                        "path": data['path'], 
                        "size": data['size'], 
                        "mtime": data['mtime'], 
                        "res": data.get('resolution', ""), 
                        "dur": data.get('duration', 0.0),
                        "codec": data.get('codec', ""),
                        "sharpness": data.get('sharpness', 0.0),
                        "fps": data.get('fps', 0.0),
                        "is_ref": data.get('is_ref', False) 
                    })
                
            if len(files) < 2: continue
            
            for f in files:
                if f["node"].flags() & Qt.ItemFlag.ItemIsUserCheckable:
                    f["node"].setCheckState(Qt.CheckState.Unchecked)

            refs = [f for f in files if f["is_ref"]]
            inbox = [f for f in files if not f["is_ref"]]

            if refs:
                for f in inbox: 
                    f["node"].setCheckState(Qt.CheckState.Checked)
            elif len(inbox) > 1:
                if s_idx == 0: 
                    best = max(inbox, key=lambda x: self._smart_score(x))
                elif s_idx in {1, 2}: 
                    best = max(inbox, key=lambda x: x["size"])
                elif s_idx == 3: 
                    best = min(inbox, key=lambda x: x["mtime"])
                else: 
                    best = max(inbox, key=lambda x: x["mtime"])

                for f in inbox:
                    if f["path"] != best["path"]: 
                        f["node"].setCheckState(Qt.CheckState.Checked)
                        
        self.model.blockSignals(False)
        self._update_savings()
        QApplication.restoreOverrideCursor()

    def _smart_score(self, item: dict) -> tuple:
        path = item['path']
        size_mb = item['size'] / (1024 * 1024)
        dur = item.get('dur', 0.0)
        sharpness = item.get('sharpness', 0.0)
        fps = item.get('fps', 0.0)

        area = 0
        if item.get('res') and "x" in item['res']:
            try:
                w, h = item['res'].split("x")
                area = int(w) * int(h)
            except Exception: pass

        optic_score = int(sharpness)

        codec = item.get('codec', '').lower()
        codec_mult = 1.0; codec_score = 1
        if any(c in codec for c in ['av01', 'av1']): codec_mult = 2.0; codec_score = 4
        elif any(c in codec for c in ['hev', 'hvc', 'h265']): codec_mult = 1.5; codec_score = 3
        elif any(c in codec for c in ['vp09', 'vp9']): codec_mult = 1.3; codec_score = 2
        elif any(c in codec for c in ['avc', 'h264']): codec_mult = 1.0; codec_score = 1

        density = 0.0
        if dur > 0: density = (size_mb / dur) * codec_mult
        elif area > 0: density = (size_mb / (area / 1_000_000)) * codec_mult
        else: density = size_mb
            
        eff_density = round(density, 4)

        ext = Path(path).suffix.lower()
        fmt_score = 1
        if ext in {'.raw', '.dng', '.png', '.tiff', '.mkv', '.pdf', '.cbz', '.heic'}: fmt_score = 2
        elif ext in {'.webp', '.gif', '.avi', '.m4v', '.wmv'}: fmt_score = 0

        return (
            area,                      
            optic_score,               
            round(fps, 0),             
            eff_density,               
            codec_score,               
            round(dur, 0),             
            fmt_score,                 
            -item['mtime']             
        )
        
    def _soft_delete_trigger(self):
        self._process_delete()
        
    def _hard_delete_trigger(self):
        self._process_delete()

    def _process_delete(self):
        to_del = []
        for i in range(self.model.rowCount()):
            group = self.model.item(i, 0)
            for j in range(group.rowCount()):
                child = group.child(j, 0)
                if child.checkState() == Qt.CheckState.Checked:
                    to_del.append(group.child(j, 6).text()) 
                   
        if not to_del:
            msg = "Нет файлов, отмеченных галочкой для удаления." if translator.current_lang == "ru" else "No files selected for deletion."
            title = "Пусто" if translator.current_lang == "ru" else "Empty"
            QMessageBox.information(self, title, msg)
            return
            
        dlg = VisualDeleteDialog(to_del, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if dlg.delete_hard: 
                res = BatchOperations.hard_delete(to_del)
            else: 
                res = BatchOperations.safe_delete(to_del)
                
            report = f"Успешно удалено: {res['deleted']}\nОшибок: {res['failed']}" if translator.current_lang == "ru" else f"Successfully deleted: {res['deleted']}\nFailed: {res['failed']}"
            title = "Отчет" if translator.current_lang == "ru" else "Report"
            QMessageBox.information(self, title, report)
            self._trigger_recluster()