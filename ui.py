"""
ui.py — CameraUI
职责：
  - 微单风格 HUD overlay（参数显示：ISO/快门/FPS/曝光补偿）
  - 拍摄按钮 + 快捷键绑定
  - 参数调节面板（ISO、快门速度、白平衡）
  - dev 模式下负责拉帧显示 OpenCV 预览

设计原则：
  - 深色半透明 HUD，类似富士/索尼微单的取景器风格
  - 字体选 monospace，数字对齐好看
  - 状态变化有短暂高亮动画
"""

import sys
from typing import Optional, Callable
from pathlib import Path

IS_PI = sys.platform in ("linux", "linux2")

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QLabel, QPushButton,
    QVBoxLayout, QHBoxLayout, QComboBox, QSlider,
    QFrame, QSizePolicy,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont, QColor, QPalette, QImage, QPixmap

import numpy as np


# ─── HUD 颜色常量 ────────────────────────────────────────────
HUD_BG = "rgba(0, 0, 0, 180)"       # 半透明黑底
HUD_TEXT = "#FFFFFF"                  # 主文字白色
HUD_DIM = "#888888"                   # 次要文字灰色
HUD_ACCENT = "#FFD700"                # 高亮金色（手动模式指示）
HUD_CAPTURE = "#FF4444"               # 拍摄红点

HUD_STYLE = f"""
QWidget {{
    background: transparent;
    color: {HUD_TEXT};
    font-family: 'SF Mono', 'Consolas', 'Courier New', monospace;
}}
QLabel#param {{
    font-size: 22px;
    font-weight: bold;
    color: {HUD_TEXT};
    letter-spacing: 1px;
}}
QLabel#param_label {{
    font-size: 11px;
    color: {HUD_DIM};
    text-transform: uppercase;
    letter-spacing: 2px;
}}
QLabel#param_manual {{
    font-size: 22px;
    font-weight: bold;
    color: {HUD_ACCENT};
}}
QPushButton#shutter {{
    background: {HUD_CAPTURE};
    border: 2px solid #FF6666;
    border-radius: 28px;
    width: 56px;
    height: 56px;
    font-size: 1px;
}}
QPushButton#shutter:pressed {{
    background: #CC0000;
}}
QPushButton#ctrl_btn {{
    background: rgba(255,255,255,20);
    border: 1px solid rgba(255,255,255,60);
    border-radius: 6px;
    color: {HUD_TEXT};
    font-size: 13px;
    padding: 4px 12px;
}}
QPushButton#ctrl_btn:pressed {{
    background: rgba(255,255,255,50);
}}
QComboBox {{
    background: rgba(255,255,255,20);
    border: 1px solid rgba(255,255,255,60);
    border-radius: 4px;
    color: {HUD_TEXT};
    padding: 2px 6px;
    font-size: 13px;
}}
"""


