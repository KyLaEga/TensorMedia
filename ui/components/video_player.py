from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, 
                             QLabel, QComboBox, QCheckBox, QSlider, QSizePolicy)
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtMultimediaWidgets import QVideoWidget

class JumpSlider(QSlider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            val = self.minimum() + ((self.maximum() - self.minimum()) * event.position().x()) / self.width()
            self.setValue(int(val))
            self.sliderMoved.emit(int(val))
            self.sliderReleased.emit() 
        super().mousePressEvent(event)

class BuiltInVideoPlayer(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.audio_output.setMuted(True) 
        self.player.setAudioOutput(self.audio_output)
        self._pending_seek = False 
        
        self.video_container = QWidget()
        self.video_container.setObjectName("video_bg") 
        self.video_container.setStyleSheet("background-color: #1E1E22; border-radius: 8px;") 
        
        vc_layout = QVBoxLayout(self.video_container)
        vc_layout.setContentsMargins(8, 8, 8, 8) 
        
        self.video_widget = QVideoWidget()
        self.video_widget.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self.video_widget.setMinimumSize(0, 0) 
        self.video_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        vc_layout.addWidget(self.video_widget)
        self.player.setVideoOutput(self.video_widget)
        
        self._setup_ui()
        self.player.positionChanged.connect(self._on_position_changed)
        self.player.durationChanged.connect(self._on_duration_changed)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.video_container, stretch=1)
        
        controls_panel = QWidget()
        controls_panel.setObjectName("controls_panel") 
        controls_panel.setFixedHeight(45)
        cp_layout = QHBoxLayout(controls_panel)
        cp_layout.setContentsMargins(10, 0, 10, 0)
        cp_layout.setSpacing(10)
        
        self.btn_play = QPushButton("⏸️")
        self.btn_play.setObjectName("player_btn")
        self.btn_play.setFixedWidth(30)
        self.btn_play.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_play.clicked.connect(self._toggle_play)
        cp_layout.addWidget(self.btn_play)
        
        self.btn_mute = QPushButton("🔇")
        self.btn_mute.setObjectName("player_btn")
        self.btn_mute.setFixedWidth(30)
        self.btn_mute.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_mute.clicked.connect(self._toggle_mute)
        cp_layout.addWidget(self.btn_mute)
        
        self.slider = JumpSlider(Qt.Orientation.Horizontal)
        self.slider.sliderMoved.connect(self._on_slider_moving_only_text)
        self.slider.sliderReleased.connect(self._execute_seek)
        cp_layout.addWidget(self.slider)
        
        self.lbl_time = QLabel("00:00")
        self.lbl_time.setObjectName("player_time")
        cp_layout.addWidget(self.lbl_time)
        
        self.combo_speed = QComboBox()
        self.combo_speed.addItems(["0.5x", "1.0x", "1.5x", "2.0x"])
        self.combo_speed.setCurrentIndex(1)
        self.combo_speed.setFixedWidth(65)
        self.combo_speed.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.combo_speed.currentTextChanged.connect(self._change_speed)
        cp_layout.addWidget(self.combo_speed)
        
        self.chk_autoplay = QCheckBox("Автоплей")
        self.chk_autoplay.setChecked(True)
        self.chk_autoplay.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        cp_layout.addWidget(self.chk_autoplay)
        
        layout.addWidget(controls_panel)

    def load_video(self, path: str):
        self.player.setSource(QUrl.fromLocalFile(path))
        if self.chk_autoplay.isChecked():
            self._pending_seek = False
            self.player.play()
            self.btn_play.setText("⏸️")
        else:
            self._pending_seek = True
            self.player.pause()
            self.btn_play.setText("▶️")

    def stop(self):
        self.player.stop()

    def _toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.btn_play.setText("▶️")
        else:
            self.player.play()
            self.btn_play.setText("⏸️")

    def _toggle_mute(self):
        is_muted = not self.audio_output.isMuted()
        self.audio_output.setMuted(is_muted)
        self.btn_mute.setText("🔇" if is_muted else "🔊")
        
    def _change_speed(self, text):
        speed = float(text.replace('x', ''))
        self.player.setPlaybackRate(speed)

    def _on_position_changed(self, position):
        if not self.slider.isSliderDown():
            self.slider.blockSignals(True)
            self.slider.setValue(position)
            self.slider.blockSignals(False)
        self._update_time_label(position)

    def _on_duration_changed(self, duration):
        self.slider.setRange(0, duration)
        if self._pending_seek and duration > 0:
            target = int(duration * 0.25)
            self.player.setPosition(target)
            self.slider.setValue(target)
            self._update_time_label(target)
            self._pending_seek = False

    def _on_slider_moving_only_text(self, position):
        self._update_time_label(position)
        
    def _execute_seek(self):
        self.player.setPosition(self.slider.value())

    def _update_time_label(self, ms_pos):
        pos = ms_pos // 1000
        self.lbl_time.setText(f"{pos//60:02d}:{pos%60:02d}")