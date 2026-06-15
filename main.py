# ============================================================
# MODULE: main.py
# ============================================================
import sys
import os
import warnings

if sys.stdout is None: sys.stdout = open(os.devnull, 'w')
if sys.stderr is None: sys.stderr = open(os.devnull, 'w')

# ГАШЕНИЕ ABI-ШУМА NumPy. C-расширения, собранные под NumPy 1.x (часть сборок
# faiss / shiboken6 / torch), при импорте под установленным NumPy 2.x печатают
# через warnings.warn(UserWarning) сообщение вида:
#   "A module that was compiled using NumPy 1.x cannot be run in NumPy 2.x ..."
# Это НЕ ошибка выполнения (ABI-shim NumPy 2 обрабатывает вызовы), а диагностика.
# Версии пакетов НЕ трогаем (downgrade NumPy сломал бы воспроизводимость сборки);
# подавляем строго это предупреждение ДО любого импорта numpy/torch/faiss/PySide6,
# иначе фильтр опоздает — варнинг печатается в момент первого импорта расширения.
warnings.filterwarnings("ignore", category=UserWarning,
                        message=r".*compiled using NumPy 1\.x.*")
warnings.filterwarnings("ignore", category=UserWarning,
                        message=r".*NumPy 1\.x cannot be run in NumPy 2.*")

# КРИТИЧЕСКИЙ ПАТЧ: Ограничение потоков на уровне ядра ОС для предотвращения крашей 
# OpenMP/BLAS (faiss, torch, numpy) на архитектуре Apple Silicon и Windows x64.
# Должно выполняться ДО ЛЮБЫХ импортов сторонних библиотек.
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

import multiprocessing
import traceback

# ВНИМАНИЕ: импорты PySide6 (и модулей, тянущих GUI, напр. weight_manager ->
# LocalWeightValidator/QDialog) НЕ должны находиться в глобальной области.
# При start_method='spawn' дочерний воркер импортирует этот модуль как
# '__mp_main__', повторно прогоняя глобальный код. Рекурсивный импорт PySide6
# в воркере роняет Shiboken. Поэтому весь GUI-импорт вынесен строго внутрь
# 'if __name__ == "__main__"' (см. низ файла), где он пропускается воркерами.

from utils.env_config import setup_offline_env
from utils.batch_operations import BatchOperations
from utils.logger import auditor

