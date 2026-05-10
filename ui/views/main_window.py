# ============================================================
# MODULE: ui/views/main_window.py
# ============================================================
import os
import shutil
import time
from pathlib import Path
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QLabel, QProgressBar, QComboBox, QLineEdit, QRadioButton, 
                             QButtonGroup, QHeaderView, QSplitter, QScrollArea, 
                             QApplication, QCheckBox, QFrame, QStackedWidget, QGridLayout, 
                             QSizePolicy, QAbstractItemView, QMessageBox)
from PySide6.QtCore import Qt, QSettings, Signal, QSortFilterProxyModel, QModelIndex
from PySide6.QtGui import QStandardItemModel

from utils.theme_manager import ThemeManager
from utils.i18n import translator
from utils.env_config import get_app_data_dir, get_cache_dir
from utils.logger import auditor
from core.db.vector_cache import VectorCache

from ui.components.video_player import BuiltInVideoPlayer, JumpSlider
from ui.components.media_tree import MediaTreeView, LazyClusterModel
from ui.components.image_label import ScalableImageLabel


class ArbitrageSortFilterProxyModel(QSortFilterProxyModel):
    """ Аппаратный прокси-слой для математически точной сортировки и скрытия узлов """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.filter_type = 0 
        self.search_text = ""

    def set_filters(self, f_type: int, text: str):
        # БЛОКИРОВКА холостой инвалидации кэша. Защищает QTreeView от сброса стейта при удалении узлов.
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
                    try: w, h = map(int, str(r).split('x')); return w * h
                    except: return 0
                return get_area(left_data.get('res', '')) < get_area(right_data.get('res', ''))
            elif col == 5: 
                return float(left_data.get('mtime', 0.0)) < float(right_data.get('mtime', 0.0))
        except Exception:
            pass

        return super().lessThan(left, right)


