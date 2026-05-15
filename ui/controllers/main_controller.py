# ============================================================
# MODULE: ui/controllers/main_controller.py
# ============================================================
import os
import shutil
import subprocess
import platform
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QThread, Signal, QModelIndex
from PySide6.QtGui import QShortcut, QKeySequence, QPixmap, QImageReader
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QDialog, QMenu

from utils.i18n import translator
from ui.views.multi_compare import MultiCompareDialog 
from ui.workers import MultiVideoWorker
from core.events import bus
from ui.components.dialogs import VisualDeleteDialog
from ui.components.image_label import ScalableImageLabel
from utils.logger import auditor

from core.services.fs_service import FileSystemService, SafeFSExecutor, BatchOpWorker, reveal_in_os
from core.services.auto_selector import AutoSelectWorker

from ui.controllers.selection_controller import SelectionController
from ui.controllers.preview_controller import PreviewController

class MainController(QObject):
    def __init__(self, view):
        super().__init__()
        self.view = view
        self.target_dir_a = None
        self.target_dir_b = None
        
        self.engine_ready = False
        self.is_paused = False
        self.is_stopped_requested = False
        self.engine = None
        
        self.current_status_key = "status_wait"
        self.current_status_args = []
        
        self.multi_preview_lbls = {}
        
        self.fs_service = FileSystemService(self.view.model, self)
        self.fs_service.integrity_violation_detected.connect(self._prune_dead_nodes)
        
        self.selection_controller = SelectionController(self)
        self.preview_controller = PreviewController(self)
        
        self.video_worker = MultiVideoWorker()
        self.video_worker.frame_ready.connect(self._on_worker_frame_ready)
        
        self.selection_timer = QTimer(self)
        self.selection_timer.setSingleShot(True)
        self.selection_timer.timeout.connect(self.preview_controller.process_selection)
        
        self.scan_seconds = 0
        self.scan_timer = QTimer(self)
        self.scan_timer.timeout.connect(self._update_timer_label)
        
        self._bind_signals()
        self._init_hotkeys()
        self._bind_event_bus()
        
        translator.language_changed.connect(self._retranslate_controller)
        
        self._toggle_scan_mode()
        bus.cmd_warmup_engine.emit()

    def _bind_event_bus(self):
        bus.evt_engine_ready.connect(self._on_engine_ready)
        bus.evt_scan_progress.connect(self._on_scan_progress)
        bus.evt_scan_completed.connect(self._on_scan_finished)
        bus.evt_scan_error.connect(self._on_scan_error)
        bus.evt_clustering_completed.connect(self._on_clustering_finished)
        bus.evt_telemetry_update.connect(self._on_telemetry_update)

    def _bind_signals(self):
        v = self.view
        v.directory_dropped.connect(self._on_directory_dropped)
        v.window_closed.connect(self._on_window_closed)
        v.rb_single.toggled.connect(self._toggle_scan_mode)
        v.btn_select_a.clicked.connect(lambda: self._select_directory('a'))
        v.btn_select_b.clicked.connect(lambda: self._select_directory('b'))
        v.combo_engine.currentIndexChanged.connect(self._trigger_recluster_if_engine_changes)
        v.mode_btn_group.idClicked.connect(self._sync_radio_to_slider)
        v.slider_threshold.valueChanged.connect(self._on_slider_change)
        v.slider_threshold.sliderReleased.connect(self._trigger_recluster)
        
        self.search_debounce_timer = QTimer(self)
        self.search_debounce_timer.setSingleShot(True)
        self.search_debounce_timer.setInterval(300)
        self.search_debounce_timer.timeout.connect(self._apply_view_filter)
        v.search_input.textChanged.connect(lambda: self.search_debounce_timer.start())
        v.combo_view_filter.currentIndexChanged.connect(self._apply_view_filter)
        
        v.btn_auto_select.clicked.connect(self.selection_controller.apply_auto_selection)
        v.btn_clear_select.clicked.connect(self.selection_controller.clear_selection)
        v.btn_scan.clicked.connect(self._start_scan)
        v.btn_pause.clicked.connect(self._toggle_pause)
        v.btn_stop.clicked.connect(self._stop_scan)
        
        v.btn_expand.clicked.connect(self._expand_all_safely)
        v.btn_collapse.clicked.connect(self._collapse_all_safely)
        
        v.tree.selectionModel().selectionChanged.connect(lambda: self.selection_timer.start(150))
        v.tree.doubleClicked.connect(self._on_item_double_clicked)
        v.tree.customContextMenuRequested.connect(self._on_context_menu)
        
        v.btn_grid.clicked.connect(self._trigger_grid_compare)
        v.btn_move.clicked.connect(self.selection_controller.move_trigger)
        v.btn_delete.clicked.connect(lambda: self.selection_controller.process_delete(False))
        v.multi_sync_slider.sliderReleased.connect(self._execute_multi_video_frames)
        
        v.model.itemChanged.connect(self.selection_controller.on_item_changed)

        v.btn_tab_scan.clicked.connect(lambda: v._switch_tab(0))
        v.btn_tab_analytics.clicked.connect(lambda: v._switch_tab(1))
        v.combo_lang.currentIndexChanged.connect(self._change_language)
        v.combo_theme.currentIndexChanged.connect(self._change_theme)
        v.btn_help.clicked.connect(self._show_help_dialog)

    def _init_hotkeys(self):
        self.sc_f1 = QShortcut(QKeySequence(Qt.Key.Key_F1), self.view, context=Qt.ShortcutContext.ApplicationShortcut)
        self.sc_f1.activated.connect(self._show_help_dialog)
        
        self.sc_space = QShortcut(QKeySequence(Qt.Key.Key_Space), self.view, context=Qt.ShortcutContext.ApplicationShortcut)
        self.sc_space.activated.connect(self.selection_controller.manual_check_selected)
        
        self.sc_return = QShortcut(QKeySequence(Qt.Key.Key_Return), self.view, context=Qt.ShortcutContext.ApplicationShortcut)
        self.sc_return.activated.connect(self.selection_controller.manual_check_selected)
        
        self.sc_backspace = QShortcut(QKeySequence(Qt.Key.Key_Backspace), self.view, context=Qt.ShortcutContext.ApplicationShortcut)
        self.sc_backspace.activated.connect(lambda: self.selection_controller.process_delete(False))
        
        self.sc_delete = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.view, context=Qt.ShortcutContext.ApplicationShortcut)
        self.sc_delete.activated.connect(lambda: self.selection_controller.process_delete(False))
        
        self.sc_shift_backspace = QShortcut(QKeySequence("Shift+Backspace"), self.view, context=Qt.ShortcutContext.ApplicationShortcut)
        self.sc_shift_backspace.activated.connect(lambda: self.selection_controller.process_delete(True))
        
        self.sc_shift_delete = QShortcut(QKeySequence("Shift+Delete"), self.view, context=Qt.ShortcutContext.ApplicationShortcut)
        self.sc_shift_delete.activated.connect(lambda: self.selection_controller.process_delete(True))
        
        self.sc_select_all = QShortcut(QKeySequence.StandardKey.SelectAll, self.view, context=Qt.ShortcutContext.ApplicationShortcut)
        self.sc_select_all.activated.connect(self.selection_controller.apply_auto_selection)
        
        self.sc_clear_d = QShortcut(QKeySequence("Ctrl+D"), self.view, context=Qt.ShortcutContext.ApplicationShortcut)
        self.sc_clear_d.activated.connect(self.selection_controller.clear_selection)
        
        self.sc_clear_meta = QShortcut(QKeySequence("Meta+D"), self.view, context=Qt.ShortcutContext.ApplicationShortcut)
        self.sc_clear_meta.activated.connect(self.selection_controller.clear_selection)

    def _set_status(self, key, *args):
        self.current_status_key = key
        self.current_status_args = args
        text = translator.tr(key)
        if args:
            text = text.format(*args)
        self.view.lbl_status.setText(text)

    def _retranslate_controller(self):
        if getattr(self, 'is_paused', False):
            self.view.btn_pause.setText(translator.tr("btn_resume"))
        else:
            self.view.btn_pause.setText(translator.tr("btn_pause"))
            
        if self.current_status_key:
            self._set_status(self.current_status_key, *self.current_status_args)
            
        if self.view.model.rowCount() > 0:
            self._update_statistics_panel()

    def _change_language(self, idx):
        lang = "en" if idx == 0 else "ru"
        translator.set_language(lang)

    def _change_theme(self, idx):
        from utils.theme_manager import ThemeManager
        app = QApplication.instance()
        if idx == 0: ThemeManager.apply_modern_dark(app)
        elif idx == 1: ThemeManager.apply_modern_light(app)
        else: ThemeManager.apply_system_theme(app)

    def _show_help_dialog(self):
        title = translator.tr("help_title") if translator.tr("help_title") else "Справка"
        text = translator.tr("help_text") if translator.tr("help_text") else "Горячие клавиши:\nF1 - Справка\nSpace/Enter - Выбрать файл\nDel/Backspace - Удалить\nShift+Del - Удалить безвозвратно"
        QMessageBox.information(self.view, title, text)

    def _on_telemetry_update(self, data):
        if hasattr(self.view, 'lbl_telemetry'):
            self.view.lbl_telemetry.setText(f"[NPU: {data['time']:.2f}s | RAM Peak: {data['ram_mb']:.0f}MB]")

    def _on_engine_ready(self, engine):
        if engine is None:
            self.engine_ready = False
            self.view.lbl_status.setText("NPU Initialization Failed. See Logs.")
            self._check_ready()
            return

        self.engine = engine
        self.engine_ready = True
        self._set_status("status_npu_ready")
        self._check_ready()

    def _on_directory_dropped(self, path):
        if os.path.isdir(path):
            path = os.path.normpath(os.path.abspath(path))
            self.target_dir_a = path
            self.view.lbl_path_a.setText(str(path))
            self._check_ready()
            if self.view.btn_scan.isEnabled():
                self._start_scan()

    def _on_window_closed(self):
        bus.cmd_stop_scan.emit() 
        if hasattr(self, 'video_worker') and self.video_worker.isRunning():
            if hasattr(self.video_worker, "stop"):
                self.video_worker.stop()
            self.video_worker.requestInterruption()
            self.video_worker.quit()
        self.selection_controller.cleanup_workers()

    def _expand_all_safely(self):
        if self.view.model.rowCount() > 0: self.view.tree.expandAll()
            
    def _collapse_all_safely(self):
        if self.view.model.rowCount() > 0: self.view.tree.collapseAll()

    def _trigger_recluster_if_engine_changes(self):
        if self.engine_ready and self.target_dir_a:
            if self.view.model.rowCount() > 0: self._start_scan()

    def _toggle_scan_mode(self):
        if self.view.rb_dual.isChecked():
            self.view.dir_b_widget.show()
        else:
            self.view.dir_b_widget.hide()
            self.target_dir_b = None
            self.view.lbl_path_b.setText(translator.tr("lbl_not_selected"))
        self._check_ready()

    def _select_directory(self, mode):
        folder = QFileDialog.getExistingDirectory(self.view, translator.tr("dialog_select_dir"))
        if folder: 
            folder = os.path.normpath(os.path.abspath(folder))
            if mode == 'a':
                self.target_dir_a = folder
                self.view.lbl_path_a.setText(str(folder))
            else:
                self.target_dir_b = folder
                self.view.lbl_path_b.setText(str(folder))
            self._check_ready()

    def _check_ready(self):
        if self.engine_ready:
            self.view.btn_scan.setEnabled(True)
        else:
            self.view.btn_scan.setEnabled(False)

    def _update_timer_label(self):
        if self.is_paused: return 
        self.scan_seconds += 1
        hrs = self.scan_seconds // 3600
        mins = (self.scan_seconds % 3600) // 60
        secs = self.scan_seconds % 60
        self.view.lbl_stat_time.setText(f"{hrs:02d}:{mins:02d}:{secs:02d}")

    def _update_statistics_panel(self):
        total_files = 0
        dup_count = 0
        img_stat = [0, 0, 0] 
        vid_stat = [0, 0, 0]
        doc_stat = [0, 0, 0]
        
        proxy = self.view.proxy_model
        
        for i in range(proxy.rowCount()):
            group_idx = proxy.index(i, 0)
            vis_children = proxy.rowCount(group_idx)
            if vis_children == 0: continue
            
            total_files += vis_children
            dups = vis_children - 1 if vis_children > 1 else 0
            dup_count += dups
            
            for j in range(vis_children):
                child_idx = proxy.index(j, 0, group_idx)
                src_idx = proxy.mapToSource(child_idx)
                child = self.view.model.itemFromIndex(src_idx)
                if not child: continue
                
                data = child.data(Qt.ItemDataRole.UserRole)
                if data and 'size' in data:
                    ext = Path(data['path']).suffix.lower()
                    if ext in {'.jpg', '.png', '.webp', '.bmp', '.heic', '.jpeg'}:
                        img_stat[0] += 1
                        img_stat[1] += data['size']
                        if j > 0: img_stat[2] += 1
                    elif ext in {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'}:
                        vid_stat[0] += 1
                        vid_stat[1] += data['size']
                        if j > 0: vid_stat[2] += 1
                    else:
                        doc_stat[0] += 1
                        doc_stat[1] += data['size']
                        if j > 0: doc_stat[2] += 1
        
        self.view.lbl_stat_files.setText(str(total_files))
        self.view.lbl_stat_dups.setText(str(dup_count))
        
        if total_files > 0:
            p_img = (img_stat[0] / total_files) * 100
            p_vid = (vid_stat[0] / total_files) * 100
            p_doc = (doc_stat[0] / total_files) * 100
            
            self.view.dist_container.layout().setStretch(0, int(p_img))
            self.view.dist_container.layout().setStretch(1, int(p_vid))
            self.view.dist_container.layout().setStretch(2, int(p_doc))
        else:
            p_img = p_vid = p_doc = 0
            self.view.dist_container.layout().setStretch(0, 0)
            self.view.dist_container.layout().setStretch(1, 0)
            self.view.dist_container.layout().setStretch(2, 0)
            
        self.view.leg_img_title.setText(translator.tr('chk_img'))
        self.view.leg_img_pct.setText(f"{int(p_img)}%")
        self.view.leg_img_dup.setText(f"+ {img_stat[2]}")
        self.view.leg_img_sz.setText(f"{img_stat[1] / (1024*1024):.1f} MB")

        self.view.leg_vid_title.setText(translator.tr('chk_vid'))
        self.view.leg_vid_pct.setText(f"{int(p_vid)}%")
        self.view.leg_vid_dup.setText(f"+ {vid_stat[2]}")
        self.view.leg_vid_sz.setText(f"{vid_stat[1] / (1024*1024):.1f} MB")

        self.view.leg_doc_title.setText(translator.tr('chk_doc'))
        self.view.leg_doc_pct.setText(f"{int(p_doc)}%")
        self.view.leg_doc_dup.setText(f"+ {doc_stat[2]}")
        self.view.leg_doc_sz.setText(f"{doc_stat[1] / (1024*1024):.1f} MB")
        
        pcs = "шт." if translator.current_lang == "ru" else "pcs"
        dups = "Дубли" if translator.current_lang == "ru" else "Dups"
            
        self.view.bar_img.setToolTip(f"{img_stat[0]} {pcs} | {img_stat[1] / (1024*1024):.1f} MB | {dups}: {img_stat[2]}")
        self.view.bar_vid.setToolTip(f"{vid_stat[0]} {pcs} | {vid_stat[1] / (1024*1024):.1f} MB | {dups}: {vid_stat[2]}")
        self.view.bar_doc.setToolTip(f"{doc_stat[0]} {pcs} | {doc_stat[1] / (1024*1024):.1f} MB | {dups}: {doc_stat[2]}")
            
        self._update_savings()

    def _update_savings(self):
        saved_bytes = 0
        selected_count = 0 
        proxy = self.view.proxy_model
        
        for i in range(proxy.rowCount()):
            group_idx = proxy.index(i, 0)
            for j in range(proxy.rowCount(group_idx)):
                child_idx = proxy.index(j, 0, group_idx)
                src_idx = proxy.mapToSource(child_idx)
                child = self.view.model.itemFromIndex(src_idx)
                
                if child and child.checkState() == Qt.CheckState.Checked:
                    selected_count += 1
                    data = child.data(Qt.ItemDataRole.UserRole)
                    if data and 'size' in data: saved_bytes += data['size']
                        
        self.view.lbl_stat_selected.setText(str(selected_count))
        self.view.lbl_stat_saved.setText(f"{saved_bytes / (1024*1024):.1f} MB")

    def _on_item_changed(self, item, source_index):
        if not item: return
        
        state = item.checkState()
        
        if item.is_cluster:
            for i in range(item.childCount()):
                child = item.child(i)
                if not child.raw_dict.get('is_ref', False):
                    child.check_state = state
            
            if item.childCount() > 0:
                first_child_idx = self.view.model.index(0, 0, source_index)
                last_child_idx = self.view.model.index(item.childCount()-1, 5, source_index)
                self.view.model.dataChanged.emit(first_child_idx, last_child_idx, [Qt.ItemDataRole.CheckStateRole])
                
        else:
            parent_item = item.parentItem
            if parent_item:
                all_checked = True
                any_checked = False
                for i in range(parent_item.childCount()):
                    child = parent_item.child(i)
                    if not child.raw_dict.get('is_ref', False):
                        if child.checkState() == Qt.CheckState.Checked:
                            any_checked = True
                        else:
                            all_checked = False
                
                new_parent_state = Qt.CheckState.PartiallyChecked
                if all_checked: new_parent_state = Qt.CheckState.Checked
                elif not any_checked: new_parent_state = Qt.CheckState.Unchecked
                
                if parent_item.checkState() != new_parent_state:
                    parent_item.check_state = new_parent_state
                    parent_idx = source_index.parent()
                    self.view.model.dataChanged.emit(parent_idx, parent_idx, [Qt.ItemDataRole.CheckStateRole])

        self.view.tree.viewport().update()
        self._update_savings()

    def _apply_view_filter(self):
        f_idx = self.view.combo_view_filter.currentIndex()
        s_text = self.view.search_input.text().strip()
        
        self.view.proxy_model.set_filters(f_idx, s_text)
        proxy = self.view.proxy_model
        
        for i in range(self.view.model.rowCount()):
            src_group_idx = self.view.model.index(i, 0)
            proxy_group_idx = proxy.mapFromSource(src_group_idx)
            
            group = self.view.model.itemFromIndex(src_group_idx)
            if group:
                cluster_id = group.data(Qt.ItemDataRole.UserRole).get('cluster_id', '?')
                visible_children = proxy.rowCount(proxy_group_idx) if proxy_group_idx.isValid() else 0
                
                group.itemData[0] = f"{translator.tr('cluster_prefix')} #{cluster_id} ({visible_children} {translator.tr('cluster_files')})"
                self.view.model.dataChanged.emit(src_group_idx, src_group_idx, [Qt.ItemDataRole.DisplayRole])
                
        if s_text or f_idx > 0:
            self.view.tree.expandAll()
            
        self._update_statistics_panel()

    def _manual_check_selected(self):
        if getattr(self.view, 'search_input', None) and self.view.search_input.hasFocus():
            return

        proxy_indexes = [idx for idx in self.view.tree.selectionModel().selectedRows(0) if idx.isValid()]
        indexes = [self.view.proxy_model.mapToSource(idx) for idx in proxy_indexes]
        if not indexes: return
        
        valid_items = []
        valid_indexes = []
        
        for idx in indexes:
            if idx.parent().isValid():
                item = self.view.model.itemFromIndex(idx.siblingAtColumn(0))
                if not item.raw_dict.get('is_ref', False):
                    valid_items.append(item)
                    valid_indexes.append(idx.siblingAtColumn(0))

        if not valid_items: return

        new_state = Qt.CheckState.Checked if valid_items[0].checkState() == Qt.CheckState.Unchecked else Qt.CheckState.Unchecked
        
        for child, idx in zip(valid_items, valid_indexes): 
            child.check_state = new_state
            self.view.model.dataChanged.emit(idx, idx.siblingAtColumn(5), [Qt.ItemDataRole.CheckStateRole, Qt.ItemDataRole.DisplayRole])
            self.view.model.itemChanged.emit(child, idx) 

        self.view.tree.viewport().update()
        self._update_savings()

    def _sync_radio_to_slider(self, idx):
        mapping = {0: 96, 1: 88, 2: 81}
        if idx in mapping:
            self.view.slider_threshold.blockSignals(True)
            self.view.slider_threshold.setValue(mapping[idx])
            self.view.slider_threshold.blockSignals(False)
            self.view.lbl_threshold.setText(f"{mapping[idx]}%")
            self._trigger_recluster()

    def _on_slider_change(self, v):
        self.view.lbl_threshold.setText(f"{v}%")
        if not self.view.radio_custom.isChecked():
            self.view.mode_btn_group.blockSignals(True)
            self.view.radio_custom.setChecked(True)
            self.view.mode_btn_group.blockSignals(False)

    def _trigger_recluster(self):
        if not self.engine_ready: return
            
        self._set_status("status_reclustering")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        
        threshold = 1.0 - (self.view.slider_threshold.value() / 100.0)
        bus.cmd_recluster.emit(threshold)

    def _on_clustering_finished(self, clusters):
        self.view.tree.setUpdatesEnabled(False)
        
        if hasattr(self.view.model, 'clear_data'):
            self.view.model.clear_data()
        else:
            self.view.model.clear()
            
        if not clusters:
            self.view.tree.setUpdatesEnabled(True)
            self._set_status("status_npu_ready")
            self.view.btn_scan.setEnabled(True)
            self.view.progress_bar.setValue(100)
            from PySide6.QtWidgets import QApplication, QMessageBox
            QApplication.restoreOverrideCursor()
            
            if hasattr(self, 'engine') and self.engine and len(getattr(self.engine, 'current_file_data', [])) > 0:
                from utils.i18n import translator
                title = "Сканирование завершено" if translator.current_lang == "ru" else "Scan Complete"
                msg = "Дубликаты не найдены. Попробуйте снизить порог чувствительности (Ползунок %) или выбрать другие директории." if translator.current_lang == "ru" else "No duplicates found. Try lowering the matching threshold (%) or selecting different directories."
                QMessageBox.information(self.view, title, msg)
            return

        self._start_render_tree(clusters)
        self.view.tree.setUpdatesEnabled(True)
        self.view.tree.expandAll()
        self._update_statistics_panel()
        from PySide6.QtWidgets import QApplication
        QApplication.restoreOverrideCursor()
        self._set_status("status_done")
        self.view._switch_tab(1)

    def _clear_selection(self):
        proxy = self.view.proxy_model
        for i in range(proxy.rowCount()):
            group_idx = proxy.index(i, 0)
            src_group_idx = proxy.mapToSource(group_idx)
            group = self.view.model.itemFromIndex(src_group_idx)
            if group: 
                group.check_state = Qt.CheckState.Unchecked
            
            for j in range(proxy.rowCount(group_idx)): 
                child_idx = proxy.index(j, 0, group_idx)
                src_idx = proxy.mapToSource(child_idx)
                child = self.view.model.itemFromIndex(src_idx)
                if child and not child.raw_dict.get('is_ref', False):
                    child.check_state = Qt.CheckState.Unchecked
                    
        self.view.model.dataChanged.emit(
            self.view.model.index(0, 0, QModelIndex()),
            self.view.model.index(self.view.model.rowCount()-1, 5, QModelIndex()),
            [Qt.ItemDataRole.CheckStateRole, Qt.ItemDataRole.DisplayRole]
        )
        self.view.tree.viewport().update()
        self._update_savings()

    def _on_item_double_clicked(self, proxy_index):
        index = self.view.proxy_model.mapToSource(proxy_index)
        if index.parent().isValid():
            item = self.view.model.itemFromIndex(index.siblingAtColumn(0))
            if not item.raw_dict.get('is_ref', False):
                state = Qt.CheckState.Checked if item.checkState() == Qt.CheckState.Unchecked else Qt.CheckState.Unchecked
                item.check_state = state
                self.view.model.dataChanged.emit(index.siblingAtColumn(0), index.siblingAtColumn(5), [Qt.ItemDataRole.CheckStateRole, Qt.ItemDataRole.DisplayRole])
                self.view.model.itemChanged.emit(item, index.siblingAtColumn(0))

    def _start_scan(self):
        if not self.engine_ready: 
            return
            
        is_dual = self.view.rb_dual.isChecked()
        
        if not self.target_dir_a:
            title = "Ошибка параметров" if translator.current_lang == "ru" else "Parameter Error"
            msg = "Не выбрана базовая директория для сканирования." if translator.current_lang == "ru" else "Base scan directory is not selected."
            QMessageBox.warning(self.view, title, msg)
            return
            
        if is_dual and not self.target_dir_b:
            title = "Ошибка параметров" if translator.current_lang == "ru" else "Parameter Error"
            msg = "В режиме сравнения не выбрана эталонная директория." if translator.current_lang == "ru" else "Reference directory is not selected in dual mode."
            QMessageBox.warning(self.view, title, msg)
            return

        dirs_to_scan = [self.target_dir_a]
        if is_dual: 
            dirs_to_scan.append(self.target_dir_b)

        exts = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.heic', '.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v', '.pdf', '.cbz', '.gif'}
        final_exts = set()
        if self.view.chk_img.isChecked(): final_exts.update({'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.heic', '.JPG', '.JPEG', '.PNG', '.WEBP', '.BMP', '.HEIC'})
        if self.view.chk_vid.isChecked(): final_exts.update({'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v', '.MP4', '.MOV', '.MKV', '.WEBM', '.AVI', '.M4V'})
        if self.view.chk_doc.isChecked(): final_exts.update({'.pdf', '.cbz', '.gif', '.PDF', '.CBZ', '.GIF'})
        
        if not final_exts:
            title = "Ошибка параметров" if translator.current_lang == "ru" else "Parameter Error"
            msg = "Не выбран ни один формат файлов (Фото, Видео или Документы)." if translator.current_lang == "ru" else "No file formats selected (Images, Videos, or Documents)."
            QMessageBox.warning(self.view, title, msg)
            return
            
        selected_mode = "visual" if self.view.combo_engine.currentIndex() == 0 else "faces"
        
        auditor.info(f"UI Triggered Scan Pipeline: Dirs={dirs_to_scan}, Extensions={len(final_exts)}")
        
        self.view.btn_scan.hide()
        self.view.btn_pause.show()
        self.view.btn_stop.show()
        self.view.model.clear()
        self.view.video_player.stop()
        
        self.scan_seconds = 0
        self.view.lbl_stat_time.setText("00:00:00")
        self.scan_timer.start(1000) 
        
        self.is_paused = False
        self.is_stopped_requested = False
        bus.cmd_start_scan.emit(dirs_to_scan, final_exts, selected_mode)

    def _on_scan_progress(self, current, total, msg):
        pct = int(current / total * 100) if total > 0 else 0
        self.view.lbl_status.setText(f"[{pct}%] {msg}")
        self.current_status_key = None 
        self.view.progress_bar.setValue(pct)

    def _toggle_pause(self):
        self.is_paused = not getattr(self, 'is_paused', False)
        bus.cmd_toggle_pause.emit()
        
        if self.is_paused:
            self._set_status("status_wait")
            self.view.btn_pause.setText(translator.tr("btn_resume") if translator.tr("btn_resume") else "Продолжить")
        else:
            self.current_status_key = None
            self.view.lbl_status.setText("Scanning..." if translator.current_lang == "en" else "Сканирование...")
            self.view.btn_pause.setText(translator.tr("btn_pause") if translator.tr("btn_pause") else "Пауза")

    def _stop_scan(self):
        self.is_stopped_requested = True
        bus.cmd_stop_scan.emit()
        self.view.btn_pause.hide()
        self.view.btn_stop.hide()
        self._set_status("status_stopping")
        QTimer.singleShot(1500, self._force_scan_abort)

    def _force_scan_abort(self):
        if not self.view.btn_scan.isVisible():
            self.scan_timer.stop()
            self.view.btn_scan.show()
            self.view.btn_scan.setEnabled(True)
            self._set_status("status_aborted")
            self.view.progress_bar.setValue(0)
            self.is_paused = False
            self.is_stopped_requested = False

    def _on_scan_error(self, err_msg):
        self._force_scan_abort()
        QMessageBox.critical(self.view, translator.tr("dialog_scan_error"), f"Критическая ошибка NPU:\n{err_msg}")

    def _on_scan_finished(self):
        self.scan_timer.stop()
        self.view.btn_pause.hide()
        self.view.btn_stop.hide()
        self.view.btn_scan.show()
        
        self.view.btn_pause.setText(translator.tr("btn_pause") if translator.tr("btn_pause") else "Пауза")
        
        if self.is_stopped_requested:
            self.view.btn_scan.setEnabled(True)
            self._set_status("status_aborted")
            self.view.progress_bar.setValue(0)
            self.is_stopped_requested = False
        else:
            self._trigger_recluster()

    def _start_render_tree(self, clusters):
        valid_clusters = []
        dirs_to_watch = set()
        
        ref_path = os.path.normpath(os.path.abspath(self.target_dir_a)) if self.target_dir_a else None
        
        for cluster in clusters:
            if self.view.rb_dual.isChecked() and ref_path:
                has_a = False
                has_b = False
                for it in cluster:
                    try:
                        item_path = os.path.normpath(os.path.abspath(it['path']))
                        if os.path.commonpath([ref_path, item_path]) == ref_path:
                            has_a = True
                        else:
                            has_b = True
                    except Exception as e:
                        has_b = True 
                
                if not (has_a and has_b):
                    continue 
            
            valid_clusters.append(cluster)
            for it in cluster:
                dirs_to_watch.add(str(Path(it['path']).parent))

        self.fs_service.update_watch_paths(dirs_to_watch)
        self.view.model.itemChanged.disconnect(self.selection_controller.on_item_changed)
        
        context_dir = self.target_dir_a if self.view.rb_dual.isChecked() else None
        self.view.model.set_context(context_dir)
        
        self.view.progress_bar.setValue(100)
        self._set_status("status_computing")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        
        self.view.model.set_clusters(valid_clusters)
        
        self.view.model.itemChanged.connect(self.selection_controller.on_item_changed)
        self.view.btn_scan.setEnabled(True)
        
        if self.view.model.rowCount() <= 50: 
            self.view.tree.expandAll()
            
        self._apply_view_filter()
        self._update_statistics_panel()
        self._set_status("status_done")
        QApplication.restoreOverrideCursor()

    def _prune_dead_nodes(self, processed_paths):
        paths_set = set(processed_paths) if isinstance(processed_paths, (list, set)) else {processed_paths}
        self.view.model.remove_paths(paths_set)
        
        self.view.preview_stack.setCurrentIndex(0)
        self.view.single_preview_label.clear_view()
        self._apply_view_filter()
        self._update_statistics_panel()

    def _on_context_menu(self, pos):
        proxy_index = self.view.tree.indexAt(pos)
        if not proxy_index.isValid(): return
        index = self.view.proxy_model.mapToSource(proxy_index)
        
        item = self.view.model.itemFromIndex(index.siblingAtColumn(0))
        menu = QMenu(self.view)
        if not index.parent().isValid():
            menu.addAction(translator.tr("ctx_select_inbox"), lambda i=item, idx=index: self.selection_controller.set_group_check_state(i, idx, Qt.CheckState.Checked))
            menu.addAction(translator.tr("btn_compare"), self._trigger_grid_compare)
        else:
            if not item.raw_dict.get('is_ref', False):
                menu.addAction(translator.tr("ctx_toggle"), self.selection_controller.manual_check_selected)
            menu.addAction(translator.tr("btn_compare"), self._trigger_grid_compare)
            
            data = item.data(Qt.ItemDataRole.UserRole)
            if data and isinstance(data, dict):
                path = data['path']
                from core.services.fs_service import reveal_in_os
                menu.addAction(translator.tr("ctx_reveal"), lambda p=path: reveal_in_os(p))
            
        menu.setStyleSheet("QMenu { background-color: #2B2D31; color: white; border: 1px solid #4E5058; } QMenu::item:selected { background-color: #5865F2; }")
        menu.exec(self.view.tree.viewport().mapToGlobal(pos))

    def _execute_multi_video_frames(self):
        if self.multi_preview_lbls:
            self.video_worker.request_frames(list(self.multi_preview_lbls.keys()), self.view.multi_sync_slider.value())
            
    def _on_worker_frame_ready(self, path, qimg):
        if path in self.multi_preview_lbls:
            if not qimg.isNull():
                self.multi_preview_lbls[path].setPixmap(QPixmap.fromImage(qimg))
            else:
                self.multi_preview_lbls[path].clear_view()

    def _trigger_grid_compare(self):
        proxy_indexes = [idx for idx in self.view.tree.selectionModel().selectedRows(0) if idx.isValid()]
        indexes = [self.view.proxy_model.mapToSource(idx) for idx in proxy_indexes]
        sel = [idx for idx in indexes if idx.parent().isValid()]
        
        if len(sel) <= 1:
            proxy_idx = self.view.tree.currentIndex()
            if not proxy_idx.isValid(): return
            idx = self.view.proxy_model.mapToSource(proxy_idx)
            item = self.view.model.itemFromIndex(idx.siblingAtColumn(0))
            gr = item if item.parent() is None else item.parent()
            pts = []
            for i in range(gr.childCount()):
                data = gr.child(i).data(Qt.ItemDataRole.UserRole)
                if data and isinstance(data, dict):
                    pts.append(data['path'])
        else:
            pts = []
            for idx in sel:
                data = self.view.model.itemFromIndex(idx.siblingAtColumn(0)).data(Qt.ItemDataRole.UserRole)
                if data and isinstance(data, dict):
                    pts.append(data['path'])
            
        if len(pts) < 2: return
        dlg = MultiCompareDialog(pts, self.view)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if dlg.files_to_delete:
                res = None
                if dlg.delete_hard: 
                    res = SafeFSExecutor.hard_delete(dlg.files_to_delete)
                else: 
                    res = SafeFSExecutor.safe_delete(dlg.files_to_delete)
                
                if res and res.get("error") == "send2trash_missing":
                    QMessageBox.critical(
                        self.view, 
                        translator.tr("dialog_del_report_title"),
                        "Ошибка: Модуль 'send2trash' не найден. Удаление в корзину невозможно."
                    )
                
                self._prune_dead_nodes(dlg.files_to_delete)