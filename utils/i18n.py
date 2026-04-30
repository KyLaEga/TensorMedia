from PyQt6.QtCore import QObject, pyqtSignal, QSettings

class TranslationEngine(QObject):
    """Singleton-диспетчер глобальной локализации."""
    language_changed = pyqtSignal()
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(TranslationEngine, cls).__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self):
        super().__init__()
        self.settings = QSettings("TensorMedia", "ArbitrageConfig")
        self.current_lang = self.settings.value("language", "en")
        
        self.dictionaries = {
            "en": {
                "theme": "🎨 Theme",
                "lang": "🌐 Language",
                "help": "⌨️ Help",
                "tab_scan": "⚙️ Scanning",
                "tab_analytics": "📊 Analytics",
                "mode_single": "Single Folder",
                "mode_dual": "Reference vs Inbox",
                "btn_select_a": "📁 Select...",
                "btn_select_b": "📥 Inbox...",
                "lbl_not_selected": "Directory not selected",
                "chk_img": "Photos",
                "chk_vid": "Videos",
                "chk_doc": "Docs",
                "engine_mode": "<b>Engine:</b>",
                "engine_visual": "Visual (SigLIP)",
                "engine_faces": "Biometrics (FaceNet)",
                "threshold": "<b>🧠 Similarity Threshold</b>",
                "btn_scan": "Scan",
                "btn_pause": "⏸️ Pause",
                "btn_stop": "⏹️ Stop",
                "status_wait": "Waiting...",
                "status_npu_ready": "✅ Neural Engine Ready",
                "col_file": "File",
                "col_fmt": "Format",
                "col_sim": "Similarity",
                "col_size": "Size",
                "col_res": "Resolution",
                "col_time": "Duration",
                "btn_compare": "⚖️ Compare",
                "btn_move": "📂 Move...",
                "btn_del": "🗑️ Delete (Del)",
                "telemetry": "<b>📊 Telemetry</b>",
                "stat_time": "⏱ Time:",
                "stat_files": "📄 Files:",
                "stat_dups": "📦 Duplicates:",
                "stat_sel": "☑️ Selected:",
                "stat_del": "💾 To Delete:",
                "filters": "<b>🔍 Filters</b>",
                "search_ph": "Filename or text...",
                "type": "Type:",
                "filter_all": "All files",
                "filter_img": "Photos only",
                "filter_vid": "Videos only",
                "filter_doc": "Docs only",
                "marking": "<b>✨ Marking</b>",
                "strat_smart": "🧠 Smart select",
                "strat_quality": "🏆 Quality",
                "strat_size": "📦 Size",
                "strat_date": "📅 Original",
                "chk_clean": "No watermarks",
                "btn_auto": "✨ Auto (Cmd+A)",
                "btn_clear": "🧹 Clear",
                "btn_sidebar_hide": "◂ Sidebar",
                "btn_sidebar_show": "▸ Sidebar",
                "btn_expand": "📂 Expand",
                "btn_collapse": "📁 Collapse",
                "btn_inspector_hide": "Inspector ◂",
                "btn_inspector_show": "Inspector ▸",
                "cluster_prefix": "Cluster",
                "cluster_files": "files",
                "ref_prefix": "[REFERENCE]",
                "help_title": "Hotkeys",
                "help_text": "<b>⌨️ Global Hotkeys:</b><br><br><b>Space</b> or <b>Enter</b> — Toggle checkmarks<br><b>Cmd+A (Ctrl+A)</b> — Smart Auto-Marking<br><b>Cmd+D (Ctrl+D)</b> — Clear all checkmarks<br><b>Backspace / Delete</b> — Move to Trash<br><b>Shift + Backspace/Delete</b> — Delete Permanently<br><b>F1</b> — Show this help<br><br><b>Mouse:</b><br><b>Drag & Drop</b> — Drag a folder from Finder to scan, or drag files from the table to any folder.",
                "img_loading": "Loading...",
                "img_error": "⚠️ Media Error",
                "img_doc": "📄 Document",
                "scan_prep": "Preparing Neural Engine",
                "scan_io": "Building I/O Matrix...",
                "scan_cache": "Restoring from cache...",
                "scan_npu": "NPU Analysis: ",
                "scan_faiss": "Building FAISS graph..."
            },
            "ru": {
                "theme": "🎨 Тема",
                "lang": "🌐 Язык",
                "help": "⌨️ Помощь",
                "tab_scan": "⚙️ Сканирование",
                "tab_analytics": "📊 Аналитика",
                "mode_single": "Одна папка",
                "mode_dual": "Эталон vs Inbox",
                "btn_select_a": "📁 Выбрать...",
                "btn_select_b": "📥 Inbox...",
                "lbl_not_selected": "Папка не выбрана",
                "chk_img": "Фото",
                "chk_vid": "Видео",
                "chk_doc": "Документы",
                "engine_mode": "<b>Режим:</b>",
                "engine_visual": "Визуальный (SigLIP)",
                "engine_faces": "Лица (FaceNet)",
                "threshold": "<b>🧠 Точность сходства</b>",
                "btn_scan": "Сканировать",
                "btn_pause": "⏸️ Пауза",
                "btn_stop": "⏹️ Стоп",
                "status_wait": "Ожидание...",
                "status_npu_ready": "✅ Нейронный движок готов",
                "col_file": "Файл",
                "col_fmt": "Формат",
                "col_sim": "Сходство",
                "col_size": "Размер",
                "col_res": "Разрешение",
                "col_time": "Время",
                "btn_compare": "⚖️ Сравнить",
                "btn_move": "📂 Перенести...",
                "btn_del": "🗑️ Удалить (Del)",
                "telemetry": "<b>📊 Телеметрия</b>",
                "stat_time": "⏱ Время:",
                "stat_files": "📄 Файлов:",
                "stat_dups": "📦 Дубликатов:",
                "stat_sel": "☑️ Выбрано:",
                "stat_del": "💾 К удалению:",
                "filters": "<b>🔍 Фильтры</b>",
                "search_ph": "Поиск по имени...",
                "type": "Тип:",
                "filter_all": "Все файлы",
                "filter_img": "Только Фото",
                "filter_vid": "Только Видео",
                "filter_doc": "Только Документы",
                "marking": "<b>✨ Разметка</b>",
                "strat_smart": "🧠 Умный выбор",
                "strat_quality": "🏆 Качество",
                "strat_size": "📦 Размер",
                "strat_date": "📅 Оригинал",
                "chk_clean": "Без вотермарок",
                "btn_auto": "✨ Авто (Cmd+A)",
                "btn_clear": "🧹 Сброс",
                "btn_sidebar_hide": "◂ Сайдбар",
                "btn_sidebar_show": "▸ Сайдбар",
                "btn_expand": "📂 Развернуть",
                "btn_collapse": "📁 Свернуть",
                "btn_inspector_hide": "Инспектор ◂",
                "btn_inspector_show": "Инспектор ▸",
                "cluster_prefix": "Кластер",
                "cluster_files": "файлов",
                "ref_prefix": "[ЭТАЛОН]",
                "help_title": "Горячие клавиши",
                "help_text": "<b>⌨️ Глобальные Горячие Клавиши:</b><br><br><b>Space (Пробел)</b> или <b>Enter</b> — Поставить/Снять галочки<br><b>Cmd+A (Ctrl+A)</b> — Умная Авторазметка<br><b>Cmd+D (Ctrl+D)</b> — Сбросить все галочки<br><b>Backspace / Delete</b> — Удалить выбранное<br><b>Shift + Backspace/Delete</b> — Удалить насовсем<br><b>F1</b> — Показать эту справку<br><br><b>Мышь:</b><br><b>Drag & Drop</b> — Перетащите папку из Finder для сканирования, или перетащите файлы из таблицы.",
                "img_loading": "Загрузка...",
                "img_error": "⚠️ Ошибка медиа",
                "img_doc": "📄 Документ",
                "scan_prep": "Подготовка нейронного движка",
                "scan_io": "Сборка матрицы I/O...",
                "scan_cache": "Восстановление из кэша...",
                "scan_npu": "Анализ NPU: ",
                "scan_faiss": "Сборка FAISS графа..."
            }
        }

    def tr(self, key: str) -> str:
        return self.dictionaries.get(self.current_lang, self.dictionaries["en"]).get(key, key)

    def set_language(self, lang_code: str):
        if lang_code in self.dictionaries and lang_code != self.current_lang:
            self.current_lang = lang_code
            self.settings.setValue("language", lang_code)
            self.language_changed.emit()

translator = TranslationEngine()