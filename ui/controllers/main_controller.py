# ============================================================
# MODULE: ui/controllers/main_controller.py
# ============================================================
import os
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QElapsedTimer
from PySide6.QtGui import QShortcut, QKeySequence, QPixmap
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QMenu

from utils.i18n import translator
from utils.theme_manager import ThemeManager
from ui.workers import MultiVideoWorker
from core.events import bus
from utils.logger import auditor

from core.services.fs_service import FileSystemService, SafeFSExecutor, BatchOpWorker, reveal_in_os

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

        # True только на recluster, который запущен СРАЗУ после завершённого
        # скана (см. _on_scan_finished). Нужен, чтобы алерт «файлы не найдены»
        # не выскакивал при холостом дёрганье ползунка порога до первого скана,
        # когда current_file_data пуст просто потому, что скан ещё не запускался.
        self._scan_just_completed = False

        # Idempotency Guard: последнее значение порога (%), по которому реально
        # отработала FAISS-рекластеризация. Холостые финализации слайдера/инпута
        # (клик без смещения, повторный Enter) сверяются с ним и отсекаются до
        # эмиссии cmd_recluster. None — пересчёта ещё не было, первый пройдёт.
        # Обновляется ИСКЛЮЧИТЕЛЬНО внутри _trigger_recluster (после прохождения
        # engine_ready), поэтому скан и смена движка тоже держат кэш свежим.
        self._last_applied_threshold = None

        # Балансировка override-курсора рекластеризации. _trigger_recluster ставит
        # WaitCursor, но оркестратор может ОТБРОСИТЬ команду (воркер ещё занят) или
        # упасть по OOM (эмитит evt_scan_error вместо evt_clustering_completed) —
        # тогда курсор не на чем снять, и при быстром дёрганье ползунка каждый
        # сброшенный пересчёт стэкал бы ещё один WaitCursor (залипшие песочные
        # часы). Флаг гарантирует, что курсор поставлен РОВНО один раз и снимается
        # ровно один раз на любом терминальном событии (готово/ошибка).
        self._recluster_cursor_active = False

        self.current_status_key = "status_wait"
        self.current_status_args = []
        
        self.multi_preview_lbls = {}
        # Активные воркеры удаления из окна сравнения. Набор, а НЕ одиночный
        # атрибут: повторное «Применить» до завершения предыдущего удаления раньше
        # затирало бы единственную Python-ссылку на ЖИВОЙ QThread (его C++-деструктор
        # на работающем потоке = Abort trap: 6). Каждый воркер держится здесь до
        # своего finished, где снимается и уходит в deleteLater.
        self._compare_del_workers = set()
        # Реактивная доводка ориентации cv2-сетки видео-пары (Grid Reactive
        # Layout). probe_media_ar для видео-лида отдаёт None → стартуем в cols=1,
        # а ось доводим по первому кадру воркера (см. _maybe_adjust_multi_orientation).
        # Инициализируем здесь, чтобы слот кадра не падал AttributeError до первой
        # сборки сетки. _multi_orientation_applied=True = доводка обезоружена.
        self._multi_grid_items = []
        self._multi_grid_cols = 1
        self._multi_pair_lead = None
        self._multi_orientation_applied = True

        self.fs_service = FileSystemService(self.view.model, self)
        self.fs_service.integrity_violation_detected.connect(self._prune_dead_nodes)
        
        self.selection_controller = SelectionController(self)
        self.preview_controller = PreviewController(self)
        
        self.video_worker = MultiVideoWorker()
        self.video_worker.frame_ready.connect(self._on_worker_frame_ready)

        # Затвор живого скраба мультипревью — паритет с панелью сравнения и
        # одиночным плеером (33 мс ≈ 30 Гц): чаще слать запросы cv2-декодеру
        # бессмысленно, очередь воркера лишь копила бы устаревшие позиции.
        self._multi_scrub_clock = QElapsedTimer()
        self._multi_scrub_clock.start()
        self._last_multi_scrub_ms = 0
        
        self.selection_timer = QTimer(self)
        self.selection_timer.setSingleShot(True)
        self.selection_timer.timeout.connect(self.preview_controller.process_selection)

        # Re-entrancy guard входа в сравнение: True от старта отложенного teardown
        # (release_source_safe ждёт destroyed старого плеера) до фактической смены
        # слоя в _finish_compare_switch. Без него пачка быстрых Enter/Cmd+P
        # укладывала несколько циклов сноса-пересоздания медиа-контекста друг на
        # друга → GIL/WindowServer-фриз при повторных заходах в сравнение.
        self._compare_switch_pending = False

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
        v.slider_threshold.sliderReleased.connect(self._on_slider_released)
        # Клавиатурный пин порога: фиксация по Enter/потере фокуса. Двусторонняя
        # связь: ввод числа двигает ползунок и радиокнопки (пресет или «Своя»),
        # после чего автоматически запускается пересчёт FAISS-выборки.
        v.lbl_threshold.value_committed.connect(self._on_threshold_committed)
        
        self.search_debounce_timer = QTimer(self)
        self.search_debounce_timer.setSingleShot(True)
        self.search_debounce_timer.setInterval(300)
        self.search_debounce_timer.timeout.connect(self._apply_view_filter)
        v.search_input.textChanged.connect(lambda: self.search_debounce_timer.start())
        v.combo_view_filter.currentIndexChanged.connect(self._apply_view_filter)
        
        v.btn_auto_select.clicked.connect(self.selection_controller.apply_auto_selection)
        v.btn_clear_select.clicked.connect(self.selection_controller.clear_selection)
        # Cmd+D / Ctrl+D больше не привязан к этой кнопке: шорткат живёт в
        # нативном QMenuBar ("Правка" → "Снять выделение") и вызывает
        # tree.clearSelection. Кнопка btn_clear_select — отдельный сброс галочек.
        v.btn_scan.clicked.connect(self._start_scan)
        v.btn_pause.clicked.connect(self._toggle_pause)
        v.btn_stop.clicked.connect(self._stop_scan)
        
        v.btn_expand.clicked.connect(self._expand_all_safely)
        v.btn_collapse.clicked.connect(self._collapse_all_safely)
        
        v.tree.selectionModel().selectionChanged.connect(lambda: self.selection_timer.start(150))
        v.tree.doubleClicked.connect(self._on_item_double_clicked)
        v.tree.customContextMenuRequested.connect(self._on_context_menu)
        
        v.btn_grid.clicked.connect(self._trigger_grid_compare)
        # Страница сравнения ПОСТОЯННА (создаётся один раз в MainWindow._setup_ui,
        # index 1 стека). Сигналы подключаются здесь ровно один раз: «Назад»/Esc →
        # стек на index 0; «Применить» → читаем выбор и удаляем файлы. Виджет
        # больше НЕ пересоздаётся (отказ от Destroy & Rebuild ради macOS).
        v.compare_widget.compare_cancelled.connect(lambda: v.root_stack.setCurrentIndex(0))
        v.compare_widget.compare_confirmed.connect(self._on_compare_confirmed)

        # Нативные шорткаты Правки (Cmd+F/O/P) живут как QAction в QMenuBar
        # (MainWindow._setup_menubar). Подключаем их триггеры здесь:
        #   Cmd+F — снять синее выделение строк дерева (НЕ трогая чекбоксы);
        #   Cmd+O — перенос отмеченных файлов; Cmd+P — сравнение выбранных.
        v.action_clear_sel.triggered.connect(self.view.tree.clearSelection)
        v.action_move.triggered.connect(self.selection_controller.move_trigger)
        v.action_compare.triggered.connect(self._trigger_grid_compare)
        v.btn_move.clicked.connect(self.selection_controller.move_trigger)
        v.btn_delete.clicked.connect(lambda: self.selection_controller.process_delete(False))
        # Транспорт мультипревью — раскладка сигналов как в «Сравнении»:
        #   sliderMoved    — живой скраб: дросселированные cv2-кадры под пальцем;
        #   sliderReleased — отпускание ИЛИ клик-прыжок JumpSlider: финальный
        #                    запрос без троттла (последняя позиция гарантирована).
        v.multi_sync_slider.sliderMoved.connect(self._scrub_multi_video_frames)
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
        
        # BLOCK 4 (эргономика навигации). Space И двойной клик ПЕРЕКЛЮЧАЮТ чекбокс
        # удаления (пакетно по всему выделению — manual_check_selected), а Enter
        # ОТКРЫВАЕТ сравнение (наравне с Cmd+P / кнопкой / контекстным меню).
        self.sc_space = QShortcut(QKeySequence(Qt.Key.Key_Space), self.view, context=Qt.ShortcutContext.ApplicationShortcut)
        self.sc_space.activated.connect(self.selection_controller.manual_check_selected)

        self.sc_return = QShortcut(QKeySequence(Qt.Key.Key_Return), self.view, context=Qt.ShortcutContext.ApplicationShortcut)
        self.sc_return.activated.connect(self._open_compare_shortcut)
        self.sc_enter = QShortcut(QKeySequence(Qt.Key.Key_Enter), self.view, context=Qt.ShortcutContext.ApplicationShortcut)
        self.sc_enter.activated.connect(self._open_compare_shortcut)
        
        self.sc_backspace = QShortcut(QKeySequence(Qt.Key.Key_Backspace), self.view, context=Qt.ShortcutContext.ApplicationShortcut)
        self.sc_backspace.activated.connect(lambda: self.selection_controller.process_delete(False))
        
        self.sc_delete = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.view, context=Qt.ShortcutContext.ApplicationShortcut)
        self.sc_delete.activated.connect(lambda: self.selection_controller.process_delete(False))
        
        self.sc_shift_backspace = QShortcut(QKeySequence("Shift+Backspace"), self.view, context=Qt.ShortcutContext.ApplicationShortcut)
        self.sc_shift_backspace.activated.connect(lambda: self.selection_controller.process_delete(True))
        
        self.sc_shift_delete = QShortcut(QKeySequence("Shift+Delete"), self.view, context=Qt.ShortcutContext.ApplicationShortcut)
        self.sc_shift_delete.activated.connect(lambda: self.selection_controller.process_delete(True))

        # Cmd+F / Cmd+O / Cmd+P БОЛЬШЕ НЕ висят на QShortcut: на macOS QShortcut
        # ненадёжен и не отображается в меню/строке статуса. Теперь это QAction
        # в нативном QMenuBar (MainWindow._setup_menubar), а их сигналы подключены
        # в _bind_signals (Cmd+F → tree.clearSelection, Cmd+O → move_trigger,
        # Cmd+P → _trigger_grid_compare). Держать второй QShortcut на тех же
        # клавишах нельзя — Qt сделал бы привязку ambiguous и не сработал бы ни один.

        # Шорткаты выделения НЕ занимаем здесь во избежание ambiguous-привязок:
        # у каждого ровно один владелец — QAction в нативном меню (MainWindow).
        # Cmd+A → "Выбрать дубликаты" (apply_auto_selection через btn_auto_select),
        # Cmd+V → "Выбрать все" (tree.selectAll). Раньше тут висел второй QShortcut
        # на Cmd+A, из-за чего Qt срабатывал неотличимо.
        #
        # Ctrl/Cmd+D is owned by MainWindow (clears the tree's row selection).
        # Keeping a second Ctrl+D here would make the shortcut ambiguous and Qt
        # would fire neither reliably, so it lives in exactly one place.

    def _set_status(self, key, *args):
        self.current_status_key = key
        self.current_status_args = args
        text = translator.tr(key)
        if args:
            text = text.format(*args)
        self.view.lbl_status.setText(text)

    def _retranslate_controller(self):
        if getattr(self, 'is_paused', False):
            self.view.update_pause_label(translator.tr("btn_resume"))
        else:
            self.view.update_pause_label(translator.tr("btn_pause"))
            
        if self.current_status_key:
            self._set_status(self.current_status_key, *self.current_status_args)
            
        if self.view.model.rowCount() > 0:
            self._update_statistics_panel()

    def _change_language(self, idx):
        lang = "en" if idx == 0 else "ru"
        translator.set_language(lang)

    def _change_theme(self, idx):
        app = QApplication.instance()
        if idx == 0: ThemeManager.apply_modern_dark(app)
        elif idx == 1: ThemeManager.apply_modern_light(app)
        else: ThemeManager.apply_system_theme(app)
        # Палитра уже сменилась — перекрасим леттербокс плеера под новую тему,
        # иначе видеовиджет останется с фоном предыдущей темы. Плеер ленивый:
        # до первого видео его не существует и красить нечего.
        if self.view.video_player is not None:
            self.view.video_player.apply_theme()

    def _show_help_dialog(self):
        title = translator.tr("help_title") if translator.tr("help_title") else "Справка"
        text = translator.tr("help_text") if translator.tr("help_text") else "Горячие клавиши:\nF1 - Справка\nSpace/Enter - Выбрать файл\nCmd+F - Снять выделение строк\nCmd+O - Перенос файлов\nCmd+P - Сравнить выбранные\nDel/Backspace - Удалить\nShift+Del - Удалить безвозвратно"
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
        # ГАШЕНИЕ FSEvents-НАБЛЮДАТЕЛЯ ДО os._exit. watchdog.Observer — это живой
        # нативный поток с FSEvents-колбэком; если его не остановить, при сносе
        # интерпретатора колбэк дёргает PyGILState_Ensure → new_threadstate уже
        # по освобождённому runtime-состоянию и роняет процесс в SIGSEGV
        # (EXC_BAD_ACCESS, faultingThread = watchdog_FSEventStreamCallback —
        # подтверждено crash-репортом). stop()/join() рвут поток и FSEvents-стрим,
        # пока интерпретатор ещё жив. Синхронно в GUI-потоке, как остальной teardown.
        fs_service = getattr(self, "fs_service", None)
        if fs_service is not None:
            fs_service.stop()
        if hasattr(self, 'video_worker') and self.video_worker.isRunning():
            if hasattr(self.video_worker, "stop"):
                self.video_worker.stop()
            self.video_worker.requestInterruption()
            self.video_worker.quit()
        # Ленивый плеер: до первого видео его нет (view.video_player is None).
        # Его фоновый cv2-воркер превью (_thumb_worker) висит в cond.wait() и не
        # охвачен прочим teardown — глушим ограниченно (флаг + notify + wait(2000))
        # через player.shutdown(), иначе он остаётся орфаном до os._exit.
        if self.view.video_player is not None:
            self.view.video_player.shutdown()
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
            
        total_label = "Файлов" if translator.current_lang == "ru" else "Files"

        self.view.leg_img_title.setText(translator.tr('chk_img'))
        self.view.leg_img_pct.setText(f"{int(p_img)}%")
        self.view.leg_img_dup.setText(f"{total_label}: {img_stat[0]}")
        self.view.leg_img_sz.setText(f"{img_stat[1] / (1024*1024):.1f} MB")

        self.view.leg_vid_title.setText(translator.tr('chk_vid'))
        self.view.leg_vid_pct.setText(f"{int(p_vid)}%")
        self.view.leg_vid_dup.setText(f"{total_label}: {vid_stat[0]}")
        self.view.leg_vid_sz.setText(f"{vid_stat[1] / (1024*1024):.1f} MB")

        self.view.leg_doc_title.setText(translator.tr('chk_doc'))
        self.view.leg_doc_pct.setText(f"{int(p_doc)}%")
        self.view.leg_doc_dup.setText(f"{total_label}: {doc_stat[0]}")
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

    # Пресеты порога: id радиокнопки -> значение %. Единый источник истины для
    # обеих сторон синхронизации (радио → пин/ползунок и ввод числа → радио).
    THRESHOLD_PRESETS = {0: 96, 1: 88, 2: 81}

    def _sync_radio_to_slider(self, idx):
        mapping = self.THRESHOLD_PRESETS
        if idx in mapping:
            self.view.slider_threshold.blockSignals(True)
            self.view.slider_threshold.setValue(mapping[idx])
            self.view.slider_threshold.blockSignals(False)
            self.view.lbl_threshold.set_value(mapping[idx])
            self._trigger_recluster()

    def _sync_presets_to_value(self, value: int):
        """Ввод числа руками: совпало с пресетом — включаем его радиокнопку,
        иначе переводим группу в состояние «Своя»."""
        group = self.view.mode_btn_group
        group.blockSignals(True)
        preset_id = next(
            (rid for rid, v in self.THRESHOLD_PRESETS.items() if v == value), None
        )
        if preset_id is not None:
            group.button(preset_id).setChecked(True)
        else:
            self.view.radio_custom.setChecked(True)
        group.blockSignals(False)

    def _on_slider_released(self, *args):
        """Финализация мышью. Если пин не сдвинулся (клик по дорожке без
        перетаскивания, повторное отпускание на том же значении) — ползунок
        уже стоит на _last_applied_threshold, и тяжёлый пересчёт пропускается."""
        if self.view.slider_threshold.value() == self._last_applied_threshold:
            return
        self._trigger_recluster()

    def _on_threshold_committed(self, value: int):
        self.view.slider_threshold.blockSignals(True)
        self.view.slider_threshold.setValue(value)
        self.view.slider_threshold.blockSignals(False)
        self._sync_presets_to_value(value)
        # Idempotency Guard для клавиатурного пина: повторный Enter без смены
        # числа не должен будить FAISS. Сверка после setValue, т.к. кэш хранит
        # значение в тех же единицах (%), что и слайдер.
        if value == self._last_applied_threshold:
            return
        self._trigger_recluster()

    def _on_slider_change(self, v):
        # Текст пина обновляет сам view (valueChanged → lbl_threshold.set_value).
        # Здесь — только UI-состояние группы пресетов: ручное движение ползунка
        # переводит радио в «Свою». Ни бэкенда, ни смены экрана.
        if not self.view.radio_custom.isChecked():
            self.view.mode_btn_group.blockSignals(True)
            self.view.radio_custom.setChecked(True)
            self.view.mode_btn_group.blockSignals(False)

    def _set_recluster_cursor(self):
        """Ставит WaitCursor РОВНО один раз на цикл рекластеризации (см. флаг)."""
        if not self._recluster_cursor_active:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            self._recluster_cursor_active = True

    def _restore_recluster_cursor(self):
        """Снимает WaitCursor рекластеризации, если он был поставлен. Идемпотентно —
        вызывается на КАЖДОМ терминальном пути (готово/пусто/ошибка), поэтому не
        даёт курсору залипнуть, когда оркестратор отбросил команду или упал."""
        if self._recluster_cursor_active:
            QApplication.restoreOverrideCursor()
            self._recluster_cursor_active = False

    def _trigger_recluster(self):
        if not self.engine_ready: return

        self._set_status("status_reclustering")
        self._set_recluster_cursor()

        pct = self.view.slider_threshold.value()
        # Единственная точка обновления кэша: фиксируем фактически отправленный
        # в FAISS порог. Скан и смена движка проходят сюда напрямую (минуя
        # guard-слоты), поэтому кэш остаётся согласован со всеми путями.
        self._last_applied_threshold = pct
        threshold = 1.0 - (pct / 100.0)
        bus.cmd_recluster.emit(threshold)

    def _on_clustering_finished(self, clusters):
        # Снимаем флаг сразу: этот вызов «съедает» отметку о завершённом скане,
        # чтобы последующие холостые recluster'ы по ползунку её уже не видели.
        just_scanned = self._scan_just_completed
        self._scan_just_completed = False

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
            self._restore_recluster_cursor()

            # Модалку показываем ТОЛЬКО после реального скана (just_scanned), а не
            # при тюнинге порога ползунком/клавиатурой: иначе при сжатии порога до
            # значения без совпадений окно «дубликаты не найдены» выскакивало на
            # КАЖДЫЙ % (пустой результат — это нормальный визуальный фидбек тюнинга,
            # а не повод для блокирующего диалога). Раньше guard стоял лишь на ветке
            # «нет подходящих файлов», а ветка «дубликатов нет» спамила модалкой.
            if just_scanned:
                is_ru = translator.current_lang == "ru"
                title = "Сканирование завершено" if is_ru else "Scan Complete"
                scanned_count = len(getattr(self.engine, 'current_file_data', [])) if getattr(self, 'engine', None) else 0

                if scanned_count > 0:
                    # Файлы есть, но при текущем пороге совпадений нет.
                    msg = ("Дубликаты не найдены. Попробуйте снизить порог чувствительности "
                           "(Ползунок %) или выбрать другие директории." if is_ru else
                           "No duplicates found. Try lowering the matching threshold (%) "
                           "or selecting different directories.")
                else:
                    # Скан реально отработал, но не нашёл НИ ОДНОГО подходящего файла
                    # (пустая папка, не те форматы, либо все файлы битые/нечитаемы).
                    self._set_status("status_done")
                    msg = ("Подходящие файлы не найдены в выбранных директориях.\n"
                           "Проверьте путь и отмеченные форматы (Фото / Видео / Документы)."
                           if is_ru else
                           "No suitable files were found in the selected directories.\n"
                           "Check the path and the selected formats (Images / Videos / Documents).")
                QMessageBox.information(self.view, title, msg)
            return

        self._start_render_tree(clusters)
        self.view.tree.setUpdatesEnabled(True)
        self.view.tree.expandAll()
        self._update_statistics_panel()
        self._restore_recluster_cursor()
        self._set_status("status_done")
        # View-Switching Lock: на вкладку результатов переключаемся ТОЛЬКО когда
        # этот пересчёт пришёл из завершённого глобального скана. Тихие recluster'ы
        # по порогу (слайдер/клавиатура) обновляют дерево на месте и НЕ угоняют
        # активный экран — иначе точная настройка порога рвала контекст
        # пользователя, выбрасывая его из настроек в аналитику на каждый %.
        if just_scanned:
            self.view._switch_tab(1)

    def _on_item_double_clicked(self, proxy_index):
        # BLOCK 4: двойной клик ВЫБИРАЕТ файл — ставит/снимает чекбокс удаления
        # (та же логика, что Space → manual_check_selected). РАНЬШЕ он открывал
        # сравнение, но это сбивало с толку (двойной клик ожидаемо «выбирает»
        # файл, а не уводит в полноэкранный режим) и накапливал медиа-контексты
        # при повторных заходах → фриз. Сравнение теперь только по Enter / Cmd+P /
        # кнопке / контекстному меню. На dblclick Qt уже сделал кликнутую строку
        # текущей и выделенной, так что manual_check_selected переключит её галочку;
        # proxy_index здесь служит лишь стражем валидности.
        if not proxy_index.isValid():
            return
        self.selection_controller.manual_check_selected()

    def _open_compare_shortcut(self):
        # BLOCK 4: Enter открывает сравнение (двойной клик теперь переключает
        # галочку, а не открывает сравнение). Гасим, когда фокус в поле поиска,
        # чтобы Enter подтверждал ввод запроса, а не прыгал в сравнение (паритет
        # с гардами selection_controller).
        if getattr(self.view, 'search_input', None) and self.view.search_input.hasFocus():
            return
        self._trigger_grid_compare()

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
        # Видимый состав ряда сменился (Скан → Пауза+Стоп) — пересчитываем,
        # влезает ли текстовый режим в текущую ширину сайдбара.
        self.view._update_scan_controls_mode()
        self.view.model.clear()
        if self.view.video_player is not None:
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
            self.view.update_pause_label(translator.tr("btn_resume") or "Продолжить")
        else:
            self.current_status_key = None
            self.view.lbl_status.setText("Scanning..." if translator.current_lang == "en" else "Сканирование...")
            self.view.update_pause_label(translator.tr("btn_pause") or "Пауза")

    def _stop_scan(self):
        self.is_stopped_requested = True
        bus.cmd_stop_scan.emit()
        self.view.btn_pause.hide()
        self.view.btn_stop.hide()
        self.view._update_scan_controls_mode()
        self._set_status("status_stopping")
        QTimer.singleShot(1500, self._force_scan_abort)

    def _force_scan_abort(self):
        if not self.view.btn_scan.isVisible():
            self.scan_timer.stop()
            self.view.btn_scan.show()
            self.view._update_scan_controls_mode()
            self.view.btn_scan.setEnabled(True)
            self._set_status("status_aborted")
            self.view.progress_bar.setValue(0)
            self.is_paused = False
            self.is_stopped_requested = False

    def _on_scan_error(self, err_msg):
        # OOM-отказ рекластеризации приходит сюда (evt_scan_error), а не в
        # _on_clustering_finished — снимаем WaitCursor рекластеризации здесь же,
        # иначе он залип бы (терминальное событие без completion).
        self._restore_recluster_cursor()
        self._force_scan_abort()
        QMessageBox.critical(self.view, translator.tr("dialog_scan_error"), f"Критическая ошибка NPU:\n{err_msg}")

    def _on_scan_finished(self):
        self.scan_timer.stop()
        self.view.btn_pause.hide()
        self.view.btn_stop.hide()
        self.view.btn_scan.show()
        self.view._update_scan_controls_mode()

        self.view.update_pause_label(translator.tr("btn_pause") or "Пауза")
        
        if self.is_stopped_requested:
            self.view.btn_scan.setEnabled(True)
            self._set_status("status_aborted")
            self.view.progress_bar.setValue(0)
            self.is_stopped_requested = False
        else:
            # Кластеризация после скана: помечаем, что следующий
            # evt_clustering_completed пришёл из реального прохода, а не из
            # холостого recluster по ползунку.
            self._scan_just_completed = True
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
                    except Exception:
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

        # Safe Batch Injection: при включённой сортировке QTreeView держит
        # ArbitrageSortFilterProxyModel «горячим» — после endResetModel прокси
        # пересортировывает весь набор, а заголовок дёргает sort() повторно.
        # На 15 000 строк это каскад O(n log n) сравнений с пересчётом
        # геометрии на каждую вставку. Гасим сортировку на время заливки и
        # восстанавливаем одним проходом после — внутри set_clusters уже
        # стоит beginResetModel/endResetModel, так что это единственная
        # дорогая остаточная синхронизация.
        was_sorting = self.view.tree.isSortingEnabled()
        self.view.tree.setSortingEnabled(False)
        self.view.model.set_clusters(valid_clusters)
        self.view.tree.setSortingEnabled(was_sorting)

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

        # АНТИ-GHOST: удалённые пути выбрасываются и из in-memory датасета
        # движка СРАЗУ. Иначе следующий recluster по ползунку строил кластеры
        # по старому списку (и по совпавшей FAISS-подписи поднимал устаревшую
        # kNN-матрицу с диска), «воскрешая» удалённые файлы в результатах.
        # Stage 0 в SmartClusterEngine.build_clusters дублирует эту защиту
        # независимой проверкой os.path.exists + SQLite.
        engine = getattr(self, 'engine', None)
        if engine is not None and getattr(engine, 'current_file_data', None):
            norm_dead = {os.path.normpath(os.path.abspath(p)) for p in paths_set}
            engine.current_file_data = [
                it for it in engine.current_file_data
                if it['path'] not in paths_set
                and os.path.normpath(os.path.abspath(it['path'])) not in norm_dead
            ]
        
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
                menu.addAction(translator.tr("ctx_reveal"), lambda p=path: reveal_in_os(p))

        # Тянем цвета из активной темы, а не хардкодим тёмную палитру — иначе в
        # светлой теме меню оставалось тёмным. Акцент выделения — фирменный
        # blurple (#5865F2), он читаем на обоих фонах.
        c = ThemeManager.colors()
        menu.setStyleSheet(
            f"QMenu {{ background-color: {c['surface']}; color: {c['text']}; "
            f"border: 1px solid {c['border']}; }} "
            f"QMenu::item:selected {{ background-color: #5865F2; color: #FFFFFF; }}"
        )
        menu.exec(self.view.tree.viewport().mapToGlobal(pos))

    # Затвор живого скраба мультипревью, мс (~30 Гц, паритет с multi_compare).
    _MULTI_SCRUB_MIN_MS = 33

    def _multi_slider_pct(self) -> float:
        """Позиция слайдера мультипревью в процентах (слайдер — в пермилле)."""
        return self.view.multi_sync_slider.value() / 10.0

    def _update_multi_pos_label(self):
        self.view.multi_pos_label.setText(f"{self._multi_slider_pct():.0f}%")

    def _scrub_multi_video_frames(self, value):
        # Живой скраб: индикатор позиции идёт за пальцем всегда (дёшево),
        # а запросы к cv2-декодеру отсекаются затвором ДО обращения к воркеру.
        self._update_multi_pos_label()
        now = self._multi_scrub_clock.elapsed()
        if now - self._last_multi_scrub_ms < self._MULTI_SCRUB_MIN_MS:
            return
        self._last_multi_scrub_ms = now
        if self.multi_preview_lbls:
            self.video_worker.request_frames(list(self.multi_preview_lbls.keys()), value / 10.0)

    def _execute_multi_video_frames(self):
        # Финальный запрос по отпусканию/клику — БЕЗ троттла: последняя
        # позиция пользователя обязана быть отрисована.
        self._update_multi_pos_label()
        if self.multi_preview_lbls:
            self.video_worker.request_frames(list(self.multi_preview_lbls.keys()), self._multi_slider_pct())
            
    def _on_worker_frame_ready(self, path, qimg):
        if path in self.multi_preview_lbls:
            if not qimg.isNull():
                self.multi_preview_lbls[path].setPixmap(QPixmap.fromImage(qimg))
                # Первый РЕАЛЬНЫЙ кадр ведущего видео несёт его AR — доводим ось.
                self._maybe_adjust_multi_orientation(path, qimg)
            else:
                self.multi_preview_lbls[path].clear_view()

    def _maybe_adjust_multi_orientation(self, path, qimg):
        """Реактивная доводка cv2-сетки видео-пары по асинхронному AR кадра.

        probe_media_ar для видео-лида вернул None → render_multi_preview стартовал
        в cols=1 (друг под другом). Первый кадр ведущего видео из MultiVideoWorker
        даёт реальный AR (кадр уже с учётом rotation, downscale пропорции хранит):
        для ПОРТРЕТА (AR < 1.0) пара перестраивается 1→2 (бок о бок, делят ширину
        во всю высоту). Срабатывает РОВНО ОДИН раз на набор (флаг applied) и только
        для текущего ведущего видео — запоздавший кадр второй карточки/прошлого
        набора оси не трогает. cv2 в GUI-потоке при этом не вызывается."""
        if self._multi_orientation_applied or path != self._multi_pair_lead:
            return
        h = qimg.height()
        if h <= 0:
            return
        self._multi_orientation_applied = True   # один раз на набор
        if qimg.width() / h < 1.0 and self._multi_grid_cols != 2:
            self._relayout_multi_grid(2)

    def _relayout_multi_grid(self, cols):
        """Перекладывает УЖЕ созданные ячейки cv2-сетки под новое число колонок,
        НЕ пересоздавая виджеты и НЕ меняя их родителя (removeWidget/addWidget на
        том же layout — без orphan/reparent, т.е. без триггера macOS Spaces Jump)."""
        grid = self.view.multi_grid
        for w in self._multi_grid_items:
            grid.removeWidget(w)
        for i, w in enumerate(self._multi_grid_items):
            grid.addWidget(w, i // cols, i % cols)
        self._multi_grid_cols = cols

    def _trigger_grid_compare(self):
        # RE-ENTRANCY GUARD. Уже на странице сравнения (index 1) либо ещё идёт
        # отложенный teardown предыдущего входа — НЕ запускаем второй цикл сноса-
        # пересоздания медиа-контекста. Без этого пачка быстрых Enter/Cmd+P (или
        # прежний двойной клик) укладывала release_source_safe друг на друга и
        # вешала WindowServer/GIL при повторных заходах в сравнение.
        if self.view.root_stack.currentIndex() == 1 or self._compare_switch_pending:
            return

        # Гасим отложенный пересчёт превью (selection_timer, 150мс от клика). Иначе
        # после ухода в полноэкранное сравнение всплывший process_selection поднял
        # бы ВТОРОЙ медиа-контекст в скрытом одиночном плеере (load_video →
        # setSource в фоновом Space, под fullscreen-сравнением). Эти осиротевшие
        # AVFoundation/Metal-контексты копились при повторных заходах → фриз.
        self.selection_timer.stop()

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
                    path = data.get('path', data.get('file_path', str(data)))
                    pts.append((path, bool(data.get('is_ref', False))))
        else:
            pts = []
            for idx in sel:
                data = self.view.model.itemFromIndex(idx.siblingAtColumn(0)).data(Qt.ItemDataRole.UserRole)
                if data and isinstance(data, dict):
                    path = data.get('path', data.get('file_path', str(data)))
                    pts.append((path, bool(data.get('is_ref', False))))
            
        if len(pts) < 2: return

        v = self.view
        # ОТКАЗ ОТ Destroy & Rebuild. Виджет сравнения ПОСТОЯНЕН (создан один раз
        # в MainWindow._setup_ui, index 1) и переиспользуется. Резкое удаление/
        # пересоздание виджета вместе с рывковым скрытием QVideoWidget в
        # полноэкранном режиме провоцировало панику macOS WindowServer
        # (Spaces Jump). Теперь последовательность строго мягкая:

        # 1. МЯГКО прячем QVideoWidget, переключая инспектор на страницу одиночного
        #    превью (index 0). QVideoWidget НЕ скрывается рывком вместе со сменой
        #    root_stack — это предотвращает краш AVPlayerLayer в Fullscreen.
        v.preview_stack.setCurrentIndex(0)

        # КРОСС-КАТТИНГ TEARDOWN (No-Deadlock + No-Spaces-Jump). РАНЬШЕ здесь шёл
        # СИНХРОННЫЙ v.video_player.stop() в ТОМ ЖЕ кванте, что и смена root_stack
        # ниже, — неразрешимый конфликт инвариантов:
        #   • синхронный снос живого источника = главный поток в QThread::wait под
        #     GIL ↔ QFFmpeg::AudioRenderer тянется за GIL → встречный дедлок;
        #   • а отложить снос «в лоб» нельзя — смена слоя при ЖИВОМ AVFoundation/
        #     Metal-контексте возвращает Spaces Jump.
        # Решение: release_source_safe уводит C++ деструкцию контекста в deleteLater
        # (вне GIL-кванта → дедлок исключён) И через сигнал destroyed снесённого
        # плеера вызывает on_done СТРОГО ПОСЛЕ деструкции — там и только там
        # переключаем root_stack. Оба инварианта держатся одновременно.
        # Взводим guard ТОЛЬКО здесь — после всех ранних return (нет выбора /
        # pts < 2), иначе застряли бы со взведённым флагом и заблокировали вход.
        self._compare_switch_pending = True
        switched_via_teardown = False
        if v.video_player is not None:
            try:
                v.video_player.video_widget.hide()
                v.video_player.hide()
                v.video_player.release_source_safe(
                    on_done=self._finish_compare_switch)
                switched_via_teardown = True
            except Exception as e:
                auditor.warning(f"video_player deferred teardown before compare failed: {e}", exc_info=True)

        # 2. Загружаем новый набор в УЖЕ существующий виджет (карточки/декодеры
        #    пересобираются внутри load(); тяжёлое медиа поднимается отложенно из
        #    showEvent — уже в активном Space). Готовим ДО смены слоя.
        v.compare_widget.load(pts)

        # 3. Смену Space выполняет on_done выше — ПОСЛЕ деструкции медиа-контекста.
        #    Фолбэк (плеера нет либо teardown бросил) — переключаем сразу, чтобы не
        #    застрять на списке файлов (и сбрасываем guard).
        if not switched_via_teardown:
            self._finish_compare_switch()

    def _finish_compare_switch(self):
        # Завершение отложенного входа в сравнение: снимаем re-entrancy guard и
        # выводим страницу сравнения. Вызывается строго ПОСЛЕ деструкции старого
        # медиа-контекста (по сигналу destroyed из release_source_safe) либо
        # синхронно в фолбэке. Идемпотентно — повторный destroyed нас не сломает.
        self._compare_switch_pending = False
        self.view.root_stack.setCurrentIndex(1)

    def _on_compare_confirmed(self):
        # «Применить» на странице сравнения: считываем выбор, возвращаем стек на
        # главную страницу и удаляем отмеченные файлы.
        files_to_delete = list(self.view.compare_widget.files_to_delete)
        delete_hard = self.view.compare_widget.delete_hard
        self.view.root_stack.setCurrentIndex(0)
        self._apply_compare_deletion(files_to_delete, delete_hard)

    def _apply_compare_deletion(self, files_to_delete, delete_hard):
        # Удаляем файлы, отмеченные в окне сравнения («Применить») — В ФОНОВОМ
        # ПОТОКЕ. Раньше safe_delete/hard_delete вызывались синхронно прямо здесь,
        # в GUI-потоке, а на macOS safe_delete делает time.sleep(0.1..0.3) на
        # КАЖДЫЙ файл (каскад NSWorkspace→send2trash→AppleScript) — это вешало
        # интерфейс на секунды. Тот же путь, что и process_delete: BatchOpWorker.
        files_to_delete = list(files_to_delete)
        if not files_to_delete:
            return

        func = SafeFSExecutor.hard_delete if delete_hard else SafeFSExecutor.safe_delete
        worker = BatchOpWorker(func, files_to_delete)
        # Удерживаем воркер в наборе на всё время его жизни (см. _compare_del_workers
        # в __init__): защищает и от гонки повторного «Применить», и от прежней
        # утечки — раньше воркер вообще не уходил в deleteLater, лишь подменялся.
        self._compare_del_workers.add(worker)

        def _on_compare_del_done(res, w=worker):
            if isinstance(res, dict) and res.get("error") == "send2trash_missing":
                QMessageBox.critical(
                    self.view,
                    translator.tr("dialog_del_report_title"),
                    "Ошибка: Модуль 'send2trash' не найден. Удаление в корзину невозможно."
                )
            self._prune_dead_nodes(files_to_delete)
            self._compare_del_workers.discard(w)
            w.deleteLater()

        worker.finished.connect(_on_compare_del_done)
        worker.start()