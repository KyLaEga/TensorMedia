from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPalette, QColor

class ThemeManager:
    @staticmethod
    def apply_modern_dark(app: QApplication):
        app.setStyle("Fusion")
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(30, 31, 34))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(219, 222, 225))
        palette.setColor(QPalette.ColorRole.Base, QColor(43, 45, 49))
        palette.setColor(QPalette.ColorRole.Text, QColor(219, 222, 225))
        palette.setColor(QPalette.ColorRole.Button, QColor(64, 66, 73))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor(219, 222, 225))
        app.setPalette(palette)

        qss = """
        QWidget { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; font-size: 14px; }
        QMainWindow, QWidget#sidebar { background-color: #1E1F22; }
        QWidget#card { background-color: #2B2D31; border-radius: 8px; }
        QWidget#toolbar_flat { background-color: #2B2D31; border-radius: 6px; }
        QWidget#controls_panel, QWidget#bottom_btns, QWidget#multi_slider_panel { background-color: #2B2D31; border-top: 1px solid #1E1F22; }
        QWidget#video_bg { background-color: #000000; }
        
        QPushButton { 
            background-color: #404249; color: #DBDEE1; 
            border: none; border-radius: 6px; 
            padding: 5px 12px; font-weight: 500; 
        }
        QPushButton:hover { background-color: #4E5058; }
        QPushButton:pressed { background-color: #313338; }
        QPushButton:disabled { background-color: #313338; color: #5C5E66; }
        
        QPushButton#primary { background-color: #23A559; color: white; font-weight: bold; font-size: 15px; }
        QPushButton#primary:hover { background-color: #1D8A4A; }
        QPushButton#action { background-color: #5865F2; color: white; font-size: 14px; }
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
        
        QPushButton#player_btn { background-color: transparent; color: #FFFFFF; font-size: 18px; font-weight: bold; padding: 0px; }
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
        
        QComboBox { 
            background-color: transparent; color: #DBDEE1; 
            border: 1px solid #4E5058; border-radius: 6px; 
            padding: 4px 8px 4px 8px; 
        }
        QComboBox:hover { background-color: #3F4147; border: 1px solid #5865F2; }
        QComboBox:focus { border: 1px solid #5865F2; }
        QComboBox::drop-down { border: none; width: 14px; }
        QComboBox::down-arrow { image: none; }
        
        QComboBox QAbstractItemView { 
            background-color: #2B2D31; color: #DBDEE1; 
            border-radius: 6px; border: 1px solid #4E5058; 
            selection-background-color: #5865F2; outline: none;
        }
        
        QLabel { color: #DBDEE1; }
        QLabel#status, QLabel#elide_label { color: #949BA4; font-size: 13px; }
        QLabel#stat_val { color: #23A559; font-weight: bold; }
        QLabel#player_time { color: #FFFFFF; font-size: 12px; font-weight: bold; }
        
        QSplitter::handle { background-color: #1E1F22; }
        QProgressBar { border: none; background-color: #1E1F22; border-radius: 2px; }
        QProgressBar::chunk { background-color: #5865F2; border-radius: 2px; }
        
        QSlider { background: transparent; height: 24px; }
        QSlider::groove:horizontal { border: none; height: 4px; background: #1E1F22; border-radius: 2px; }
        QSlider::sub-page:horizontal { background: #5865F2; border-radius: 2px; }
        QSlider::handle:horizontal { background: #FFFFFF; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; border: 1px solid #1E1F22; }
        QSlider::handle:horizontal:hover { background: #4D8BFF; transform: scale(1.1); }
        """
        app.setStyleSheet(qss)

    @staticmethod
    def apply_modern_light(app: QApplication):
        app.setStyle("Fusion")
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(242, 243, 245))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(49, 51, 56))
        palette.setColor(QPalette.ColorRole.Base, QColor(255, 255, 255))
        palette.setColor(QPalette.ColorRole.Text, QColor(49, 51, 56))
        palette.setColor(QPalette.ColorRole.Button, QColor(227, 229, 232))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor(49, 51, 56))
        app.setPalette(palette)

        qss = """
        QWidget { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; font-size: 14px; }
        QMainWindow, QWidget#sidebar { background-color: #F2F3F5; }
        QWidget#card { background-color: #FFFFFF; border-radius: 8px; border: 1px solid #E3E5E8; }
        QWidget#toolbar_flat { background-color: #FFFFFF; border-radius: 6px; border: 1px solid #E3E5E8; }
        QWidget#controls_panel, QWidget#bottom_btns, QWidget#multi_slider_panel { background-color: #FFFFFF; border-top: 1px solid #E3E5E8; }
        QWidget#video_bg { background-color: #E3E5E8; }
        
        QPushButton { background-color: #E3E5E8; color: #313338; border: none; border-radius: 6px; padding: 4px 10px; font-weight: 500; }
        QPushButton:hover { background-color: #D4D7DC; }
        QPushButton:pressed { background-color: #B5BAC1; }
        QPushButton:disabled { background-color: #E3E5E8; color: #949BA4; }
        
        QPushButton#primary { background-color: #23A559; color: white; font-weight: bold; font-size: 15px; }
        QPushButton#primary:hover { background-color: #1D8A4A; }
        QPushButton#action { background-color: #5865F2; color: white; font-size: 14px; }
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
        
        QPushButton#player_btn { background-color: transparent; color: #313338; font-size: 18px; font-weight: bold; padding: 0px; }
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
        
        QComboBox { 
            background-color: transparent; color: #313338; 
            border: 1px solid #D4D7DC; border-radius: 6px; 
            padding: 4px 8px 4px 8px; 
        }
        QComboBox:hover { background-color: #F2F3F5; border: 1px solid #5865F2; }
        QComboBox:focus { border: 1px solid #5865F2; }
        QComboBox::drop-down { border: none; width: 14px; }
        QComboBox::down-arrow { image: none; }
        
        QComboBox QAbstractItemView { 
            background-color: #FFFFFF; color: #313338; 
            border-radius: 6px; border: 1px solid #D4D7DC; 
            selection-background-color: #5865F2; selection-color: white; outline: none;
        }
        
        QLabel { color: #313338; }
        QLabel#status, QLabel#elide_label { color: #5C5E66; font-size: 13px; }
        QLabel#stat_val { color: #23A559; font-weight: bold; }
        QLabel#player_time { color: #313338; font-size: 12px; font-weight: bold; }
        
        QSplitter::handle { background-color: #E3E5E8; }
        QProgressBar { border: none; background-color: #E3E5E8; border-radius: 2px; }
        QProgressBar::chunk { background-color: #5865F2; border-radius: 2px; }
        
        QSlider { background: transparent; height: 24px; }
        QSlider::groove:horizontal { border: none; height: 4px; background: #E3E5E8; border-radius: 2px; }
        QSlider::sub-page:horizontal { background: #5865F2; border-radius: 2px; }
        QSlider::handle:horizontal { background: #FFFFFF; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; border: 1px solid #D4D7DC; }
        QSlider::handle:horizontal:hover { background: #F2F3F5; transform: scale(1.1); }
        """
        app.setStyleSheet(qss)

    @staticmethod
    def apply_system_theme(app: QApplication):
        app.setStyle("Fusion")
        app.setPalette(app.style().standardPalette())
        app.setStyleSheet("")