from PySide6.QtCore import QObject, Qt, QModelIndex
from PySide6.QtWidgets import QApplication, QMessageBox, QFileDialog, QDialog
from utils.i18n import translator
from core.services.auto_selector import AutoSelectWorker
from core.services.fs_service import SafeFSExecutor, BatchOpWorker
from ui.components.dialogs import VisualDeleteDialog

class SelectionController(QObject):
    def __init__(self, main_controller):
        super().__init__()
        self.main_controller = main_controller
        self.view = main_controller.view
        self.auto_worker = None
        self.del_worker = None
        self.move_worker = None

    def on_item_changed(self, item, source_index):
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
        self.main_controller._update_savings()

    def manual_check_selected(self):
        if getattr(self.view, 'search_input', None) and self.view.search_input.hasFocus():
            return

        proxy_indexes = [idx for idx in self.view.tree.selectionModel().selectedRows(0) if idx.isValid()]
        indexes = [self.view.proxy_model.mapToSource(idx) for idx in proxy_indexes]
        if not indexes: return

        # Space переключает галочку удаления у выделенных строк. РАНЬШЕ в расчёт
        # шли только файлы (дети) — выделенный кластер пробел игнорировал. Теперь
        # делим выделение на кластеры (группы) и файлы: для кластера пробел метит
        # сразу всю группу (все её не-эталонные файлы), для файлов — как прежде.
        cluster_items = []        # выделенные группы целиком
        file_items = []           # (item, source_index) выбранных пофайлово
        for idx in indexes:
            item = self.view.model.itemFromIndex(idx.siblingAtColumn(0))
            if item is None:
                continue
            if item.is_cluster:
                cluster_items.append(item)
            elif not item.raw_dict.get('is_ref', False):
                file_items.append((item, idx.siblingAtColumn(0)))

        if not cluster_items and not file_items:
            return

        # Единое целевое состояние на весь пакет: инвертируем «ведущий» элемент
        # (приоритет у кластера, иначе первый файл), чтобы один пробел не
        # «расходился» по разным состояниям внутри смешанного выделения.
        lead = cluster_items[0] if cluster_items else file_items[0][0]
        new_state = (Qt.CheckState.Checked
                     if lead.checkState() == Qt.CheckState.Unchecked
                     else Qt.CheckState.Unchecked)

        self.view.model.blockSignals(True)
        for cluster in cluster_items:
            cluster.check_state = new_state
            for i in range(cluster.childCount()):
                child = cluster.child(i)
                if not child.raw_dict.get('is_ref', False):
                    child.check_state = new_state
        for child, _ in file_items:
            child.check_state = new_state
        self.view.model.blockSignals(False)

        # Пакет мог затронуть несколько групп (несколько кластеров и/или файлы из
        # разных кластеров) — перерисовываем дерево целиком одним сигналом (тот же
        # приём, что в clear_selection/_apply_computed_selection; QTreeView
        # перерисует лишь видимую область).
        self.view.model.dataChanged.emit(
            self.view.model.index(0, 0, QModelIndex()),
            self.view.model.index(self.view.model.rowCount() - 1, 5, QModelIndex()),
            [Qt.ItemDataRole.CheckStateRole, Qt.ItemDataRole.DisplayRole]
        )

        # Доводим tristate родителей у файлов, выбранных пофайлово (у кластеров
        # состояние уже согласовано — все дети равны самому кластеру).
        for child, idx in file_items:
            self.on_item_changed(child, idx)

        self.view.tree.viewport().update()
        self.main_controller._update_savings()

    def clear_selection(self):
        # КРИТИЧЕСКИЙ ПАТЧ: Запрет сброса выбора при наборе текста в поиске
        if getattr(self.view, 'search_input', None) and self.view.search_input.hasFocus():
            return
            
        proxy = self.view.proxy_model
        self.view.model.blockSignals(True)
        
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
                    
        self.view.model.blockSignals(False)
        self.view.model.dataChanged.emit(
            self.view.model.index(0, 0, QModelIndex()),
            self.view.model.index(self.view.model.rowCount()-1, 5, QModelIndex()),
            [Qt.ItemDataRole.CheckStateRole, Qt.ItemDataRole.DisplayRole]
        )
        self.view.tree.viewport().update()
        self.main_controller._update_savings()

    def set_group_check_state(self, item, group_idx, state):
        for i in range(item.childCount()):
            child = item.child(i)
            if not child.raw_dict.get('is_ref', False):
                child.check_state = state
                
        self.view.model.dataChanged.emit(
            self.view.model.index(0, 0, group_idx),
            self.view.model.index(item.childCount()-1, 5, group_idx),
            [Qt.ItemDataRole.CheckStateRole, Qt.ItemDataRole.DisplayRole]
        )
        self.view.tree.viewport().update()
        self.main_controller._update_savings()

    def apply_auto_selection(self):
        s_idx = self.view.combo_strategy.currentIndex()
        self.view.btn_auto_select.setEnabled(False)
        self.main_controller._set_status("status_computing")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        clusters_data = []
        proxy = self.view.proxy_model
        
        for i in range(proxy.rowCount()):
            cluster = []
            group_idx = proxy.index(i, 0)
            for j in range(proxy.rowCount(group_idx)):
                child_idx = proxy.index(j, 0, group_idx)
                src_idx = proxy.mapToSource(child_idx)
                child = self.view.model.itemFromIndex(src_idx)
                if child:
                    data = child.data(Qt.ItemDataRole.UserRole)
                    if data: cluster.append(data)
            if len(cluster) > 1:
                clusters_data.append(cluster)

        self.auto_worker = AutoSelectWorker(clusters_data, s_idx, self)
        self.auto_worker.finished.connect(self._apply_computed_selection)
        self.auto_worker.start()

    def _apply_computed_selection(self, paths_to_check):
        check_set = set(paths_to_check)
        
        self.view.model.blockSignals(True) 
        self.view.tree.setUpdatesEnabled(False)
        
        proxy = self.view.proxy_model
        for i in range(proxy.rowCount()):
            group_idx = proxy.index(i, 0)
            for j in range(proxy.rowCount(group_idx)):
                child_idx = proxy.index(j, 0, group_idx)
                src_idx = proxy.mapToSource(child_idx)
                child = self.view.model.itemFromIndex(src_idx)
                if child and not child.raw_dict.get('is_ref', False):
                    data = child.data(Qt.ItemDataRole.UserRole)
                    if data and data['path'] in check_set:
                        child.check_state = Qt.CheckState.Checked
                    else:
                        child.check_state = Qt.CheckState.Unchecked

        self.view.model.blockSignals(False)
        self.view.tree.setUpdatesEnabled(True)
        self.view.model.dataChanged.emit(
            self.view.model.index(0, 0, QModelIndex()),
            self.view.model.index(self.view.model.rowCount()-1, 5, QModelIndex()),
            [Qt.ItemDataRole.CheckStateRole, Qt.ItemDataRole.DisplayRole]
        )    
        self.view.tree.viewport().update()        
        self.main_controller._update_savings()
        
        QApplication.restoreOverrideCursor()
        self.view.btn_auto_select.setEnabled(True)
        self.main_controller._set_status("status_done")

    def move_trigger(self):
        to_act = []
        proxy = self.view.proxy_model
        for i in range(proxy.rowCount()):
            group_idx = proxy.index(i, 0)
            for j in range(proxy.rowCount(group_idx)):
                child_idx = proxy.index(j, 0, group_idx)
                src_idx = proxy.mapToSource(child_idx)
                child = self.view.model.itemFromIndex(src_idx)
                
                if child and child.checkState() == Qt.CheckState.Checked:
                    data = child.data(Qt.ItemDataRole.UserRole)
                    if data and isinstance(data, dict):
                        to_act.append(data['path'])
                    
        if not to_act: return
        dest = QFileDialog.getExistingDirectory(self.view, translator.tr("dialog_move_title"))
        if dest:
            self.view.btn_move.setEnabled(False)
            self.main_controller._set_status("status_computing")
            
            self.move_worker = BatchOpWorker(SafeFSExecutor.move_files, to_act, dest)
            def _on_move_done(res):
                self.main_controller._prune_dead_nodes(to_act)
                report = translator.tr("dialog_move_report").format(moved=res['moved'], failed=res['failed'])
                QMessageBox.information(self.view, translator.tr("dialog_move_done"), report)
                self.view.btn_move.setEnabled(True)
                self.main_controller._set_status("status_done")
                
            self.move_worker.finished.connect(_on_move_done)
            self.move_worker.start()

    def process_delete(self, default_hard=False):
        # КРИТИЧЕСКИЙ ПАТЧ: Защита от случайного удаления при стирании текста в поиске кнопкой Backspace
        if getattr(self.view, 'search_input', None) and self.view.search_input.hasFocus():
            return
            
        to_del = []
        proxy = self.view.proxy_model
        
        for i in range(proxy.rowCount()):
            group_idx = proxy.index(i, 0)
            for j in range(proxy.rowCount(group_idx)):
                child_idx = proxy.index(j, 0, group_idx)
                src_idx = proxy.mapToSource(child_idx)
                child = self.view.model.itemFromIndex(src_idx)
                
                if child and child.checkState() == Qt.CheckState.Checked:
                    data = child.data(Qt.ItemDataRole.UserRole)
                    if data and isinstance(data, dict):
                        to_del.append(data['path'])
                   
        if not to_del:
            QMessageBox.information(self.view, translator.tr("dialog_del_empty_title"), translator.tr("dialog_del_empty_msg"))
            return
            
        dlg = VisualDeleteDialog(to_del, self.view)
        if default_hard: dlg.rb_hard.setChecked(True) if hasattr(dlg, 'rb_hard') else None
        
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.view.btn_delete.setEnabled(False)
            self.main_controller._set_status("status_computing")
            
            func = SafeFSExecutor.hard_delete if dlg.delete_hard else SafeFSExecutor.safe_delete
            
            self.del_worker = BatchOpWorker(func, to_del)
            def _on_del_done(res):
                self.main_controller._prune_dead_nodes(to_del)
                
                if isinstance(res, dict) and res.get("error") == "send2trash_missing":
                    QMessageBox.critical(
                        self.view,
                        translator.tr("dialog_del_report_title"),
                        translator.tr("err_send2trash_missing")
                    )
                elif res.get("failed", 0) > 0:
                    report = translator.tr("dialog_del_report_msg").format(deleted=res['deleted'], failed=res['failed'])
                    msg = f"{report}\n\n{translator.tr('warn_trash_external')}"
                    QMessageBox.warning(self.view, translator.tr("dialog_del_report_title"), msg)
                else:
                    report = translator.tr("dialog_del_report_msg").format(deleted=res['deleted'], failed=res['failed'])
                    QMessageBox.information(self.view, translator.tr("dialog_del_report_title"), report)
                
                self.view.btn_delete.setEnabled(True)
                self.main_controller._set_status("status_done")
                
            self.del_worker.finished.connect(_on_del_done)
            self.del_worker.start()
            
    def cleanup_workers(self):
        for worker in [self.auto_worker, self.del_worker, self.move_worker]:
            if worker and worker.isRunning():
                if hasattr(worker, "stop"):
                    worker.stop()
                worker.requestInterruption()
                worker.quit()