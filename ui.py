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
import os
import struct
import threading
import glob
from enum import Enum, auto
from typing import Optional, Callable
from pathlib import Path

IS_PI = sys.platform in ("linux", "linux2")

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QLabel, QPushButton,
    QVBoxLayout, QHBoxLayout, QGridLayout, QComboBox,
    QFrame, QSizePolicy, QStackedWidget,
)
from PyQt5.QtCore import Qt, QTimer, QDateTime, QSize, pyqtSignal
from PyQt5.QtGui import QImage, QPixmap, QImageReader

import numpy as np


# ─── HUD 颜色常量 ────────────────────────────────────────────
HUD_BG      = "rgba(0, 0, 0, 180)"
HUD_TEXT    = "#FFFFFF"
HUD_DIM     = "#888888"
HUD_ACCENT  = "#FFD700"   # 金色：手动模式 / 当前选中值
HUD_CAPTURE = "#FF4444"   # 红：拍摄指示
HUD_SELECT  = "#FFFFFF"   # 白：MENU 模式聚焦边框
HUD_ADJUST  = "#00FFCC"   # 青：ADJUST 模式聚焦边框

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
QComboBox {{
    background: rgba(255,255,255,20);
    border: 1px solid rgba(255,255,255,60);
    border-radius: 4px;
    color: {HUD_TEXT};
    padding: 2px 6px;
    font-size: 13px;
}}
"""


# ─── 交互模式枚举 ─────────────────────────────────────────────
class UIMode(Enum):
    NORMAL = auto()   # Page Down 拍照，Page Up 进菜单
    MENU   = auto()   # Page Up 循环聚焦，Page Down 进调节
    ADJUST = auto()   # Page Up 增，Page Down 减，双击 Page Up 退出


class AppMode(Enum):
    CAMERA              = auto()  # 拍摄模式（沿用 UIMode 子状态）
    ALBUM_GRID          = auto()  # 4x3 缩略图
    ALBUM_SINGLE        = auto()  # 单图全屏
    ALBUM_DELETE_CONFIRM = auto() # 删除确认覆盖在 SINGLE 之上


# ─── 各参数选项（显示用字符串，顺序与内部步进列表对应）─────────
_PARAM_TITLES = ["ISO", "SHUTTER", "AWB"]

_STRIP_OPTIONS = [
    ["AUTO", "100", "200", "400", "800", "1600", "3200"],
    ["AUTO", "1/4000", "1/2000", "1/1000", "1/500", "1/250",
     "1/125", "1/60", "1/30", "1/15", "1s"],
    ["AUTO", "DAYLIGHT", "CLOUDY", "TUNGSTEN", "FLUORESCENT"],
]


class ParamWidget(QWidget):
    """单个参数显示块：标签 + 值"""

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

        self.iso_w     = ParamWidget("ISO")
        self.shutter_w = ParamWidget("SHUTTER")
        self.fps_w     = ParamWidget("FPS")
        self.awb_w     = ParamWidget("AWB")

        for w in [self.iso_w, self.shutter_w, self.awb_w, self.fps_w]:
            layout.addWidget(w)
            sep = QFrame()
            sep.setFrameShape(QFrame.VLine)
            sep.setStyleSheet("color: rgba(255,255,255,30);")
            layout.addWidget(sep)

        layout.addStretch()

        self.capture_dot = QLabel("●")
        self.capture_dot.setStyleSheet(f"color: {HUD_CAPTURE}; font-size: 14px;")
        self.capture_dot.setVisible(False)
        layout.addWidget(self.capture_dot)

    def update_params(self, state, config):
        if config.iso is None:
            self.iso_w.set_value(f"A {state.actual_iso}", manual=False)
        else:
            self.iso_w.set_value(str(config.iso), manual=True)

        if config.shutter_speed is None:
            self.shutter_w.set_value(self._format_shutter(state.actual_exposure_us), manual=False)
        else:
            self.shutter_w.set_value(self._format_shutter(config.shutter_speed), manual=True)

        self.fps_w.set_value(f"{state.fps:.1f}")
        self.awb_w.set_value(config.awb_mode.upper())
        self.capture_dot.setVisible(state.is_capturing)

    @staticmethod
    def _format_shutter(us: int) -> str:
        if us == 0:
            return "AUTO"
        elif us >= 1_000_000:
            return f"{us/1_000_000:.1f}s"
        elif us >= 10_000:
            return f"1/{int(1000 / (us / 1000))}"
        else:
            return f"1/{int(1_000_000 / us)}"


class OptionStrip(QWidget):
    """
    ADJUST 模式下浮动显示的竖向参数选项条，定位在当前参数上方。
    显示当前参数的所有可选值，当前值用金色高亮。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            "background: rgba(0,0,0,210); border-radius: 6px;"
        )
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

        self._options_col = QVBoxLayout(self)
        self._options_col.setContentsMargins(10, 6, 10, 6)
        self._options_col.setSpacing(2)

        self._option_labels: list[QLabel] = []
        self._current_idx = 0
        self._anchor = None
        self.hide()

    def show_options(self, options: list[str], current_idx: int, anchor_widget=None):
        # 清空旧选项
        for lbl in self._option_labels:
            lbl.setParent(None)
        self._option_labels.clear()
        while self._options_col.count():
            item = self._options_col.takeAt(0)
            if item.widget():
                item.widget().setParent(None)

        for opt in options:
            lbl = QLabel(opt)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setContentsMargins(8, 1, 8, 1)
            self._option_labels.append(lbl)
            self._options_col.addWidget(lbl)

        self._current_idx = current_idx
        self._refresh_styles()
        # 根据最长选项 + 16px bold 字体计算所需宽度
        from PyQt5.QtGui import QFont, QFontMetrics
        font = QFont(self.font())
        font.setPointSize(16)
        font.setBold(True)
        fm = QFontMetrics(font)
        max_text_w = max((fm.horizontalAdvance(opt) for opt in options), default=0)
        self.setMinimumWidth(max_text_w + 36)
        self.adjustSize()

        if anchor_widget is not None:
            self._anchor = anchor_widget
            self._position_above_anchor()

        self.show()
        self.raise_()

    def _position_above_anchor(self):
        if self._anchor is None or self.parent() is None:
            return
        anchor_top = self._anchor.mapTo(self.parent(), self._anchor.rect().topLeft())
        x = anchor_top.x() + (self._anchor.width() - self.width()) // 2
        y = anchor_top.y() - self.height() - 6
        x = max(4, min(self.parent().width() - self.width() - 4, x))
        y = max(4, y)
        self.move(x, y)

    def set_current(self, idx: int):
        self._current_idx = idx
        self._refresh_styles()

    def _refresh_styles(self):
        for i, lbl in enumerate(self._option_labels):
            if i == self._current_idx:
                lbl.setStyleSheet(
                    f"color: {HUD_ACCENT}; font-size: 16px; font-weight: bold;"
                )
            else:
                lbl.setStyleSheet(
                    "color: rgba(255,255,255,100); font-size: 11px;"
                )


