import os
from pathlib import Path
from PyQt6.QtWidgets import QTreeView, QAbstractItemView
from PyQt6.QtCore import Qt, QUrl, QMimeData
from PyQt6.QtGui import QStandardItem, QDrag

class SortableStandardItem(QStandardItem):
    def __init__(self, text, sort_val=None):
        super().__init__(str(text))
        self.sort_val = sort_val if sort_val is not None else text

    def __lt__(self, other):
        if isinstance(other, SortableStandardItem):
            try: return float(self.sort_val) < float(other.sort_val)
            except: return str(self.sort_val) < str(other.sort_val)
        return super().__lt__(other)

class MediaTreeView(QTreeView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setUniformRowHeights(True)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setStyleSheet("QTreeView::item { min-height: 24px; padding: 2px 0px; }")

    def startDrag(self, supportedActions):
        indexes = self.selectedIndexes()
        urls = []
        for idx in indexes:
            if idx.column() == 6: 
                path = idx.data()
                if path and os.path.exists(path):
                    urls.append(QUrl.fromLocalFile(path))
        
        if urls:
            mime = QMimeData()
            mime.setUrls(urls)
            drag = QDrag(self)
            drag.setMimeData(mime)
            drag.exec(supportedActions)