class ParamWidget(QWidget):
    """单个参数显示块：标签 + 值，点击可弹出调节"""

    def __init__(self, label: str, value: str = "—", parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(1)

        self.value_label = QLabel(value)
        self.value_label.setObjectName("param")
        self.value_label.setAlignment(Qt.AlignCenter)

        self.name_label = QLabel(label)
        self.name_label.setObjectName("param_label")
        self.name_label.setAlignment(Qt.AlignCenter)

        layout.addWidget(self.value_label)
        layout.addWidget(self.name_label)

    def set_value(self, value: str, manual: bool = False):
        self.value_label.setText(value)
        self.value_label.setObjectName("param_manual" if manual else "param")
        # 触发样式刷新
        self.value_label.style().unpolish(self.value_label)
        self.value_label.style().polish(self.value_label)


class TopHUD(QWidget):
    """顶部 HUD 条：ISO / 快门 / FPS / AWB"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {HUD_BG}; border-radius: 8px;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(0)

        self.iso_w = ParamWidget("ISO")
        self.shutter_w = ParamWidget("SHUTTER")
        self.fps_w = ParamWidget("FPS")
        self.awb_w = ParamWidget("AWB")

        for w in [self.iso_w, self.shutter_w, self.fps_w, self.awb_w]:
            layout.addWidget(w)
            # 分隔线
            sep = QFrame()
            sep.setFrameShape(QFrame.VLine)
            sep.setStyleSheet("color: rgba(255,255,255,30);")
            layout.addWidget(sep)

        layout.addStretch()

        # 录制指示点（拍摄时亮起）
        self.capture_dot = QLabel("●")
        self.capture_dot.setStyleSheet(f"color: {HUD_CAPTURE}; font-size: 14px;")
        self.capture_dot.setVisible(False)
        layout.addWidget(self.capture_dot)

    def update_params(self, state, config):
        # ISO
        if config.iso is None:
            self.iso_w.set_value(f"A {state.actual_iso}", manual=False)
        else:
            self.iso_w.set_value(str(config.iso), manual=True)

        # 快门
        if config.shutter_speed is None:
            us = state.actual_exposure_us
            self.shutter_w.set_value(self._format_shutter(us), manual=False)
        else:
            self.shutter_w.set_value(
                self._format_shutter(config.shutter_speed), manual=True
            )

        # FPS
        self.fps_w.set_value(f"{state.fps:.1f}")

        # AWB
        self.awb_w.set_value(config.awb_mode.upper())

        # 拍摄状态
        self.capture_dot.setVisible(state.is_capturing)

    @staticmethod
    def _format_shutter(us: int) -> str:
        """将微秒快门速度格式化为易读字符串"""
        if us == 0:
            return "AUTO"
        elif us >= 1_000_000:
            return f"{us/1_000_000:.1f}s"
        elif us >= 10_000:
            ms = us / 1000
            return f"1/{int(1000/ms)}"
        else:
            return f"1/{int(1_000_000/us)}"


class BottomControls(QWidget):
    """底部控制栏：拍摄按钮 + 快速参数调节"""

    capture_pressed = pyqtSignal()

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.setStyleSheet(f"background: {HUD_BG}; border-radius: 8px;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(16)

        # ISO 调节
        iso_label = QLabel("ISO")
        iso_label.setObjectName("param_label")
        self.iso_combo = QComboBox()
        self.iso_combo.addItems(["Auto", "100", "200", "400", "800", "1600", "3200"])
        self.iso_combo.currentTextChanged.connect(self._on_iso_changed)

        # 快门调节
        shutter_label = QLabel("SHUTTER")
        shutter_label.setObjectName("param_label")
        self.shutter_combo = QComboBox()
        self.shutter_combo.addItems([
            "Auto", "1/4000", "1/2000", "1/1000",
            "1/500", "1/250", "1/125", "1/60", "1/30", "1/15", "1s"
        ])
        self.shutter_combo.currentTextChanged.connect(self._on_shutter_changed)

        # AWB 调节
        awb_label = QLabel("AWB")
        awb_label.setObjectName("param_label")
        self.awb_combo = QComboBox()
        self.awb_combo.addItems(["auto", "daylight", "cloudy", "tungsten", "fluorescent"])
        self.awb_combo.currentTextChanged.connect(self._on_awb_changed)

        layout.addWidget(iso_label)
        layout.addWidget(self.iso_combo)
        layout.addSpacing(8)
        layout.addWidget(shutter_label)
        layout.addWidget(self.shutter_combo)
        layout.addSpacing(8)
        layout.addWidget(awb_label)
        layout.addWidget(self.awb_combo)
        layout.addStretch()

        # 拍摄按钮
        self.shutter_btn = QPushButton()
        self.shutter_btn.setObjectName("shutter")
        self.shutter_btn.setFixedSize(56, 56)
        self.shutter_btn.clicked.connect(self.capture_pressed.emit)
        layout.addWidget(self.shutter_btn)

    def _on_iso_changed(self, text):
        if text == "Auto":
            self.engine.set_iso(None)
        else:
            self.engine.set_iso(int(text))

    def _on_shutter_changed(self, text):
        if text == "Auto":
            self.engine.set_shutter_speed(None)
        else:
            # 解析 "1/1000" → 1000 us
            if text.startswith("1/"):
                denom = int(text[2:])
                us = 1_000_000 // denom
            elif text.endswith("s"):
                us = int(float(text[:-1]) * 1_000_000)
            else:
                us = None
            if us:
                self.engine.set_shutter_speed(us)

    def _on_awb_changed(self, text):
        self.engine.set_awb_mode(text)


class PreviewWidget(QLabel):
    """
    dev 模式下的预览显示区（Pi 上不用，直接 GPU 渲染）。
    把 numpy RGB array 转成 QPixmap 显示。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding
        )
        self.setStyleSheet("background: #111111;")
        self.setText("等待相机...")
        self.setStyleSheet("background: #111111; color: #555; font-size: 16px;")

    def set_frame(self, frame: np.ndarray):
        h, w, ch = frame.shape
        img = QImage(frame.data, w, h, ch * w, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(img).scaled(
            self.width(), self.height(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.setPixmap(pixmap)
        self.setStyleSheet("background: #111111;")


class CameraUI(QMainWindow):
    """
    主窗口。

    Pi 模式：
      - 无预览区（GPU 负责），只有 HUD 上下条
      - 窗口透明，浮在 picamera2 preview 上方
      - 依赖 compositor 或 Qt 的 WA_TranslucentBackground

    Dev 模式：
      - 中间有 PreviewWidget 显示 OpenCV 帧
      - 上下 HUD 条
    """

    def __init__(
        self,
        engine,
        output_dir: Path,
        on_capture: Callable,
        on_quit: Callable,
        dev_mode: bool = False,
        preview_widget=None,
        parent=None,
    ):
        super().__init__(parent)
        self.engine = engine
        self.output_dir = output_dir
        self.on_capture = on_capture
        self.on_quit = on_quit
        self.dev_mode = dev_mode
        self._preview_widget = preview_widget

        self._setup_window()
        self._setup_ui()

    def _setup_window(self):
        self.setWindowTitle("PiCamera2")

        if not self.dev_mode:
            # Pi 模式：全屏无边框窗口
            self.setWindowFlags(Qt.FramelessWindowHint)
            self.showFullScreen()
        else:
            # Dev 模式：普通窗口
            self.resize(1024, 640)

    def _setup_ui(self):
        central = QWidget()
        central.setObjectName("central")
        central.setStyleSheet(HUD_STYLE)
        self.setCentralWidget(central)

        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 顶部 HUD
        self.top_hud = TopHUD()
        main_layout.addWidget(self.top_hud)

        if self._preview_widget is not None:
            # Pi 模式：QPicamera2 嵌入式预览
            main_layout.addWidget(self._preview_widget, stretch=1)
        elif self.dev_mode:
            # Dev 模式：OpenCV 预览
            self.cv_preview = PreviewWidget()
            main_layout.addWidget(self.cv_preview, stretch=1)
        else:
            spacer = QWidget()
            spacer.setSizePolicy(
                QSizePolicy.Expanding, QSizePolicy.Expanding
            )
            main_layout.addWidget(spacer, stretch=1)

        # 底部控制栏
        self.bottom_controls = BottomControls(engine=self.engine)
        self.bottom_controls.capture_pressed.connect(self._on_capture)
        main_layout.addWidget(self.bottom_controls)

    # ─── 刷新方法（由 QTimer 调用）──────────────────────────

    def refresh_hud(self):
        """更新顶部参数显示"""
        self.top_hud.update_params(
            state=self.engine.state,
            config=self.engine.config,
        )

    def update_preview_frame(self):
        """dev 模式：从 camera engine 拉帧更新预览"""
        if not self.dev_mode:
            return
        frame = self.engine.read_frame()
        if frame is not None:
            self.cv_preview.set_frame(frame)

    # ─── 拍摄 ────────────────────────────────────────────────

    def _on_capture(self):
        """触发拍摄，并做一次视觉反馈（TODO：快门动画）"""
        path = self.on_capture()
        print(f"[UI] Capture triggered → {path}")

    # ─── 键盘快捷键 ──────────────────────────────────────────

    def keyPressEvent(self, event):
        key = event.key()

        if key in (Qt.Key_Space, Qt.Key_Return):
            # 空格/回车：拍照
            self._on_capture()

        elif key == Qt.Key_Q or key == Qt.Key_Escape:
            self.on_quit()

        elif key == Qt.Key_Up:
            # 上键：增大 ISO
            self._adjust_iso(+1)

        elif key == Qt.Key_Down:
            # 下键：减小 ISO
            self._adjust_iso(-1)

        elif key == Qt.Key_Right:
            # 右键：快门加速
            self._adjust_shutter(+1)

        elif key == Qt.Key_Left:
            # 左键：快门减速
            self._adjust_shutter(-1)

        else:
            super().keyPressEvent(event)

    def _adjust_iso(self, direction: int):
        iso_steps = [None, 100, 200, 400, 800, 1600, 3200]
        cur = self.engine.config.iso
        idx = iso_steps.index(cur) if cur in iso_steps else 0
        new_idx = max(0, min(len(iso_steps) - 1, idx + direction))
        self.engine.set_iso(iso_steps[new_idx])
        combo = self.bottom_controls.iso_combo
        combo.setCurrentText("Auto" if iso_steps[new_idx] is None else str(iso_steps[new_idx]))

    def _adjust_shutter(self, direction: int):
        shutter_us = [None, 250, 500, 1000, 2000, 4000, 8000, 16000, 33333, 66666, 1_000_000]
        cur = self.engine.config.shutter_speed
        idx = 0
        for i, v in enumerate(shutter_us):
            if v == cur:
                idx = i
                break
        new_idx = max(0, min(len(shutter_us) - 1, idx + direction))
        self.engine.set_shutter_speed(shutter_us[new_idx])