class BottomControls(QWidget):
    """底部控制栏：拍摄按钮 + 快速参数调节（支持 MENU/ADJUST 聚焦高亮）"""

    capture_pressed = pyqtSignal()

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.setStyleSheet(f"background: {HUD_BG}; border-radius: 8px;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(8)

        # 每个参数用 QFrame 包裹，便于整体高亮
        self.iso_combo     = QComboBox()
        self.shutter_combo = QComboBox()
        self.awb_combo     = QComboBox()

        self.iso_combo.addItems(["Auto", "100", "200", "400", "800", "1600", "3200"])
        self.shutter_combo.addItems([
            "Auto", "1/4000", "1/2000", "1/1000",
            "1/500", "1/250", "1/125", "1/60", "1/30", "1/15", "1s"
        ])
        self.awb_combo.addItems(["auto", "daylight", "cloudy", "tungsten", "fluorescent"])

        self.iso_combo.currentTextChanged.connect(self._on_iso_changed)
        self.shutter_combo.currentTextChanged.connect(self._on_shutter_changed)
        self.awb_combo.currentTextChanged.connect(self._on_awb_changed)

        # 不让任何控件抢走主窗口的键盘焦点
        for w in (self.iso_combo, self.shutter_combo, self.awb_combo):
            w.setFocusPolicy(Qt.NoFocus)

        self._param_groups: list[QFrame] = []
        for label_text, combo in [
            ("ISO", self.iso_combo),
            ("SHUTTER", self.shutter_combo),
            ("AWB", self.awb_combo),
        ]:
            group = self._make_group(label_text, combo)
            self._param_groups.append(group)
            layout.addWidget(group)

        layout.addStretch()

        self.shutter_btn = QPushButton()
        self.shutter_btn.setObjectName("shutter")
        self.shutter_btn.setFixedSize(56, 56)
        self.shutter_btn.setFocusPolicy(Qt.NoFocus)
        self.shutter_btn.clicked.connect(self.capture_pressed.emit)
        layout.addWidget(self.shutter_btn)

    @staticmethod
    def _make_group(label_text: str, combo: QComboBox) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.NoFrame)
        frame.setStyleSheet("border-radius: 6px;")
        row = QHBoxLayout(frame)
        row.setContentsMargins(6, 4, 6, 4)
        row.setSpacing(6)
        lbl = QLabel(label_text)
        lbl.setObjectName("param_label")
        row.addWidget(lbl)
        row.addWidget(combo)
        return frame

    def get_param_group(self, idx: int) -> QFrame:
        return self._param_groups[idx]

    def set_focused(self, param_idx: Optional[int], border_color: Optional[str] = None):
        """高亮聚焦的参数组；param_idx=None 清除所有高亮"""
        for i, group in enumerate(self._param_groups):
            if i == param_idx and border_color:
                group.setStyleSheet(
                    f"background: rgba(255,255,255,12); "
                    f"border: 1px solid {border_color}; border-radius: 6px;"
                )
            else:
                group.setStyleSheet("border-radius: 6px;")

    def _on_iso_changed(self, text):
        if text == "Auto":
            self.engine.set_iso(None)
        else:
            self.engine.set_iso(int(text))

    def _on_shutter_changed(self, text):
        if text == "Auto":
            self.engine.set_shutter_speed(None)
        elif text.startswith("1/"):
            us = 1_000_000 // int(text[2:])
            self.engine.set_shutter_speed(us)
        elif text.endswith("s"):
            us = int(float(text[:-1]) * 1_000_000)
            self.engine.set_shutter_speed(us)

    def _on_awb_changed(self, text):
        self.engine.set_awb_mode(text)


