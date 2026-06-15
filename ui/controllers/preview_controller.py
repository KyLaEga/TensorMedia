import os
from pathlib import Path
from PySide6.QtCore import QObject, Qt
from ui.components.image_label import ScalableImageLabel

# Дублирует discrete_preview.DISCRETE_EXTS НАМЕРЕННО: импорт модуля Контура Б
# тянет PIL на верхнем уровне и сломал бы ленивую загрузку (модуль импортируется
# только внутри view.ensure_discrete_preview, при первом реальном обращении).
DISCRETE_EXTS = {'.gif', '.pdf', '.cbz'}

# Статические растры, которые в ПАРНОМ выборе обслуживает Контур Б: пара фото
# получает адаптивную ориентацию по AR, а индексатор кадров скрывается сам
# (total == 1 → панель невидима). Одиночное фото остаётся на странице 0.
STATIC_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}


class PreviewController(QObject):
    """Маршрутизатор бифуркационного движка предпросмотра.

    Контур А (видеопоток, страница 1 стека): QMediaPlayer + QVideoSink с
    дросселированным real-time скраббингом — BuiltInVideoPlayer, создаётся
    лениво через view.ensure_video_player() при первом выборе видео.

    Контур Б (дискретный, страница 3): GIF/PDF/CBZ листаются целочисленным
    индексом кадра/страницы без плеера — DiscreteScrubbingWidget, создаётся
    лениво через view.ensure_discrete_preview(). Одиночный файл и пара (1×2)
    обслуживаются одним и тем же виджетом с общим слайдером.

    Статика (страница 0) и cv2-сетка 3+ файлов (страница 2) — без изменений.
    """

    # Видео-расширения — те же, что у MultiCompareWidget.VIDEO_EXTS.
    _VIDEO_EXTS = {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'}

    def __init__(self, main_controller):
        super().__init__()
        self.main_controller = main_controller
        self.view = main_controller.view

    def _stop_video(self):
        # Плеер ленивый: до первого видео его нет — глушить нечего.
        if self.view.video_player is not None:
            self.view.video_player.stop()

    def render_preview(self, p):
        self._stop_video()
        if not os.path.exists(p):
            self.view.preview_stack.setCurrentIndex(0)
            self.view.single_preview_label.clear_view()
            return

        ext = Path(p).suffix.lower()
        if ext in self._VIDEO_EXTS:
            # Контур А. setCurrentIndex ПОСЛЕ load_video: смена страницы стека
            # шлёт hideEvent предыдущей (дискретный контур закрывает хэндлы).
            self.view.ensure_video_player().load_video(p)
            self.view.preview_stack.setCurrentIndex(1)
        elif ext in DISCRETE_EXTS:
            # Контур Б: мгновенное индексное листание кадров/страниц.
            self.view.ensure_discrete_preview().load([p])
            self.view.preview_stack.setCurrentIndex(3)
        else:
            self.view.preview_stack.setCurrentIndex(0)
            self.view.single_preview_label.load_image(p)

    def render_discrete_pair(self, paths):
        """Пакетный режим Контура Б: сетка 1×2 с единым слайдером индекса."""
        self._stop_video()
        self.view.ensure_discrete_preview().load(paths[:2])
        self.view.preview_stack.setCurrentIndex(3)

    def render_multi_preview(self, paths):
        self._stop_video()
        self.view.preview_stack.setCurrentIndex(2)

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

        pair_lead = None   # ведущее ВИДЕО пары для реактивной доводки оси
        if count == 1:
            cols = 1
        elif count == 2:
            # Адаптивная ориентация пары: ось по AR ПЕРВОГО файла. Для РАСТРОВОГО
            # лида probe_media_ar отдаёт AR сразу (заголовок, безопасно в UI). Для
            # ВИДЕО-лида он возвращает None — синхронный cv2 в GUI-потоке ЗАПРЕЩЁН
            # (парс MOOV гигабайтного ролика вешал поток). Тогда стартуем в cols=1
            # и доводим ось РЕАКТИВНО по первому кадру из воркера (см.
            # MainController._maybe_adjust_multi_orientation).
            from utils.media_probe import probe_media_ar
            ar = probe_media_ar(paths[0])
            if ar is not None:
                cols = 2 if ar < 1.0 else 1            # растровый лид — ось готова
            else:
                cols = 1                                # видео-лид — дефолт + реактив
                if Path(paths[0]).suffix.lower() in self._VIDEO_EXTS:
                    pair_lead = paths[0]
        elif count <= 4: cols = 2
        elif count <= 9: cols = 3
        elif count <= 16: cols = 4
        else: cols = 5

        mc = self.main_controller
        mc.multi_preview_lbls.clear()
        # Состояние реактивной доводки сетки (см. _maybe_adjust_multi_orientation):
        # ячейки в порядке вставки + текущее число колонок + ведущее видео. Лид не
        # видео (pair_lead is None) → доводка отключена (ось уже верна синхронно).
        mc._multi_grid_items = []
        mc._multi_grid_cols = cols
        mc._multi_pair_lead = pair_lead
        mc._multi_orientation_applied = pair_lead is None
        has_v = False

        for i, p in enumerate(paths):
            lbl = ScalableImageLabel()

            ext = Path(p).suffix.lower()
            if ext in self._VIDEO_EXTS or ext in DISCRETE_EXTS:
                has_v = True
                mc.multi_preview_lbls[p] = lbl
            else:
                lbl.load_image(p)

            mc._multi_grid_items.append(lbl)
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

            if len(paths) > 1:
                self._route_multi(paths)
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
            # ТИХАЯ ПОДГОТОВКА (Quiet State Preparation). Выбор файлов в дереве
            # НЕ угоняет экран на страницу сравнения: переход на синхронное
            # видеосравнение выполняет только явное действие («Сравнить» /
            # Cmd+P / контекстное меню → _trigger_grid_compare). Здесь — только
            # пассивные контуры: дискретная пара 1×2 либо статичная cv2-сетка.
            self._route_multi(paths)
        elif len(sel) == 1:
            data = self.view.model.itemFromIndex(sel[0].siblingAtColumn(0)).data(Qt.ItemDataRole.UserRole)
            if data and isinstance(data, dict):
                self.render_preview(data['path'])
        else:
            self.view.preview_stack.setCurrentIndex(0)
            self.view.single_preview_label.clear_view()

    def _route_multi(self, paths):
        """Ровно два дискретных/статических файла → синхронная пара Контура Б
        (адаптивная ориентация по AR, единый индекс; для пары фото индексатор
        скрыт); любой другой состав → cv2-сетка с %-слайдером."""
        pair_exts = DISCRETE_EXTS | STATIC_EXTS
        if len(paths) == 2 and all(Path(p).suffix.lower() in pair_exts for p in paths):
            self.render_discrete_pair(paths)
        else:
            self.render_multi_preview(paths)