class MainWindow(QMainWindow):
    directory_dropped = Signal(str)
    window_closed = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Tensor Media Arbitrage v1.0")
        self.resize(1450, 900)
        self.settings = QSettings("TensorMedia", "ArbitrageConfig")
        
        self._setup_ui()
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

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            self.directory_dropped.emit(urls[0].toLocalFile())

    def closeEvent(self, event):
        self.settings.setValue("master_splitter", self.master_splitter.saveState())
        self.settings.setValue("inner_splitter", self.inner_splitter.saveState())
        self.window_closed.emit()
        super().closeEvent(event)

    def _switch_tab(self, index: int):
        self.tabs.setCurrentIndex(index)
        self.btn_tab_scan.setChecked(index == 0)
        self.btn_tab_analytics.setChecked(index == 1)

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
        self.sidebar_widget.setFixedWidth(310) 
        
        sidebar_layout = QVBoxLayout(self.sidebar_widget)
        sidebar_layout.setContentsMargins(10, 10, 10, 10) 
        sidebar_layout.setSpacing(12)

        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(0, 0, 0, 0)
        top_bar.setSpacing(8)
        
        self.combo_theme = QComboBox()
        self.combo_theme.addItems(["🌙 Dark", "☀️ Light", "💻 Sys"])
        self.combo_theme.setFixedHeight(34) 
        self.combo_theme.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        
        self.combo_lang = QComboBox()
        self.combo_lang.addItems(["🇬🇧 EN", "🇷🇺 RU"])
        
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
            font-size: 14px; font-weight: bold;
        }
        QPushButton:checked {
            background-color: #5865F2; color: white; border: none;
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
        
        mode_l = QHBoxLayout()
        mode_l.setSpacing(15) 
        self.rb_single = QRadioButton()
        self.rb_dual = QRadioButton()
        self.rb_single.setChecked(True)
        mode_l.addWidget(self.rb_single)
        mode_l.addWidget(self.rb_dual)
        dir_l.addLayout(mode_l)

        dir_a_l = QHBoxLayout()
        self.btn_select_a = QPushButton()
        self.btn_select_a.setObjectName("secondary")
        self.btn_select_a.setMinimumWidth(110) 
        self.btn_select_a.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
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
        self.btn_select_b.setMinimumWidth(110) 
        self.btn_select_b.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        dir_b_l.addWidget(self.btn_select_b)
        self.lbl_path_b = QLabel()
        self.lbl_path_b.setObjectName("elide_label")
        dir_b_l.addWidget(self.lbl_path_b, stretch=1)
        dir_l.addWidget(self.dir_b_widget)
        self.dir_b_widget.hide()
        
        type_l = QHBoxLayout()
        type_l.setSpacing(15) 
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
        cal_l.setContentsMargins(14, 14, 14, 14) 
        cal_l.setSpacing(12) 
        
        engine_mode_l = QHBoxLayout()
        self.lbl_engine_mode = QLabel()
        engine_mode_l.addWidget(self.lbl_engine_mode)
        
        self.combo_engine = QComboBox()
        self.combo_engine.setMinimumWidth(180) 
        self.combo_engine.setFixedHeight(34)
        self.combo_engine.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.combo_engine.addItems(["", ""]) 
        engine_mode_l.addWidget(self.combo_engine)
        cal_l.addLayout(engine_mode_l)

        self.lbl_threshold_title = QLabel()
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
        self.slider_threshold = JumpSlider(Qt.Orientation.Horizontal)
        self.slider_threshold.setRange(50, 100)
        self.slider_threshold.setValue(88)
        self.lbl_threshold = QLabel("88%")
        self.lbl_threshold.setFixedWidth(40)
        self.lbl_threshold.setAlignment(Qt.AlignmentFlag.AlignRight)
        slider_l.addWidget(self.slider_threshold)
        slider_l.addWidget(self.lbl_threshold)
        cal_l.addLayout(slider_l)
        config_l.addWidget(cal_card)

        self.btn_toggle_db = QPushButton()
        self.btn_toggle_db.setObjectName("secondary")
        self.btn_toggle_db.setCheckable(True)
        self.btn_toggle_db.setFixedHeight(34)
        config_l.addWidget(self.btn_toggle_db)

        self.db_card = QWidget()
        self.db_card.setObjectName("card")
        db_l = QVBoxLayout(self.db_card)
        db_l.setContentsMargins(14, 14, 14, 14)
        db_l.setSpacing(12)
        
        self.lbl_db_info = QLabel()
        self.lbl_db_info.setWordWrap(True)
        self.lbl_db_info.setStyleSheet("color: #949BA4; font-size: 11px;")
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
            lbl_title.setStyleSheet("font-size: 11px; font-weight: bold; color: #DCDDDE;")
            hdr_l.addWidget(c)
            hdr_l.addWidget(lbl_title)
            hdr_l.addStretch()
            
            lbl_pct = QLabel("0%")
            lbl_pct.setStyleSheet("font-size: 11px; color: #949BA4;")
            lbl_dup = QLabel("0")
            lbl_dup.setStyleSheet("font-size: 11px; color: #949BA4;")
            lbl_sz = QLabel("0.0 MB")
            lbl_sz.setStyleSheet("font-size: 11px; color: #949BA4;")
            
            legend_l.addWidget(hdr_w, 0, col_idx)
            legend_l.addWidget(lbl_pct, 1, col_idx)
            legend_l.addWidget(lbl_dup, 2, col_idx)
            legend_l.addWidget(lbl_sz, 3, col_idx)
            
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
        filter_l.addWidget(self.lbl_filter_title)
        
        self.search_input = QLineEdit()
        self.search_input.setFixedHeight(34)
        filter_l.addWidget(self.search_input)

        f_box = QHBoxLayout()
        self.lbl_filter_type = QLabel()
        f_box.addWidget(self.lbl_filter_type)
        self.combo_view_filter = QComboBox()
        self.combo_view_filter.setFixedHeight(34)
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
        mark_l.addWidget(self.lbl_mark_title)
        
        self.combo_strategy = QComboBox()
        self.combo_strategy.setFixedHeight(34)
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
        self.lbl_status = QLabel()
        self.lbl_status.setObjectName("status")
        self.lbl_status.setWordWrap(True)
        
        self.lbl_telemetry = QLabel()
        self.lbl_telemetry.setObjectName("telemetry_hud")
        self.lbl_telemetry.setStyleSheet("color: #5865F2; font-size: 11px; font-weight: bold;")
        self.lbl_telemetry.setAlignment(Qt.AlignmentFlag.AlignRight)
        
        status_hud.addWidget(self.lbl_status, stretch=1)
        status_hud.addWidget(self.lbl_telemetry)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(6) 
        self.progress_bar.setTextVisible(False)
        
        scan_status_layout.addLayout(status_hud)
        scan_status_layout.addWidget(self.progress_bar)
        sidebar_layout.addLayout(scan_status_layout)
        
        scan_controls = QHBoxLayout()
        scan_controls.setSpacing(10)
        self.btn_scan = QPushButton()
        self.btn_scan.setObjectName("primary")
        self.btn_scan.setMinimumHeight(46)
        self.btn_scan.setEnabled(False)
        
        self.btn_pause = QPushButton()
        self.btn_pause.setObjectName("secondary")
        self.btn_pause.setMinimumHeight(46)
        self.btn_pause.hide()
        
        self.btn_stop = QPushButton()
        self.btn_stop.setObjectName("secondary")
        self.btn_stop.setMinimumHeight(46)
        self.btn_stop.setStyleSheet("QPushButton { background-color: #DA3633; border: none; color: white; }") 
        self.btn_stop.hide()
        
        self.scan_controls_layout = scan_controls
        self.scan_controls_layout.addWidget(self.btn_scan)
        self.scan_controls_layout.addWidget(self.btn_pause)
        self.scan_controls_layout.addWidget(self.btn_stop)
        sidebar_layout.addLayout(self.scan_controls_layout)
        
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
        ms_layout.addWidget(QLabel("🎥"))
        ms_layout.addWidget(self.multi_sync_slider)
        multi_layout.addWidget(self.multi_slider_panel)
        self.multi_slider_panel.hide()
        
        self.preview_stack.addWidget(multi_widget)
        inspector_layout.addWidget(self.preview_stack, stretch=1)

        bottom_btns = QWidget()
        bottom_btns.setFixedHeight(64) 
        bottom_btns.setObjectName("bottom_btns")
        bb_layout = QHBoxLayout(bottom_btns)
        bb_layout.setContentsMargins(12, 12, 12, 12)
        bb_layout.setSpacing(10)
        
        self.btn_grid = QPushButton()
        self.btn_grid.setMinimumHeight(40)
        self.btn_grid.setObjectName("secondary")
        
        self.btn_move = QPushButton()
        self.btn_move.setMinimumHeight(40)
        self.btn_move.setObjectName("action")
        
        self.btn_delete = QPushButton()
        self.btn_delete.setMinimumHeight(40)
        self.btn_delete.setObjectName("primary")
        self.btn_delete.setStyleSheet("QPushButton { background-color: #DA3633; }") 
        
        bb_layout.addWidget(self.btn_grid)
        bb_layout.addWidget(self.btn_move)
        bb_layout.addWidget(self.btn_delete)
        inspector_layout.addWidget(bottom_btns)

        self.inner_splitter.addWidget(tree_container)
        self.inner_splitter.addWidget(self.inspector_frame)
        self.inner_splitter.setStretchFactor(0, 4)
        self.inner_splitter.setStretchFactor(1, 6)
        
        self.master_splitter.addWidget(content_widget)
        self.master_splitter.setStretchFactor(0, 0)
        self.master_splitter.setStretchFactor(1, 1)

        self.master_splitter.splitterMoved.connect(lambda: self.tree._trigger_stretch())
        self.inner_splitter.splitterMoved.connect(lambda: self.tree._trigger_stretch())

    def _toggle_sidebar(self):
        is_visible = self.sidebar_widget.isVisible()
        self.sidebar_widget.setVisible(not is_visible)
        if is_visible:
            self.btn_toggle_sidebar.setText(translator.tr("btn_sidebar_show"))
        else:
            self.master_splitter.setSizes([310, self.width() - 310])
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
        sp_state = self.settings.value("master_splitter")
        if sp_state: self.master_splitter.restoreState(sp_state)
        isp_state = self.settings.value("inner_splitter")
        if isp_state: self.inner_splitter.restoreState(isp_state)

    def _retranslate_ui(self):
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
            translator.tr("col_res"), translator.tr("col_time")
        ])
        
        header = self.tree.header()
        header.setStretchLastSection(False) 
        header.setMinimumSectionSize(50)
        
        for i in range(6):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)

        lang = getattr(translator, 'current_lang', 'en')
        is_ru = (lang == 'ru')

        self.btn_toggle_db.setText("📦 Управление ядром данных" if is_ru else "📦 Data Core Management")
        self.lbl_db_info.setText(
            "Очистка матриц FAISS и SQLite-векторов. Потребуется полный рескан файлов." if is_ru 
            else "Clears FAISS matrices and SQLite vectors. Requires full rescan."
        )
        self.btn_clear_faiss.setText("Очистить FAISS" if is_ru else "Clear FAISS")
        self.btn_clear_faiss.setToolTip(
            "Удаляет только матрицы связей на диске" if is_ru 
            else "Deletes only relationship matrices on disk"
        )
        self.btn_purge_db.setText("Сброс SQLite" if is_ru else "Purge SQLite")
        self.btn_purge_db.setToolTip(
            "Физическое удаление всех файлов БД и журналов" if is_ru 
            else "Physical deletion of all DB files and logs"
        )

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
        path = get_app_data_dir() / "faiss_cache"
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            msg = "Матрицы FAISS успешно очищены" if translator.current_lang == "ru" else "FAISS matrices successfully cleared"
            self.lbl_status.setText(msg)
            auditor.info("UI: FAISS cache manually purged")

    def _purge_all_data(self):
        lang = getattr(translator, 'current_lang', 'en')
        is_ru = (lang == 'ru')
        
        title = "Критическое действие" if is_ru else "Critical Action"
        msg = "Это удалит ВСЕ вычисленные векторы SQLite и графы FAISS. Продолжить?" if is_ru else "This will delete ALL computed SQLite vectors and FAISS graphs. Continue?"
        
        reply = QMessageBox.warning(
            self, title, msg, 
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            self._purge_faiss() 
            try:
                cache_dir = get_cache_dir()
                for mode in ["visual", "faces"]:
                    base_name = f"meta_v2_{mode}.db"
                    for ext in ["", "-wal", "-shm"]:
                        db_file = cache_dir / f"{base_name}{ext}"
                        if db_file.exists():
                            db_file.unlink() 
                
                auditor.info("UI: Physical DB purge executed.")
                self.lbl_status.setText("Ядро данных физически уничтожено" if is_ru else "Data Core physically destroyed")
            except Exception as e:
                auditor.error(f"UI DB File Delete error: {e}")
                self.lbl_status.setText(f"Ошибка удаления БД: {e}" if is_ru else f"DB Deletion Error: {e}")

    def update_telemetry_hud(self, elapsed_seconds: float, ram_mb: float):
        try:
            m, s = divmod(int(elapsed_seconds), 60)
            h, m = divmod(m, 60)
            sub_s = int((elapsed_seconds - int(elapsed_seconds)) * 10)
            time_str = f"{h:02d}:{m:02d}:{s:02d}.{sub_s}"
            self.lbl_telemetry.setText(f"⏱ {time_str} | 🧠 {ram_mb:.1f} MB")
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