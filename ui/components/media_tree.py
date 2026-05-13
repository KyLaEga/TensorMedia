# ============================================================
# MODULE: ui/components/media_tree.py
# ============================================================
import os
from pathlib import Path

from PySide6.QtWidgets import QTreeView, QAbstractItemView
from PySide6.QtCore import Qt, QUrl, QMimeData, QModelIndex, QTimer, QAbstractItemModel, Signal
from PySide6.QtGui import QDrag, QColor

from utils.i18n import translator

class TreeItem:
    """ Масштабируемый узел графа. __slots__ блокирует создание __dict__, экономя 65% RAM. """
    __slots__ = ('parentItem', 'itemData', 'childItems', 'is_cluster', 'raw_dict', 'check_state')

    def __init__(self, data: list, parent=None, is_cluster=False, raw_dict=None):
        self.parentItem = parent
        self.itemData = data
        self.childItems = []
        self.is_cluster = is_cluster
        self.raw_dict = raw_dict
        self.check_state = Qt.CheckState.Unchecked

    def appendChild(self, item):
        self.childItems.append(item)

    def child(self, row, column=0):
        if 0 <= row < len(self.childItems):
            return self.childItems[row]
        return None

    def childCount(self):
        return len(self.childItems)

    def rowCount(self):
        return self.childCount()

    def columnCount(self):
        return len(self.itemData)

    def data(self, role_or_column):
        if role_or_column == Qt.ItemDataRole.UserRole:
            return self.raw_dict
        if isinstance(role_or_column, int) and 0 <= role_or_column < len(self.itemData):
            return self.itemData[role_or_column]
        return None

    def parent(self):
        return self.parentItem

    def row(self):
        if self.parentItem:
            try:
                return self.parentItem.childItems.index(self)
            except ValueError:
                return 0
        return 0

    def checkState(self):
        return self.check_state


