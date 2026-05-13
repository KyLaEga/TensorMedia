import os
from pathlib import Path
from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QPixmap, QImageReader
from ui.components.image_label import ScalableImageLabel

class PreviewController(QObject):
    """Отвечает за рендеринг одиночного и множественного превью (фото, видео, документы)."""
    
    def __init__(self, main_controller):
        super().__init__()
        self.main_controller = main_controller
        self.view = main_controller.view

    def render_preview(self, p):
        self.view.video_player.stop()
        if not os.path.exists(p): 
            self.view.preview_stack.setCurrentIndex(0)
            self.view.single_preview_label.clear_view()
            return
        ext = Path(p).suffix.lower()
        if ext in {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'}:
            self.view.preview_stack.setCurrentIndex(1)
            self.view.video_player.load_video(p)
        else:
            self.view.preview_stack.setCurrentIndex(0)
            if ext == '.gif':
                from PySide6.QtGui import QMovie
                movie = QMovie(p)
                movie.setCacheMode(QMovie.CacheMode.CacheAll)
                self.view.single_preview_label.setMovie(movie)
            elif ext in {'.pdf', '.cbz'}:
                self.view.single_preview_label.load_document(p)
            else:
                # Асинхронная загрузка обычных изображений
                self.view.single_preview_label.load_image(p)

    def render_multi_preview(self, paths):
        self.view.video_player.stop()
        self.view.preview_stack.setCurrentIndex(2)
        
        # Очистка сетки
        for r in reversed(range(self.view.multi_grid.count())):
            item_at = self.view.multi_grid.itemAt(r)
            if item_at:
                w = item_at.widget()
                if w:
                    w.deleteLater()
                self.view.multi_grid.removeItem(item_at)
            
        for r in range(50): self.view.multi_grid.setRowStretch(r, 0)
            
        count = len(paths)
        if count == 0: return
        
        if count <= 2: cols = 1
        elif count <= 4: cols = 2
        elif count <= 9: cols = 3
        elif count <= 16: cols = 4
        else: cols = 5
            
        self.main_controller.multi_preview_lbls.clear() 
        video_paths = []
        has_v = False
        
        for i, p in enumerate(paths):
            lbl = ScalableImageLabel() 
            
            ext = Path(p).suffix.lower()
            if ext in {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v', '.gif', '.cbz', '.pdf'}:
                has_v = True
                video_paths.append(p)
                self.main_controller.multi_preview_lbls[p] = lbl
            else:
                # Асинхронная загрузка для каждого элемента в сетке
                lbl.load_image(p)
            
            self.view.multi_grid.addWidget(lbl, i // cols, i % cols)
            
        if has_v: 
            self.view.multi_slider_panel.show()
            self.main_controller._execute_multi_video_frames()
        else: 
            self.view.multi_slider_panel.hide()

    def process_selection(self):
        proxy_indexes = [idx for idx in self.view.tree.selectionModel().selectedRows(0) if idx.isValid()]
        indexes = [self.view.proxy_model.mapToSource(idx) for idx in proxy_indexes]
        
        if not indexes:
            self.view.preview_stack.setCurrentIndex(0)
            self.view.single_preview_label.clear_view()
            return

        if len(indexes) == 1 and not indexes[0].parent().isValid():
            proxy_group_idx = proxy_indexes[0]
            paths = []
            
            for i in range(self.view.proxy_model.rowCount(proxy_group_idx)):
                proxy_child_idx = self.view.proxy_model.index(i, 0, proxy_group_idx)
                src_child_idx = self.view.proxy_model.mapToSource(proxy_child_idx)
                
                child = self.view.model.itemFromIndex(src_child_idx)
                if child:
                    data = child.data(Qt.ItemDataRole.UserRole)
                    if data and isinstance(data, dict):
                        paths.append(data['path'])
                        
            if len(paths) > 1 or (len(paths) == 1 and Path(paths[0]).suffix.lower() in {'.cbz', '.pdf'}):
                self.render_multi_preview(paths)
            elif len(paths) == 1:
                self.render_preview(paths[0])
            return

        sel = [idx for idx in indexes if idx.parent().isValid()]
        
        if len(sel) > 1:
            paths = []
            for idx in sel:
                data = self.view.model.itemFromIndex(idx.siblingAtColumn(0)).data(Qt.ItemDataRole.UserRole)
                if data and isinstance(data, dict):
                    paths.append(data['path'])
            self.render_multi_preview(paths) 
        elif len(sel) == 1: 
            data = self.view.model.itemFromIndex(sel[0].siblingAtColumn(0)).data(Qt.ItemDataRole.UserRole)
            if data and isinstance(data, dict):
                p = data['path']
                if Path(p).suffix.lower() in {'.cbz', '.pdf'}: self.render_multi_preview([p])
                else: self.render_preview(p)
        else: 
            self.view.preview_stack.setCurrentIndex(0)
            self.view.single_preview_label.clear_view()