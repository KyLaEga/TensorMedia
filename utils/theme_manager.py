import os
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPalette, QColor, QIcon, QPixmap
from PySide6.QtCore import QByteArray
# Импорт QtSvg регистрирует image-handler формата "SVG" — без него
# QPixmap.loadFromData(..., "SVG") возвращает пустой пиксмап и все векторные
# иконки (play/pause/mute) рендерятся пустой областью.
from PySide6 import QtSvg  # noqa: F401

class ThemeManager:
    # Subdirectories (relative to the resolved asset root) for bundled files.
    _ICONS_SUBDIR = os.path.join("assets", "icons")
    _THEMES_SUBDIR = os.path.join("assets", "themes")

    # ----------------------------------------------------------------------
    # Design-system palettes — the single source of truth for theme colours.
    #
    # Every theme exposes the SAME four semantic keys so widgets can pull a
    # colour without hard-coding a hex literal (which is how black letterbox
    # bars and mismatched panels crept in):
    #   bg      — app/window background (the darkest dark / lightest light)
    #   surface — raised panels, cards, the video letterbox backing
    #   text    — primary foreground text
    #   border  — separators and outlines
    # ----------------------------------------------------------------------
    DARK = {
        "bg": "#1E1F22",
        "surface": "#2B2D31",
        "text": "#DBDEE1",
        "border": "#4E5058",
    }
    LIGHT = {
        "bg": "#F2F3F5",
        "surface": "#FFFFFF",
        "text": "#313338",
        "border": "#E3E5E8",
    }

    # ----------------------------------------------------------------------
    # Typography — the semantic type scale; the ONLY font sizes allowed.
    #
    # The base family is set once via app.setFont() in main.py; these sizes
    # are the entire design system. No widget or stylesheet may invent its own
    # font-size/font-family — it references a constant here, or opts a QLabel
    # into a tier via the dynamic "txt" property (see _typography_qss):
    #   FONT_CAPTION — captions, secondary metadata   -> QLabel[txt="caption"]
    #   FONT_BASE    — default body text (app.setFont) -> QLabel[txt="body"]
    #   FONT_HEADER  — H2: card / section titles       -> QLabel[txt="h2"]
    #   FONT_H1      — H1: top-level emphasis          -> QLabel[txt="h1"]
    # ----------------------------------------------------------------------
    FONT_CAPTION = 11
    FONT_BASE = 13
    FONT_HEADER = 16
    FONT_H1 = 20

    # ----------------------------------------------------------------------
    # Metrics — the semantic sizing scale for interactive controls.
    #
    # Button heights were previously hard-coded at the call site (54 for the
    # scan row, 40 for the bottom action bar, 48-wide for the trash square),
    # which broke the vertical rhythm: every action button rendered at a
    # different height. These constants are the single source of truth so
    # every primary action shares one height and icon-only buttons form a
    # perfect square. No widget may invent its own button geometry — it
    # references a constant here.
    #   BUTTON_HEIGHT_PRIMARY — text action buttons (Scan, Compare, Move,
    #                           Back, Confirm): one shared height.
    #   BUTTON_HEIGHT_ICON    — square icon-only buttons (Trash): used as
    #                           BOTH width and height so the button is a
    #                           perfect square aligned to the primary row.
    # ----------------------------------------------------------------------
    BUTTON_HEIGHT_PRIMARY = 40
    BUTTON_HEIGHT_ICON = 40

    # ----------------------------------------------------------------------
    # Icon glyph registry — единый реестр векторных глифов приложения.
    #
    # Inline-SVG path-data (Material-style, viewBox 0 0 24 24) вместо emoji или
    # PNG-ассетов: вектор не мылится на Retina, не зависит от шрифтов ОС и
    # красится под активную тему в момент сборки иконки (make_icon).
    # Здесь живут ВСЕ глифы UI: плеер (play/pause/volume/volume_muted) и
    # адаптивная панель сканирования (scan/stop). Виджеты не имеют права
    # держать собственные SVG-литералы — только ссылаться на этот реестр.
    # ----------------------------------------------------------------------
    ICON_GLYPHS = {
        "play": "M8 5v14l11-7z",
        "pause": "M6 19h4V5H6v14zm8-14v14h4V5h-4z",
        "stop": "M6 6h12v12H6z",
        # Лупа — компактный режим кнопки «Сканировать»
        "scan": ("M15.5 14h-.79l-.28-.27C15.41 12.59 16 11.11 16 9.5 16 5.91 "
                 "13.09 3 9.5 3S3 5.91 3 9.5 5.91 16 9.5 16c1.61 0 3.09-.59 "
                 "4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 "
                 "11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"),
        # Звук включён (unmute)
        "volume": ("M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05"
                   "c1.48-.73 2.5-2.25 2.5-4.02z"),
        # Звук выключен (mute) — динамик с перечёркиванием
        "volume_muted": ("M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45"
                         "c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64"
                         "l1.51 1.51C20.63 14.91 21 13.5 21 12c0-4.28-2.99-7.86"
                         "-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3 3 4.27 "
                         "7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 "
                         "1.18v2.06c1.38-.31 2.63-.95 3.69-1.81L19.73 21 21 "
                         "19.73l-9-9L4.27 3zM12 4 9.91 6.09 12 8.18V4z"),
    }

    @classmethod
    def make_icon(cls, glyph_name: str, color: str) -> QIcon:
        """Собирает QIcon из глифа реестра: path-data оборачивается в <svg>,
        заливка красится в `color`, растеризация через SVG image-handler.
        Неизвестный глиф деградирует в пустую QIcon (не роняет UI)."""
        path_d = cls.ICON_GLYPHS.get(glyph_name)
        if not path_d:
            try:
                from utils.logger import auditor
                auditor.warning(f"ThemeManager: unknown icon glyph '{glyph_name}'")
            except Exception:
                pass
            return QIcon()
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
            f'width="48" height="48"><path fill="{color}" d="{path_d}"/></svg>'
        )
        pixmap = QPixmap()
        pixmap.loadFromData(QByteArray(svg.encode("utf-8")), "SVG")
        return QIcon(pixmap)

    # The palette currently applied to the app; widgets read it via colors().
    # Defaults to DARK because that is the theme MainWindow applies on startup.
    _active = DARK

    @classmethod
    def colors(cls) -> dict:
        """Return the semantic colour map (bg/surface/text/border) for the
        theme currently applied to the application."""
        return cls._active

    @staticmethod
    def _resource_root() -> Path:
        """Resolve the root directory that holds bundled resources.

        Explicitly honours PyInstaller's ``sys._MEIPASS`` so SVG/PNG/ICNS and
        ``.qss`` theme files load correctly from a frozen macOS ``.app`` bundle
        (and Windows onefile/onedir builds). Falls back to the project root when
        running from source.
        """
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).resolve().parent
            # macOS .app: data files live in Contents/Resources, next to MacOS/.
            if sys.platform == "darwin":
                mac_resources = exe_dir.parent / "Resources"
                if mac_resources.exists():
                    return mac_resources
            return exe_dir
        return Path(__file__).resolve().parent.parent


    @classmethod
    def icon_path(cls, filename: str) -> str:
        """Absolute path to a bundled icon (SVG/PNG/ICNS)."""
        return str(cls._resource_root() / cls._ICONS_SUBDIR / filename)

    @classmethod
    def load_icon(cls, filename: str) -> QIcon:
        """Load a bundled icon, degrading to an empty QIcon on any failure.

        A missing or invalid file never raises, so a packaging slip cannot crash
        startup — the window simply shows no custom icon.
        """
        path = cls.icon_path(filename)
        if os.path.exists(path):
            icon = QIcon(path)
            if not icon.isNull():
                return icon
        try:
            from utils.logger import auditor
            auditor.warning(f"ThemeManager: icon asset missing or invalid: {path}")
        except Exception:
            pass
        return QIcon()

    @classmethod
    def _load_stylesheet(cls, theme_name: str, fallback_qss: str) -> str:
        """Return the stylesheet for ``theme_name``.

        Prefers an external override at ``assets/themes/<theme_name>.qss`` so the
        theme can be tuned without code changes; on any failure (missing file, or
        a permission/encoding error inside a misconfigured bundle) it returns the
        embedded ``fallback_qss`` string so the UI is always styled.
        """
        qss_path = cls._resource_root() / cls._THEMES_SUBDIR / f"{theme_name}.qss"
        try:
            if qss_path.is_file():
                text = qss_path.read_text(encoding="utf-8")
                if text.strip():
                    return text
        except OSError as exc:
            try:
                from utils.logger import auditor
                auditor.warning(f"ThemeManager: failed to read {qss_path}: {exc}")
            except Exception:
                pass
        return fallback_qss

    @classmethod
    def _typography_qss(cls) -> str:
        """Theme-agnostic semantic type scale, appended to every stylesheet.

        Widgets opt in with a dynamic property (``label.setProperty("txt","h2")``)
        instead of hard-coding a font-size literal. Only SIZE/WEIGHT live here —
        colour stays theme-driven via the base ``QLabel`` rules, which Qt merges
        with these more-specific attribute selectors.
        """
        return (
            f'QLabel[txt="h1"] {{ font-size: {cls.FONT_H1}px; font-weight: bold; }}'
            f'QLabel[txt="h2"] {{ font-size: {cls.FONT_HEADER}px; font-weight: bold; }}'
            f'QLabel[txt="body"] {{ font-size: {cls.FONT_BASE}px; }}'
            f'QLabel[txt="caption"] {{ font-size: {cls.FONT_CAPTION}px; }}'
        )

    @classmethod
    def apply_modern_dark(cls, app: QApplication):
        cls._active = cls.DARK
        app.setStyle("Fusion")
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(cls.DARK["bg"]))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(cls.DARK["text"]))
        palette.setColor(QPalette.ColorRole.Base, QColor(cls.DARK["surface"]))
        palette.setColor(QPalette.ColorRole.Text, QColor(cls.DARK["text"]))
        palette.setColor(QPalette.ColorRole.Button, QColor(64, 66, 73))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor(cls.DARK["text"]))
        app.setPalette(palette)

        qss = """
        QMainWindow, QWidget#sidebar { background-color: #1E1F22; }
        QWidget#card { background-color: #2B2D31; border-radius: 8px; }
        QWidget#toolbar_flat { background-color: #2B2D31; border-radius: 6px; }
        QWidget#controls_panel, QWidget#bottom_btns, QWidget#multi_slider_panel { background-color: #2B2D31; border-top: 1px solid #1E1F22; }
        QWidget#video_bg { background-color: #2B2D31; }
        
        QPushButton { 
            background-color: #404249; color: #DBDEE1; 
            border: none; border-radius: 6px; 
            padding: 5px 12px; font-weight: 500; 
        }
        QPushButton:hover { background-color: #4E5058; }
        QPushButton:pressed { background-color: #313338; }
        QPushButton:disabled { background-color: #313338; color: #5C5E66; }
        
        QPushButton#primary { background-color: #23A559; color: white; font-weight: bold; }
        QPushButton#primary:hover { background-color: #1D8A4A; }
        QPushButton#action { background-color: #5865F2; color: white; }
        QPushButton#action:hover { background-color: #4752C4; }
        
        QPushButton#secondary { 
            background-color: transparent; 
            border: 1px solid #4E5058; 
            border-radius: 6px;
            padding: 4px 10px; 
            color: #DBDEE1;
        }
        QPushButton#secondary:hover { background-color: #3F4147; border: 1px solid #5865F2; }
        
        QPushButton#collapser { background-color: transparent; color: #949BA4; padding: 4px 8px; }
        QPushButton#collapser:hover { background-color: #3F4147; color: #FFFFFF; }
        
        QPushButton#player_btn { background-color: transparent; color: #FFFFFF; font-weight: bold; padding: 0px; }
        QPushButton#player_btn:hover { color: #5865F2; }
        
        QLineEdit { 
            background-color: #1E1F22; color: #FFFFFF; 
            border: 1px solid #4E5058; border-radius: 6px; 
            padding: 4px 10px; selection-background-color: #5865F2;
        }
        QLineEdit:focus { border: 1px solid #5865F2; }
        
        QCheckBox { spacing: 8px; color: #DBDEE1; }
        QCheckBox::indicator, QRadioButton::indicator { width: 16px; height: 16px; border-radius: 4px; border: 2px solid #5865F2; background: transparent; }
        QRadioButton::indicator { border-radius: 8px; }
        QCheckBox::indicator:checked, QRadioButton::indicator:checked { background: #5865F2; border: 2px solid #5865F2; }
        
        QTreeWidget#tree { background-color: #2B2D31; border: none; outline: none; border-radius: 8px; padding: 5px; color: #DBDEE1; }
        QTreeWidget::item { padding: 4px; border-radius: 4px; }
        QTreeWidget::item:selected { background-color: #3F4147; color: white; }
        QHeaderView::section { background-color: #1E1F22; color: #949BA4; border: none; padding: 4px 8px; font-weight: bold; }
        
        /* combobox-popup: 0 forces the non-native dropdown list so item metrics
           come from QSS (not the macOS popup delegate); without it the highlight
           rect and the click hitbox desync on macOS/Retina. */
        QComboBox {
            background-color: transparent; color: #DBDEE1;
            border: 1px solid #4E5058; border-radius: 6px;
            padding: 4px 6px 4px 6px;
            combobox-popup: 0;
            outline: none;
        }
        QComboBox:hover { background-color: #3F4147; border: 1px solid #5865F2; }
        QComboBox:focus, QComboBox:on { border: 1px solid #5865F2; }
        QComboBox::drop-down {
            subcontrol-origin: padding; subcontrol-position: center right;
            width: 20px; border: none; background: transparent;
        }
        QComboBox::down-arrow { width: 12px; height: 12px; }
        QComboBox::down-arrow:on { top: 1px; }

        QComboBox QAbstractItemView {
            background-color: #2B2D31; color: #DBDEE1;
            border: 1px solid #4E5058; border-radius: 6px;
            padding: 4px; outline: none;
            selection-background-color: #5865F2; selection-color: #FFFFFF;
        }
        QComboBox QAbstractItemView::item {
            min-height: 26px; padding: 4px 10px;
            border: none; border-radius: 4px;
        }
        QComboBox QAbstractItemView::item:hover { background-color: #3F4147; color: #FFFFFF; }
        QComboBox QAbstractItemView::item:selected { background-color: #5865F2; color: #FFFFFF; }
        
        QLabel { color: #DBDEE1; }
        QLabel#status, QLabel#elide_label { color: #949BA4; }
        QLabel#stat_val { color: #23A559; font-weight: bold; }
        QLabel#player_time { color: #FFFFFF; font-weight: bold; }
        
        /* Crisp 1px hairline divider (handleWidth is 1px in code). A flat tone
           keeps it minimal; it lights up with the accent only on hover/drag so
           the splitter never reads as a thick native macOS bar. */
        QSplitter::handle:horizontal { width: 1px; background-color: #2B2D31; }
        QSplitter::handle:vertical { height: 1px; background-color: #2B2D31; }
        QSplitter::handle:hover { background-color: #5865F2; }
        QSplitter::handle:pressed { background-color: #4752C4; }
        QProgressBar { border: none; background-color: #1E1F22; border-radius: 2px; }
        QProgressBar::chunk { background-color: #5865F2; border-radius: 2px; }
        
        QSlider { background: transparent; height: 24px; }
        QSlider::groove:horizontal { border: none; height: 4px; background: #1E1F22; border-radius: 2px; }
        QSlider::sub-page:horizontal { background: #5865F2; border-radius: 2px; }
        QSlider::handle:horizontal { background: #FFFFFF; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; border: 1px solid #1E1F22; }
        QSlider::handle:horizontal:hover { background: #4D8BFF; }
        """
        app.setStyleSheet(cls._load_stylesheet("dark", qss) + cls._typography_qss())

    @classmethod
    def apply_modern_light(cls, app: QApplication):
        cls._active = cls.LIGHT
        app.setStyle("Fusion")
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(cls.LIGHT["bg"]))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(cls.LIGHT["text"]))
        palette.setColor(QPalette.ColorRole.Base, QColor(cls.LIGHT["surface"]))
        palette.setColor(QPalette.ColorRole.Text, QColor(cls.LIGHT["text"]))
        palette.setColor(QPalette.ColorRole.Button, QColor(cls.LIGHT["border"]))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor(cls.LIGHT["text"]))
        app.setPalette(palette)

        qss = """
        QMainWindow, QWidget#sidebar { background-color: #F2F3F5; }
        QWidget#card { background-color: #FFFFFF; border-radius: 8px; border: 1px solid #E3E5E8; }
        QWidget#toolbar_flat { background-color: #FFFFFF; border-radius: 6px; border: 1px solid #E3E5E8; }
        QWidget#controls_panel, QWidget#bottom_btns, QWidget#multi_slider_panel { background-color: #FFFFFF; border-top: 1px solid #E3E5E8; }
        QWidget#video_bg { background-color: #FFFFFF; }
        
        QPushButton { background-color: #E3E5E8; color: #313338; border: none; border-radius: 6px; padding: 4px 10px; font-weight: 500; }
        QPushButton:hover { background-color: #D4D7DC; }
        QPushButton:pressed { background-color: #B5BAC1; }
        QPushButton:disabled { background-color: #E3E5E8; color: #949BA4; }
        
        QPushButton#primary { background-color: #23A559; color: white; font-weight: bold; }
        QPushButton#primary:hover { background-color: #1D8A4A; }
        QPushButton#action { background-color: #5865F2; color: white; }
        QPushButton#action:hover { background-color: #4752C4; }
        
        QPushButton#secondary { 
            background-color: transparent; 
            border: 1px solid #D4D7DC; 
            border-radius: 6px; 
            padding: 4px 10px; 
            color: #313338; 
        }
        QPushButton#secondary:hover { background-color: #E3E5E8; border: 1px solid #5865F2; }
        
        QPushButton#collapser { background-color: transparent; color: #5C5E66; padding: 4px 8px; }
        QPushButton#collapser:hover { background-color: #E3E5E8; color: #313338; }
        
        QPushButton#player_btn { background-color: transparent; color: #313338; font-weight: bold; padding: 0px; }
        QPushButton#player_btn:hover { color: #5865F2; }
        
        QLineEdit { 
            background-color: #FFFFFF; color: #313338; 
            border: 1px solid #D4D7DC; border-radius: 6px; 
            padding: 4px 10px; selection-background-color: #5865F2; selection-color: white;
        }
        QLineEdit:focus { border: 1px solid #5865F2; }
        
        QCheckBox { spacing: 8px; color: #313338; }
        QCheckBox::indicator, QRadioButton::indicator { width: 16px; height: 16px; border-radius: 4px; border: 2px solid #5865F2; background: transparent; }
        QRadioButton::indicator { border-radius: 8px; }
        QCheckBox::indicator:checked, QRadioButton::indicator:checked { background: #5865F2; border: 2px solid #5865F2; }
        
        QTreeWidget#tree { background-color: #FFFFFF; border: 1px solid #E3E5E8; outline: none; border-radius: 8px; padding: 5px; color: #313338; }
        QTreeWidget::item { padding: 4px; border-radius: 4px; }
        QTreeWidget::item:selected { background-color: #E3E5E8; color: #000000; }
        QHeaderView::section { background-color: #F2F3F5; color: #5C5E66; border: none; padding: 4px 8px; font-weight: bold; border-bottom: 1px solid #E3E5E8; }
        
        /* combobox-popup: 0 forces the non-native dropdown list so item metrics
           come from QSS (not the macOS popup delegate); without it the highlight
           rect and the click hitbox desync on macOS/Retina. */
        QComboBox {
            background-color: transparent; color: #313338;
            border: 1px solid #D4D7DC; border-radius: 6px;
            padding: 4px 6px 4px 6px;
            combobox-popup: 0;
            outline: none;
        }
        QComboBox:hover { background-color: #F2F3F5; border: 1px solid #5865F2; }
        QComboBox:focus, QComboBox:on { border: 1px solid #5865F2; }
        QComboBox::drop-down {
            subcontrol-origin: padding; subcontrol-position: center right;
            width: 20px; border: none; background: transparent;
        }
        QComboBox::down-arrow { width: 12px; height: 12px; }
        QComboBox::down-arrow:on { top: 1px; }

        QComboBox QAbstractItemView {
            background-color: #FFFFFF; color: #313338;
            border: 1px solid #D4D7DC; border-radius: 6px;
            padding: 4px; outline: none;
            selection-background-color: #5865F2; selection-color: #FFFFFF;
        }
        QComboBox QAbstractItemView::item {
            min-height: 26px; padding: 4px 10px;
            border: none; border-radius: 4px;
        }
        QComboBox QAbstractItemView::item:hover { background-color: #F2F3F5; color: #313338; }
        QComboBox QAbstractItemView::item:selected { background-color: #5865F2; color: #FFFFFF; }
        
        QLabel { color: #313338; }
        QLabel#status, QLabel#elide_label { color: #5C5E66; }
        QLabel#stat_val { color: #23A559; font-weight: bold; }
        QLabel#player_time { color: #313338; font-weight: bold; }
        
        /* Crisp 1px hairline divider (handleWidth is 1px in code). A flat tone
           keeps it minimal; it lights up with the accent only on hover/drag so
           the splitter never reads as a thick native macOS bar. */
        QSplitter::handle:horizontal { width: 1px; background-color: #E3E5E8; }
        QSplitter::handle:vertical { height: 1px; background-color: #E3E5E8; }
        QSplitter::handle:hover { background-color: #5865F2; }
        QSplitter::handle:pressed { background-color: #4752C4; }
        QProgressBar { border: none; background-color: #E3E5E8; border-radius: 2px; }
        QProgressBar::chunk { background-color: #5865F2; border-radius: 2px; }
        
        QSlider { background: transparent; height: 24px; }
        QSlider::groove:horizontal { border: none; height: 4px; background: #E3E5E8; border-radius: 2px; }
        QSlider::sub-page:horizontal { background: #5865F2; border-radius: 2px; }
        QSlider::handle:horizontal { background: #FFFFFF; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; border: 1px solid #D4D7DC; }
        QSlider::handle:horizontal:hover { background: #F2F3F5; }
        """
        app.setStyleSheet(cls._load_stylesheet("light", qss) + cls._typography_qss())

    @classmethod
    def apply_system_theme(cls, app: QApplication):
        app.setStyle("Fusion")
        std = app.style().standardPalette()
        app.setPalette(std)
        app.setStyleSheet(cls._typography_qss())
        # Keep colors() coherent with whatever the OS handed us: pick the
        # semantic map whose brightness matches the system window background,
        # so a video letterbox (and anything else reading colors()) blends in.
        window = std.color(QPalette.ColorRole.Window)
        cls._active = cls.DARK if window.lightnessF() < 0.5 else cls.LIGHT