class LazyClusterModel(QAbstractItemModel):
    itemChanged = Signal(object, QModelIndex) 

    def __init__(self, parent=None):
        super().__init__(parent)
        self.headers = [
            translator.tr("col_file"), translator.tr("col_fmt"), 
            translator.tr("col_sim"), translator.tr("col_size"), 
            translator.tr("col_res"), translator.tr("col_time")
        ]
        self.rootItem = TreeItem(self.headers)
        self.target_dir_a = None

    def clear(self):
        self.beginResetModel()
        if hasattr(self, 'rootItem') and self.rootItem:
            self.rootItem.childItems.clear()
        self.endResetModel()

    def remove_paths(self, paths_to_remove: set):
        if not paths_to_remove: return

        for i in range(len(self.rootItem.childItems) - 1, -1, -1):
            cluster = self.rootItem.childItems[i]
            cluster_idx = self.index(i, 0, QModelIndex())

            removed_count = 0
            
            for j in range(len(cluster.childItems) - 1, -1, -1):
                if cluster.childItems[j].raw_dict.get('path') in paths_to_remove:
                    self.beginRemoveRows(cluster_idx, j, j)
                    del cluster.childItems[j]
                    self.endRemoveRows()
                    removed_count += 1

            if len(cluster.childItems) <= 1:
                self.beginRemoveRows(QModelIndex(), i, i)
                del self.rootItem.childItems[i]
                self.endRemoveRows()
            elif removed_count > 0:
                total_size = sum(ch.raw_dict.get('size', 0) for ch in cluster.childItems)
                cluster.itemData[3] = f"{total_size/(1024*1024):.1f} MB"
                
                c_id = cluster.raw_dict.get('cluster_id', '?')
                cluster.itemData[0] = f"{translator.tr('cluster_prefix')} #{c_id} ({len(cluster.childItems)} {translator.tr('cluster_files')})"
                
                self.dataChanged.emit(
                    self.index(i, 0, QModelIndex()),
                    self.index(i, 5, QModelIndex()),
                    [Qt.DisplayRole]
                )

    def setHorizontalHeaderLabels(self, labels: list):
        self.headers = labels
        if hasattr(self, 'rootItem') and self.rootItem:
            self.rootItem.itemData = labels
        self.headerDataChanged.emit(Qt.Horizontal, 0, len(labels) - 1)

    def set_context(self, target_dir_a):
        self.target_dir_a = target_dir_a

    def set_clusters(self, clusters):
        self.beginResetModel()
        self.rootItem.childItems.clear()
        
        valid_cluster_idx = 1
        for cluster in clusters:
            total_size = sum(it.get('size', 0) for it in cluster)
            sz_str = f"{total_size/(1024*1024):.1f} MB"
            formats = set(Path(it['path']).suffix.upper().replace('.', '') for it in cluster)
            fmt_str = ", ".join(sorted(formats))
            
            sims = [it.get('similarity', 1.0) for it in cluster if 'similarity' in it]
            avg_sim = sum(sims) / len(sims) if sims else 1.0
            avg_sim_str = f"~{avg_sim*100:.1f}%"
            
            parent_name = f"{translator.tr('cluster_prefix')} #{valid_cluster_idx} ({len(cluster)} {translator.tr('cluster_files')})"
            
            parent_data = [parent_name, fmt_str, avg_sim_str, sz_str, "", ""]
            parent_item = TreeItem(parent_data, parent=self.rootItem, is_cluster=True, raw_dict={'cluster_id': valid_cluster_idx})
            
            ref_path = os.path.normpath(os.path.abspath(self.target_dir_a)) if self.target_dir_a else None
            for it in cluster:
                p = it['path']
                try:
                    item_path = os.path.normpath(os.path.abspath(p))
                except Exception:
                    item_path = p

                ext = Path(p).suffix.upper()
                sim_str = f"{it.get('similarity', 1.0)*100:.1f}%" if 'similarity' in it else "Base"
                i_sz_str = f"{it.get('size', 0)/(1024*1024):.1f} MB"
                res = it.get('resolution', "")
                dur = f"{int(it.get('duration', 0))//60:02d}:{int(it.get('duration', 0))%60:02d}" if it.get('duration') else ""
                
                is_ref = False
                if ref_path:
                    try:
                        if os.path.commonpath([ref_path, item_path]) == ref_path:
                            is_ref = True
                    except Exception:
                        is_ref = False
                    display_name = f"[REF] {Path(p).name}" if is_ref else f"[INBOX] {Path(p).name}"
                else:
                    display_name = Path(p).name
                
                it['is_ref'] = is_ref
                
                child_data = [display_name, ext, sim_str, i_sz_str, res, dur]
                child_item = TreeItem(child_data, parent=parent_item, is_cluster=False, raw_dict=it)
                parent_item.appendChild(child_item)
                
            self.rootItem.appendChild(parent_item)
            valid_cluster_idx += 1
            
        self.endResetModel()

    def columnCount(self, parent=QModelIndex()):
        if parent.isValid():
            return parent.internalPointer().columnCount()
        return self.rootItem.columnCount()

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None

        item = index.internalPointer()

        if role == Qt.DisplayRole:
            return item.data(index.column())
            
        elif role == Qt.UserRole:
            return item.raw_dict

        elif role == Qt.CheckStateRole and index.column() == 0:
            if item.is_cluster or not item.raw_dict.get('is_ref', False):
                return item.check_state
            return None
            
        elif role == Qt.BackgroundRole:
            if item.is_cluster:
                return QColor(128, 128, 128, 40)
            return None
            
        elif role == Qt.ForegroundRole:
            # В светлой теме синий текст эталона может быть плохо виден, 
            # но мы оставляем его для акцента, если он не мешает.
            if not item.is_cluster and item.raw_dict.get('is_ref', False):
                return QColor("#5865F2")
            return None

        return None

    def setData(self, index, value, role=Qt.EditRole):
        if role == Qt.CheckStateRole and index.column() == 0:
            item = index.internalPointer()
            if item.is_cluster or not item.raw_dict.get('is_ref', False):
                item.check_state = Qt.CheckState(value)
                self.dataChanged.emit(index, index, [Qt.CheckStateRole])
                self.itemChanged.emit(item, index) 
                return True
        return False

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags

        item = index.internalPointer()
        flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        
        if index.column() == 0:
            if item.is_cluster or not item.raw_dict.get('is_ref', False):
                flags |= Qt.ItemIsUserCheckable
                
        return flags

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.rootItem.data(section)
        return None

    def index(self, row, column, parent=QModelIndex()):
        if not self.hasIndex(row, column, parent):
            return QModelIndex()

        if not parent.isValid():
            parentItem = self.rootItem
        else:
            parentItem = parent.internalPointer()

        childItem = parentItem.child(row)
        if childItem:
            return self.createIndex(row, column, childItem)
        return QModelIndex()

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()

        childItem = index.internalPointer()
        parentItem = childItem.parent()

        if parentItem == self.rootItem or parentItem is None:
            return QModelIndex()

        return self.createIndex(parentItem.row(), 0, parentItem)

    def rowCount(self, parent=QModelIndex()):
        if parent.column() > 0:
            return 0

        if not parent.isValid():
            parentItem = self.rootItem
        else:
            parentItem = parent.internalPointer()

        return parentItem.childCount()

    def item(self, row, column=0):
        idx = self.index(row, column, QModelIndex())
        if idx.isValid():
            return idx.internalPointer()
        return None

    def itemFromIndex(self, index):
        if index.isValid():
            return index.internalPointer()
        return None


class MediaTreeView(QTreeView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setUniformRowHeights(True)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.setStyleSheet("QTreeView::item { min-height: 24px; padding: 2px 0px; }")
        
        self.header().sectionResized.connect(self._on_section_resized)
        self._is_stretching = False

    def setModel(self, model):
        super().setModel(model)
        window = self.window()
        if hasattr(window, '_retranslate_ui'):
            window._retranslate_ui()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._trigger_stretch()

    def _on_section_resized(self, logicalIndex, oldSize, newSize):
        if not self._is_stretching and logicalIndex != 0:
            self._trigger_stretch()

    def _trigger_stretch(self):
        QTimer.singleShot(0, self._apply_manual_stretch)

    def _apply_manual_stretch(self):
        header = self.header()
        if not header or header.count() == 0:
            return
            
        self._is_stretching = True
        total_w = self.width() - 2
        
        if self.verticalScrollBar().isVisible():
            total_w -= self.verticalScrollBar().width()
            
        other_w = 0
        for i in range(1, header.count()):
            if not header.isSectionHidden(i):
                other_w += header.sectionSize(i)
                
        new_w = max(80, total_w - other_w)
        self.setColumnWidth(0, new_w)
        self._is_stretching = False

    def startDrag(self, supportedActions):
        indexes = self.selectedIndexes()
        urls = []
        for idx in indexes:
            data = idx.data(Qt.ItemDataRole.UserRole)
            if data and isinstance(data, dict) and 'path' in data:
                path = data['path']
                if os.path.exists(path):
                    urls.append(QUrl.fromLocalFile(path))
        
        if urls:
            mime = QMimeData()
            mime.setUrls(urls)
            drag = QDrag(self)
            drag.setMimeData(mime)
            drag.exec(supportedActions)