class ApplicationBootstrap:
    @staticmethod
    def _render_critical_ui(exc_value, error_msg):
        try:
            # Парентим к активному окну и делаем диалог ОКОННО-МОДАЛЬНЫМ (sheet).
            # Беспарентный app-modal QMessageBox — это отдельное top-level окно;
            # пока главное окно занимает собственный fullscreen-Space (зелёная
            # кнопка macOS), WindowServer не может прикрепить его как sheet и
            # уводит пользователя на другой рабочий стол ("Spaces Jump"). Sheet,
            # привязанный к активному окну, остаётся внутри текущего Space.
            from PySide6.QtCore import Qt
            parent = QApplication.activeWindow()
            msg_box = QMessageBox(parent)
            msg_box.setWindowModality(Qt.WindowModality.WindowModal)
            msg_box.setIcon(QMessageBox.Icon.Critical)
            msg_box.setWindowTitle("Критический сбой")
            msg_box.setText(f"Произошла фатальная ошибка:\n\n{exc_value}")
            msg_box.setDetailedText(error_msg)
            msg_box.exec()
        except Exception as gui_exc:
            auditor.error(f"FAILED TO RENDER CRITICAL UI: {gui_exc}")

    @staticmethod
    def global_exception_handler(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return

        error_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        auditor.critical(f"CRITICAL RUNTIME ERROR:\n{error_msg}")
        
        app = QApplication.instance()
        if app and not app.closingDown():
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: ApplicationBootstrap._render_critical_ui(exc_value, error_msg))

    @classmethod
    def execute(cls):
        os.environ["QT_API"] = "pyside6"
        sys.excepthook = cls.global_exception_handler
        
        if sys.platform == "win32":
            try:
                import ctypes
                myappid = 'com.tensormedia.arbitrage.v1'
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
            except Exception as e:
                auditor.warning(f"Failed to set Windows AppUserModelID: {e}")
        
        auditor.info("TensorMedia Application Bootstrapping Started.")
        
        setup_offline_env()
        BatchOperations.check_and_recover_pending_transactions()
        
        QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
        app = QApplication(sys.argv)

        # ПЕРЕХВАТЧИК ЗАВЕРШЕНИЯ. Cmd-Q / закрытие окна роняет процесс в Abort
        # trap: 6 (SIGABRT): деструкторы C++ паникуют, потому что фоновый поток
        # CompareVideoWorker заблокирован в __psynch_cvwait, а PySide6 пытается
        # разобрать живое Qt-дерево. Поэтому процесс мы валим на уровне ОС
        # (os._exit) ДО запуска C++-деструкторов. НО os._exit пропускает и
        # cleanup multiprocessing, из-за чего семафоры пула утекают
        # ('leaked semaphore objects'). Решение — порядок: сперва graceful
        # teardown пула (MLOrchestrator.stop_all, см. ниже), и только ПОСЛЕДНИМ
        # слотом aboutToQuit — собственно os._exit. Qt вызывает слоты в порядке
        # подключения, поэтому здесь раннюю «голую» привязку os._exit мы больше
        # НЕ ставим (иначе она срабатывала первой и stop_all не успевал отдать
        # семафоры). Привязка os._exit перенесена ниже, после connect(stop_all).

        # Единственный источник типографики на всё приложение. Никаких локальных
        # .setFont()/font-family/font-size в виджетах и QSS — всё наследуется
        # отсюда, чтобы шрифт был консистентным. Базовый размер берётся из
        # дизайн-системы (ThemeManager.FONT_BASE), а не из «магического» числа.
        #
        # КРОССПЛАТФОРМЕННОСТЬ (RCA «пережатых шрифтов» на Windows): семейство
        # ".AppleSystemUIFont" существует ТОЛЬКО в CoreText. На Windows Qt не
        # находил его и подставлял суррогат с чужими метриками (DirectWrite),
        # из-за чего ломались ширины строк, ряды наслаивались и элементы
        # пробивали границы карточек. Семейство выбирается по платформе:
        # macOS — системный San Francisco, Windows — Segoe UI (родная
        # ClearType-гарнитура), прочее (Linux) — системный GeneralFont Qt.
        from utils.theme_manager import ThemeManager
        if sys.platform == "darwin":
            _font = QFont(".AppleSystemUIFont", ThemeManager.FONT_BASE)
        elif sys.platform == "win32":
            # Segoe UI carries no emoji glyphs; declare Segoe UI Emoji as an
            # explicit fallback family so emoji used in labels/tabs (🔍 📊 🗑 🧹
            # ⚖️ 📌 …) resolve via Qt's per-glyph fallback instead of rendering as
            # tofu boxes. (Under CrossOver/Wine the emoji font is often absent —
            # that is a Wine limitation, not reproducible on real Windows.)
            _font = QFont("Segoe UI", ThemeManager.FONT_BASE)
            _font.setFamilies(["Segoe UI", "Segoe UI Emoji"])
        else:
            from PySide6.QtGui import QFontDatabase
            _font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
            _font.setPointSize(ThemeManager.FONT_BASE)
        app.setFont(_font)

        icon_name = "app.ico" if sys.platform == "win32" else "app.icns"
        app.setWindowIcon(ThemeManager.load_icon(icon_name))
        
        validator = LocalWeightValidator()
        validator.start()
        
        if validator.exec() == QDialog.Accepted:
            from core.services.ml_orchestrator import MLOrchestrator
            from ui.views.main_window import MainWindow
            from ui.controllers.main_controller import MainController
            
            cls.orchestrator = MLOrchestrator()
            window = MainWindow()
            controller = MainController(window)

            # Cmd/Ctrl+D регистрируется системно через нативный QMenuBar
            # ("Правка" → "Снять выделение") в MainWindow и привязан к
            # tree.clearSelection. Отдельный app-level eventFilter не нужен и
            # не должен возвращаться — иначе будет двойное срабатывание.

            if cls.orchestrator:
                window.window_closed.connect(cls.orchestrator.stop_all)
                app.aboutToQuit.connect(cls.orchestrator.stop_all)

            # БЕЗОПАСНЫЙ СИСТЕМНЫЙ ВЫХОД подключаем ПОСЛЕДНИМ слотом aboutToQuit.
            # Qt вызывает слоты в порядке подключения, поэтому graceful teardown
            # пула (stop_all → shutdown_pool + terminate/join всех дочерних
            # процессов, СИНХРОННОЕ освобождение POSIX-семафоров) гарантированно
            # завершается ДО выхода. И только после этого процесс снимается через
            # os._exit(0).
            #
            # Почему os._exit, а НЕ SIGKILL и НЕ обычный возврат из app.exec():
            #   • SIGKILL(9) валил процесс по сигналу — оболочка печатала
            #     "zsh: killed", и, что хуже, сигнал прилетал АСИНХРОННО и мог
            #     обогнать resource_tracker, оставив 'leaked semaphore objects'.
            #   • Обычный возврат запускает C++-деструкторы живого Qt-дерева, а
            #     фоновый QThread, заблокированный в __psynch_cvwait, роняет это
            #     в Abort trap: 6 (SIGABRT).
            #   • os._exit(0) — это syscall _exit: НЕ запускает C++-деструкторы
            #     (нет SIGABRT) и завершает процесс штатным кодом возврата 0 (нет
            #     "zsh: killed"). Семафоры пула к этому моменту уже освобождены
            #     синхронно в stop_all, поэтому утечки исключены, а resource_tracker
            #     штатно завершится следом за родителем — без зомби-процессов.
            app.aboutToQuit.connect(lambda: os._exit(0))
            
            auditor.info("UI and NPU Orchestrator initialized successfully.")
            window.show()
            # macOS: зелёная кнопка работает штатно — окно уходит в полноценный
            # изолированный Space. Прыжки рабочих столов при показе сравнения
            # исключены тем, что страница сравнения встроена в QStackedWidget
            # (setCurrentIndex), а не создаётся как отдельный NSWindow/QDialog.
            sys.exit(app.exec())
        else:
            auditor.warning("NPU Weight Validation Failed. Terminating process.")
            sys.exit(1)

if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        multiprocessing.set_start_method('spawn', force=True)
    except RuntimeError:
        pass

    # Импорты, напрямую зависящие от GUI, выполняются ТОЛЬКО в главном процессе.
    # Связывание происходит в глобальном пространстве имён модуля, поэтому методы
    # ApplicationBootstrap (определённые выше) корректно резолвят эти имена в
    # момент вызова execute(), уже после их импорта здесь.
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont
    from core.ml.weight_manager import LocalWeightValidator

    ApplicationBootstrap.execute()