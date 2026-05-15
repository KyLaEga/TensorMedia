# ============================================================
# MODULE: ui/components/video_player.py
# ============================================================
import os
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, 
                             QLabel, QComboBox, QCheckBox, QSlider, QSizePolicy)
from PySide6.QtCore import Qt, QUrl, QTimer
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget

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
        
        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.5) 
        self.player.setAudioOutput(self.audio_output)
        self._pending_seek = False 
        
        self.video_container = QWidget()
        self.video_container.setObjectName("video_bg") 
        self.video_container.setStyleSheet("background-color: #1E1E22; border-radius: 8px;") 
        
        vc_layout = QVBoxLayout(self.video_container)
        vc_layout.setContentsMargins(8, 8, 8, 8) 
        
        self.video_widget = QVideoWidget()
        self.video_widget.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.video_widget.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
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
        
        self.btn_play = QPushButton("Play")
        self.btn_play.setObjectName("player_btn")
        self.btn_play.setFixedWidth(50)
        self.btn_play.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_play.clicked.connect(self._toggle_play)
        cp_layout.addWidget(self.btn_play)
        
        self.btn_mute = QPushButton("Vol")
        self.btn_mute.setObjectName("player_btn")
        self.btn_mute.setFixedWidth(40)
        self.btn_mute.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_mute.clicked.connect(self._toggle_mute)
        cp_layout.addWidget(self.btn_mute)

        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(50)
        self.volume_slider.setFixedWidth(70)
        self.volume_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.volume_slider.valueChanged.connect(self._change_volume)
        cp_layout.addWidget(self.volume_slider)
        
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
        
        self.chk_autoplay = QCheckBox("Autoplay")
        self.chk_autoplay.setChecked(True)
        self.chk_autoplay.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        cp_layout.addWidget(self.chk_autoplay)
        
        layout.addWidget(controls_panel)

    def load_video(self, path: str):
        self.stop()
        
        if not os.path.exists(path):
            from utils.logger import auditor
            auditor.error(f"Video file missing: {path}")
            return
            
        self.player.setSource(QUrl.fromLocalFile(os.path.abspath(path)))
        
        if self.chk_autoplay.isChecked():
            self._pending_seek = False
            self.player.play()
            self.btn_play.setText("Pause")
        else:
            self._pending_seek = True
            self.btn_play.setText("Play")
            QTimer.singleShot(100, self.player.pause)

    def stop(self):
        if self.player.playbackState() != QMediaPlayer.PlaybackState.StoppedState:
            self.player.stop()

    def _toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.btn_play.setText("Play")
        else:
            self.player.play()
            self.btn_play.setText("Pause")

    def _toggle_mute(self):
        is_muted = not self.audio_output.isMuted()
        self.audio_output.setMuted(is_muted)
        self.btn_mute.setText("Mute" if is_muted else "Vol")

    def _change_volume(self, value):
        self.audio_output.setVolume(value / 100.0)
        if value == 0:
            self.btn_mute.setText("Mute")
        elif self.audio_output.isMuted():
            self.audio_output.setMuted(False)
            self.btn_mute.setText("Vol")
            
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