class PreviewWidget(QLabel):
    """dev 模式下的预览显示区，把 numpy RGB array 转成 QPixmap 显示"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet("background: #111111; color: #555; font-size: 16px;")
        self.setText("等待相机...")

    def set_frame(self, frame: np.ndarray):
        h, w, ch = frame.shape
        img = QImage(frame.data, w, h, ch * w, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(img).scaled(
            self.width(), self.height(),
            Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
        self.setPixmap(pixmap)
        self.setStyleSheet("background: #111111;")


class AlbumView(QWidget):
    """
    相册视图。内部用 QStackedWidget 在 GRID（4x3 缩略图）和 SINGLE（单图全屏）之间切换。
    DELETE_CONFIRM 用浮动 label 覆盖在 SINGLE 上。

    导航由 CameraUI 通过下列方法驱动：
      enter_album / leave_album / prev / next / select / back_to_grid
      request_delete / confirm_delete / cancel_delete
    """

    GRID_COLS = 4
    GRID_ROWS = 3
    PAGE_SIZE = GRID_COLS * GRID_ROWS

    def __init__(self, output_dir: Path, parent=None):
        super().__init__(parent)
        self.output_dir = output_dir
        self.photos: list[Path] = []
        self.page = 0
        self.sel = 0

        self.setStyleSheet("background: #000;")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._stack = QStackedWidget(self)
        outer.addWidget(self._stack)

        self._build_grid_page()
        self._build_single_page()

    # ─── 页面构建 ───────────────────────────────────────────

    def _build_grid_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        grid_host = QWidget()
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(6, 6, 6, 6)
        grid.setSpacing(4)
        self._thumb_labels: list[QLabel] = []
        for r in range(self.GRID_ROWS):
            for c in range(self.GRID_COLS):
                lbl = QLabel()
                lbl.setAlignment(Qt.AlignCenter)
                lbl.setMinimumSize(180, 135)
                lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
                self._thumb_labels.append(lbl)
                grid.addWidget(lbl, r, c)
        v.addWidget(grid_host, stretch=1)

        self._grid_status = QLabel()
        self._grid_status.setAlignment(Qt.AlignCenter)
        self._grid_status.setStyleSheet(
            f"color: {HUD_DIM}; font-size: 11px; padding: 2px 8px;"
        )
        v.addWidget(self._grid_status)

        self._stack.addWidget(page)

    def _build_single_page(self):
        owner = self

        class SinglePage(QWidget):
            def resizeEvent(self, ev):
                owner._relayout_single()
                super().resizeEvent(ev)

        page = SinglePage()
        page.setStyleSheet("background: #000;")
        self._single_label = QLabel(page)
        self._single_label.setAlignment(Qt.AlignCenter)
        self._single_label.setStyleSheet("background: #000;")

        self._confirm_label = QLabel(page)
        self._confirm_label.setAlignment(Qt.AlignCenter)
        self._confirm_label.setStyleSheet(
            f"background: rgba(0,0,0,220); color: {HUD_TEXT}; "
            f"font-size: 16px; font-weight: bold; "
            f"border: 2px solid {HUD_CAPTURE}; border-radius: 10px; padding: 18px 24px;"
        )
        self._confirm_label.setText("删除这张照片？\n\nO = 确认    I/II = 取消")
        self._confirm_label.hide()

        self._single_info = QLabel(page)
        self._single_info.setStyleSheet(
            f"background: rgba(0,0,0,160); color: {HUD_TEXT}; "
            f"font-size: 11px; padding: 4px 10px; border-radius: 4px;"
        )
        self._single_info.setAttribute(Qt.WA_TransparentForMouseEvents)

        self._single_page = page
        self._stack.addWidget(page)

    def _relayout_single(self):
        w, h = self._single_page.width(), self._single_page.height()
        self._single_label.setGeometry(0, 0, w, h)
        self._render_single_pixmap()
        self._confirm_label.adjustSize()
        self._confirm_label.move(
            (w - self._confirm_label.width()) // 2,
            (h - self._confirm_label.height()) // 2,
        )
        self._single_info.adjustSize()
        self._single_info.move(10, 10)

    # ─── 公开 API ───────────────────────────────────────────

    def enter_album(self):
        """进入相册：刷新照片列表，回到 GRID 首页首项"""
        self.refresh_photos()
        self.page = 0
        self.sel = 0
        self._stack.setCurrentIndex(0)
        self._render_grid()

    def refresh_photos(self):
        if not self.output_dir.exists():
            self.photos = []
        else:
            self.photos = sorted(
                self.output_dir.glob("*.jpg"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )

    def prev(self):
        """GRID 选中上一张（含跨页）"""
        global_idx = self.page * self.PAGE_SIZE + self.sel
        if global_idx <= 0:
            return
        global_idx -= 1
        self.page = global_idx // self.PAGE_SIZE
        self.sel = global_idx % self.PAGE_SIZE
        self._render_grid()

    def next(self):
        global_idx = self.page * self.PAGE_SIZE + self.sel
        if global_idx >= len(self.photos) - 1:
            return
        global_idx += 1
        self.page = global_idx // self.PAGE_SIZE
        self.sel = global_idx % self.PAGE_SIZE
        self._render_grid()

    def select(self):
        """GRID → SINGLE"""
        if not self.photos:
            return False
        self._stack.setCurrentIndex(1)
        self._render_single_pixmap()
        return True

    def back_to_grid(self):
        self._confirm_label.hide()
        self._stack.setCurrentIndex(0)
        self._render_grid()

    def request_delete(self):
        self._confirm_label.show()
        self._confirm_label.raise_()
        self._relayout_single()

    def cancel_delete(self):
        self._confirm_label.hide()

    def confirm_delete(self):
        """删除当前选中照片（含同名 .dng）"""
        idx = self.page * self.PAGE_SIZE + self.sel
        if 0 <= idx < len(self.photos):
            target = self.photos[idx]
            try:
                target.unlink(missing_ok=True)
                dng = target.with_suffix(".dng")
                dng.unlink(missing_ok=True)
                print(f"[ALBUM] Deleted {target.name}")
            except Exception as e:
                print(f"[ALBUM] Delete failed: {e}")
        # 删除后回 GRID
        self.refresh_photos()
        total = len(self.photos)
        if total == 0:
            self.page = 0
            self.sel = 0
        else:
            new_idx = min(idx, total - 1)
            self.page = new_idx // self.PAGE_SIZE
            self.sel = new_idx % self.PAGE_SIZE
        self._confirm_label.hide()
        self._stack.setCurrentIndex(0)
        self._render_grid()

    def is_in_single(self) -> bool:
        return self._stack.currentIndex() == 1

    # ─── 渲染 ───────────────────────────────────────────────

    def _render_grid(self):
        start = self.page * self.PAGE_SIZE
        for i, lbl in enumerate(self._thumb_labels):
            idx = start + i
            highlighted = (i == self.sel and idx < len(self.photos))
            border = HUD_ACCENT if highlighted else "transparent"
            lbl.setStyleSheet(
                f"background: #1a1a1a; border: 2px solid {border};"
            )
            if idx < len(self.photos):
                self._load_thumb(lbl, self.photos[idx])
            else:
                lbl.clear()

        total_pages = max(1, (len(self.photos) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        if self.photos:
            cur = start + self.sel + 1
            self._grid_status.setText(
                f"{cur} / {len(self.photos)}    页 {self.page + 1}/{total_pages}"
            )
        else:
            self._grid_status.setText("无照片")

    def _load_thumb(self, label: QLabel, path: Path):
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        target_w = max(80, label.width() - 8)
        target_h = max(60, label.height() - 8)
        orig = reader.size()
        if orig.isValid() and orig.width() > 0 and orig.height() > 0:
            ratio = min(target_w / orig.width(), target_h / orig.height())
            reader.setScaledSize(QSize(
                max(1, int(orig.width() * ratio)),
                max(1, int(orig.height() * ratio)),
            ))
        img = reader.read()
        if not img.isNull():
            label.setPixmap(QPixmap.fromImage(img))
        else:
            label.setText("?")

    def _render_single_pixmap(self):
        idx = self.page * self.PAGE_SIZE + self.sel
        if not (0 <= idx < len(self.photos)):
            self._single_label.clear()
            self._single_info.hide()
            return
        path = self.photos[idx]
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        w = max(100, self._single_page.width())
        h = max(100, self._single_page.height())
        orig = reader.size()
        if orig.isValid() and orig.width() > 0 and orig.height() > 0:
            ratio = min(w / orig.width(), h / orig.height())
            reader.setScaledSize(QSize(
                max(1, int(orig.width() * ratio)),
                max(1, int(orig.height() * ratio)),
            ))
        img = reader.read()
        if not img.isNull():
            self._single_label.setPixmap(QPixmap.fromImage(img))
        self._single_info.setText(f"{idx + 1} / {len(self.photos)}    {path.name}")
        self._single_info.adjustSize()
        self._single_info.show()
        self._single_info.raise_()


class CameraUI(QMainWindow):
    """
    主窗口。

    Pi 模式：只有顶部 HUD 条 + 底部控制栏，中间透明让 GPU 直渲 preview。
    Dev 模式：中间插入 PreviewWidget 显示 OpenCV 帧。

    遥控器两键状态机：
      NORMAL  → PageDown=拍照, PageUp=进MENU

    蓝牙自拍杆：AB Shutter3 发 KEY_VOLUMEUP，经 evdev 线程捕获后触发拍照。
    """

    _bt_shutter_signal = pyqtSignal()

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

        # ─── 遥控器状态机 ────────────────────────────────────
        self._app_mode = AppMode.CAMERA
        self._ui_mode  = UIMode.NORMAL
        self._menu_idx = 0   # 当前聚焦参数：0=ISO 1=SHUTTER 2=AWB
        # 双击检测：第一次 PageUp 等 300ms 看是否有第二次
        self._pageup_pending = False
        self._single_click_timer = QTimer()
        self._single_click_timer.setSingleShot(True)
        self._single_click_timer.timeout.connect(self._on_pageup_single)

        self._mode_timer = QTimer()
        self._mode_timer.setSingleShot(True)
        self._mode_timer.timeout.connect(self._exit_to_normal)

        self._setup_window()
        self._setup_ui()

        # 应用层事件过滤器：无论哪个子控件有焦点，按键都经过此处
        from PyQt5.QtWidgets import QApplication
        QApplication.instance().installEventFilter(self)

        # 蓝牙自拍杆：绕过 Wayland 音量键拦截，直接读 evdev
        self._bt_shutter_signal.connect(self._on_capture)
        self._start_bt_listener()

    def _find_bt_shutter_device(self):
        """扫描 /dev/input/event* 找 AB Shutter3 Consumer Control 设备"""
        for path in sorted(glob.glob('/dev/input/event*')):
            try:
                name_path = f'/sys/class/input/{os.path.basename(path)}/device/name'
                with open(name_path) as f:
                    name = f.read().strip()
                if 'Shutter' in name and 'Consumer' in name:
                    return path
            except Exception:
                continue
        return None

    def _start_bt_listener(self):
        dev = self._find_bt_shutter_device()
        if not dev:
            print('[BT] AB Shutter3 not found, skipping listener')
            return
        print(f'[BT] Listening on {dev}')
        t = threading.Thread(target=self._bt_listener_loop, args=(dev,), daemon=True)
        t.start()

    def _bt_listener_loop(self, dev_path):
        # struct input_event: timeval(8+8) + type(2) + code(2) + value(4) = 24 bytes
        FMT = 'llHHi'
        SIZE = struct.calcsize(FMT)
        EV_KEY, KEY_VOLUMEUP, KEY_PRESS = 1, 115, 1
        try:
            with open(dev_path, 'rb') as f:
                while True:
                    data = f.read(SIZE)
                    if len(data) < SIZE:
                        break
                    _, _, etype, code, value = struct.unpack(FMT, data)
                    if etype == EV_KEY and code == KEY_VOLUMEUP and value == KEY_PRESS:
                        self._bt_shutter_signal.emit()
        except Exception as e:
            print(f'[BT] Listener error: {e}')

    def eventFilter(self, obj, event):
        from PyQt5.QtCore import QEvent
        if event.type() == QEvent.KeyPress:
            self.keyPressEvent(event)
            return True
        return False

    def _setup_window(self):
        self.setWindowTitle("PiCamera2")
        self.setAttribute(Qt.WA_AcceptTouchEvents, True)
        if not self.dev_mode:
            self.setWindowFlags(Qt.FramelessWindowHint)
        else:
            self.resize(1024, 640)

    def _setup_ui(self):
        central = QWidget()
        central.setObjectName("central")
        central.setStyleSheet(HUD_STYLE)
        self.setCentralWidget(central)

        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 顶层 stacked：[0] camera_page  [1] album_view
        self._page_stack = QStackedWidget()
        main_layout.addWidget(self._page_stack)

        # ─── camera_page ─────────────────────────────────────
        camera_page = QWidget()
        cam_layout = QVBoxLayout(camera_page)
        cam_layout.setContentsMargins(0, 0, 0, 0)
        cam_layout.setSpacing(0)

        self.top_hud = TopHUD()
        cam_layout.addWidget(self.top_hud)

        if self._preview_widget is not None:
            cam_layout.addWidget(self._preview_widget, stretch=1)
        elif self.dev_mode:
            self.cv_preview = PreviewWidget()
            cam_layout.addWidget(self.cv_preview, stretch=1)
        else:
            spacer = QWidget()
            spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            cam_layout.addWidget(spacer, stretch=1)

        self.bottom_controls = BottomControls(engine=self.engine)
        self.bottom_controls.capture_pressed.connect(self._on_capture)
        cam_layout.addWidget(self.bottom_controls)

        # 浮动选项条，定位在 camera_page 上方
        self.option_strip = OptionStrip(parent=camera_page)

        self._page_stack.addWidget(camera_page)

        # ─── album_view ──────────────────────────────────────
        self.album_view = AlbumView(self.output_dir)
        self._page_stack.addWidget(self.album_view)

        self._page_stack.setCurrentIndex(0)

    # ─── 刷新方法（由 QTimer 调用）──────────────────────────

    def refresh_hud(self):
        self.top_hud.update_params(
            state=self.engine.state,
            config=self.engine.config,
        )

    def update_preview_frame(self):
        if not self.dev_mode:
            return
        frame = self.engine.read_frame()
        if frame is not None:
            self.cv_preview.set_frame(frame)

    # ─── 拍摄 ────────────────────────────────────────────────

    def _on_capture(self):
        path = self.on_capture()
        print(f"[UI] Capture triggered → {path}")

    # ─── 键盘快捷键 ──────────────────────────────────────────

    # 键盘调试别名（无 J09 时方便测试）
    _DEBUG_KEY_ALIAS = {
        Qt.Key_Space: Qt.Key_F4,  # 圆圈
        Qt.Key_J:     Qt.Key_F1,  # I
        Qt.Key_K:     Qt.Key_F2,  # O
        Qt.Key_L:     Qt.Key_F3,  # II
    }

    def keyPressEvent(self, event):
        key = self._DEBUG_KEY_ALIAS.get(event.key(), event.key())

        # F4 (双击右滑): CAMERA NORMAL ↔ ALBUM 切换；MENU/ADJUST 中忽略以免误触
        if key == Qt.Key_F4:
            if self._app_mode == AppMode.CAMERA and self._ui_mode != UIMode.NORMAL:
                return
            self._toggle_album()
            return

        if self._app_mode == AppMode.CAMERA:
            self._handle_camera_key(key, event)
        elif self._app_mode == AppMode.ALBUM_GRID:
            self._handle_album_grid_key(key)
        elif self._app_mode == AppMode.ALBUM_SINGLE:
            self._handle_album_single_key(key)
        elif self._app_mode == AppMode.ALBUM_DELETE_CONFIRM:
            self._handle_album_confirm_key(key)

    def _handle_camera_key(self, key, event):
        if key in (Qt.Key_VolumeDown, Qt.Key_F2):
            self._on_capture()
            return

        if self._ui_mode == UIMode.NORMAL:
            if key == Qt.Key_PageDown:
                self._on_capture()
            elif key in (Qt.Key_PageUp, Qt.Key_Back, Qt.Key_F1):
                self._enter_menu()
            elif key in (Qt.Key_Space, Qt.Key_Return):
                self._on_capture()
            elif key in (Qt.Key_Q, Qt.Key_Escape):
                self.on_quit()
            else:
                super().keyPressEvent(event)

        elif self._ui_mode == UIMode.MENU:
            if key == Qt.Key_PageUp:
                self._menu_cycle()
            elif key in (Qt.Key_PageDown, Qt.Key_Back, Qt.Key_F1):
                self._enter_adjust()
            elif key == Qt.Key_Right:
                self._menu_param_next()
            elif key == Qt.Key_Left:
                self._menu_param_prev()
            elif key in (Qt.Key_Sleep, Qt.Key_F3, Qt.Key_Escape):
                self._exit_to_normal()
                return
            else:
                super().keyPressEvent(event)
            self._reset_mode_timer(5000)

        elif self._ui_mode == UIMode.ADJUST:
            if key == Qt.Key_PageUp:
                if not self._pageup_pending:
                    self._adjust_selected(+1)
                    self._reset_mode_timer(8000)
                    self._pageup_pending = True
                    self._single_click_timer.start(300)
                else:
                    self._single_click_timer.stop()
                    self._pageup_pending = False
                    self._adjust_selected(-1)
                    self._exit_to_menu()
            elif key == Qt.Key_PageDown:
                self._adjust_selected(-1)
                self._reset_mode_timer(8000)
            elif key == Qt.Key_Up:
                self._adjust_selected(-1)
                self._reset_mode_timer(8000)
            elif key == Qt.Key_Down:
                self._adjust_selected(+1)
                self._reset_mode_timer(8000)
            elif key in (Qt.Key_Sleep, Qt.Key_F3, Qt.Key_Back, Qt.Key_F1):
                self._exit_to_menu()
            elif key == Qt.Key_Escape:
                self._exit_to_normal()
            else:
                super().keyPressEvent(event)

    def _on_pageup_single(self):
        """300ms 内没有第二次 PageUp → 单击已处理完毕，清除 pending 状态"""
        self._pageup_pending = False

    # ─── Album 状态机 ────────────────────────────────────────

    def _toggle_album(self):
        if self._app_mode == AppMode.CAMERA:
            self._enter_album()
        else:
            self._exit_album()

    def _enter_album(self):
        # 进 album 前先把相机子状态清干净
        self._exit_to_normal()
        self._app_mode = AppMode.ALBUM_GRID
        self.album_view.enter_album()
        self._page_stack.setCurrentIndex(1)

    def _exit_album(self):
        self._app_mode = AppMode.CAMERA
        self._page_stack.setCurrentIndex(0)

    def _handle_album_grid_key(self, key):
        if key in (Qt.Key_F1, Qt.Key_Left, Qt.Key_Up, Qt.Key_PageUp):
            self.album_view.prev()
        elif key in (Qt.Key_F3, Qt.Key_Right, Qt.Key_Down, Qt.Key_PageDown):
            self.album_view.next()
        elif key in (Qt.Key_F2, Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            if self.album_view.select():
                self._app_mode = AppMode.ALBUM_SINGLE

    def _handle_album_single_key(self, key):
        if key in (Qt.Key_F1, Qt.Key_F3, Qt.Key_PageUp, Qt.Key_PageDown):
            self.album_view.request_delete()
            self._app_mode = AppMode.ALBUM_DELETE_CONFIRM
        elif key in (Qt.Key_F2, Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space, Qt.Key_Escape):
            self.album_view.back_to_grid()
            self._app_mode = AppMode.ALBUM_GRID

    def _handle_album_confirm_key(self, key):
        if key in (Qt.Key_F2, Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.album_view.confirm_delete()
            self._app_mode = AppMode.ALBUM_GRID
        elif key in (Qt.Key_F1, Qt.Key_F3, Qt.Key_Escape):
            self.album_view.cancel_delete()
            self._app_mode = AppMode.ALBUM_SINGLE

    # ─── 状态机 ──────────────────────────────────────────────

    def _enter_menu(self):
        self._ui_mode  = UIMode.MENU
        self._menu_idx = 0
        self.bottom_controls.set_focused(0, HUD_SELECT)
        self._reset_mode_timer(5000)

    def _menu_cycle(self):
        self._menu_idx += 1
        if self._menu_idx >= len(_PARAM_TITLES):
            self._exit_to_normal()
        else:
            self.bottom_controls.set_focused(self._menu_idx, HUD_SELECT)

    def _menu_param_next(self):
        self._menu_idx = (self._menu_idx + 1) % len(_PARAM_TITLES)
        self.bottom_controls.set_focused(self._menu_idx, HUD_SELECT)

    def _menu_param_prev(self):
        self._menu_idx = (self._menu_idx - 1) % len(_PARAM_TITLES)
        self.bottom_controls.set_focused(self._menu_idx, HUD_SELECT)

    def _enter_adjust(self):
        self._ui_mode = UIMode.ADJUST
        self.bottom_controls.set_focused(self._menu_idx, HUD_ADJUST)
        idx = self._current_option_idx(self._menu_idx)
        anchor = self.bottom_controls.get_param_group(self._menu_idx)
        self.option_strip.show_options(
            _STRIP_OPTIONS[self._menu_idx],
            idx,
            anchor_widget=anchor,
        )
        self._reset_mode_timer(8000)

    def _exit_to_menu(self):
        self._ui_mode = UIMode.MENU
        self.option_strip.hide()
        self.bottom_controls.set_focused(self._menu_idx, HUD_SELECT)
        self._reset_mode_timer(5000)

    def _exit_to_normal(self):
        self._ui_mode = UIMode.NORMAL
        self._mode_timer.stop()
        self.option_strip.hide()
        self.bottom_controls.set_focused(None)

    def _adjust_selected(self, direction: int):
        if self._menu_idx == 0:
            self._adjust_iso(direction)
        elif self._menu_idx == 1:
            self._adjust_shutter(direction)
        elif self._menu_idx == 2:
            self._adjust_awb(direction)
        self.option_strip.set_current(self._current_option_idx(self._menu_idx))

    def _reset_mode_timer(self, ms: int):
        self._mode_timer.stop()
        self._mode_timer.start(ms)

    # ─── 参数当前选项下标 ────────────────────────────────────

    def _current_option_idx(self, param_idx: int) -> int:
        if param_idx == 0:
            iso_steps = [None, 100, 200, 400, 800, 1600, 3200]
            cur = self.engine.config.iso
            return iso_steps.index(cur) if cur in iso_steps else 0
        elif param_idx == 1:
            shutter_us = [None, 250, 500, 1000, 2000, 4000, 8000, 16000, 33333, 66666, 1_000_000]
            cur = self.engine.config.shutter_speed
            for i, v in enumerate(shutter_us):
                if v == cur:
                    return i
            return 0
        elif param_idx == 2:
            awb_steps = ["auto", "daylight", "cloudy", "tungsten", "fluorescent"]
            cur = self.engine.config.awb_mode
            return awb_steps.index(cur) if cur in awb_steps else 0
        return 0

    # ─── 参数调节 ────────────────────────────────────────────

    def _adjust_iso(self, direction: int):
        iso_steps = [None, 100, 200, 400, 800, 1600, 3200]
        cur = self.engine.config.iso
        idx = iso_steps.index(cur) if cur in iso_steps else 0
        new_idx = max(0, min(len(iso_steps) - 1, idx + direction))
        self.engine.set_iso(iso_steps[new_idx])
        combo = self.bottom_controls.iso_combo
        combo.blockSignals(True)
        combo.setCurrentText("Auto" if iso_steps[new_idx] is None else str(iso_steps[new_idx]))
        combo.blockSignals(False)

    def _adjust_shutter(self, direction: int):
        shutter_us = [None, 250, 500, 1000, 2000, 4000, 8000, 16000, 33333, 66666, 1_000_000]
        shutter_labels = ["Auto", "1/4000", "1/2000", "1/1000",
                          "1/500", "1/250", "1/125", "1/60", "1/30", "1/15", "1s"]
        cur = self.engine.config.shutter_speed
        idx = 0
        for i, v in enumerate(shutter_us):
            if v == cur:
                idx = i
                break
        new_idx = max(0, min(len(shutter_us) - 1, idx + direction))
        self.engine.set_shutter_speed(shutter_us[new_idx])
        combo = self.bottom_controls.shutter_combo
        combo.blockSignals(True)
        combo.setCurrentText(shutter_labels[new_idx])
        combo.blockSignals(False)

    def _adjust_awb(self, direction: int):
        awb_steps = ["auto", "daylight", "cloudy", "tungsten", "fluorescent"]
        cur = self.engine.config.awb_mode
        idx = awb_steps.index(cur) if cur in awb_steps else 0
        new_idx = (idx + direction) % len(awb_steps)
        self.engine.set_awb_mode(awb_steps[new_idx])
        combo = self.bottom_controls.awb_combo
        combo.blockSignals(True)
        combo.setCurrentText(awb_steps[new_idx])
        combo.blockSignals(False)
