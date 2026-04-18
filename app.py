"""
app.py — CameraApp
职责：
  - 应用生命周期管理（启动、退出、信号处理）
  - 初始化 picamera2 的 QtPreview（或 DrmPreview）
  - 管理输出目录
  - 连接 camera engine 和 UI 层

关于预览渲染策略：
  - Pi 上推荐：Picamera2(display=DrmPreview) → GPU 直通到 KMS plane，不走 Python
  - 开发机：QtPreview 在普通窗口里渲染（CPU 路径，仅用于调试）
"""

import os
import signal
import sys
import datetime
import threading
from pathlib import Path

from camera import CameraConfig, CameraEngine

IS_PI = sys.platform in ("linux", "linux2")

# Qt 导入（PyQt6 优先，PySide6 备用）
try:
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import Qt, QTimer
    QT_AVAILABLE = True
    QT_BACKEND = "PyQt6"
except ImportError:
    try:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import Qt, QTimer
        QT_AVAILABLE = True
        QT_BACKEND = "PySide6"
    except ImportError:
        QT_AVAILABLE = False
        QT_BACKEND = None

if QT_AVAILABLE:
    from ui import CameraUI


class CameraApp:
    """
    顶层应用类。

    启动流程：
      1. 创建输出目录
      2. 初始化 CameraEngine（配置双流）
      3. 初始化 Qt 应用 + CameraUI（HUD overlay）
      4. 启动 picamera2 preview（GPU 直通）
      5. 进入 Qt 事件循环

    退出流程：
      Ctrl+C / SIGTERM → 调用 shutdown() → 停止 camera → 退出 Qt
    """

    def __init__(
        self,
        interface: str = "csi",
        output_dir: str = "./photos",
        preview_size: tuple = (1920, 1080),
    ):
        self.interface = interface
        self.output_dir = Path(output_dir)
        self.preview_size = preview_size

        # 确保输出目录存在
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 初始化配置
        self.cam_config = CameraConfig(
            preview_size=preview_size,
            still_size=(4056, 3040),  # IMX477 最大分辨率
        )

        # 初始化引擎
        self.engine = CameraEngine(config=self.cam_config, interface=interface)

        # Qt 应用
        self.qt_app = None
        self.ui = None

    def run(self):
        """应用主入口"""
        print(f"[APP] Starting PiCamera2 app")
        print(f"[APP] Interface: {self.interface}")
        print(f"[APP] Output dir: {self.output_dir}")
        print(f"[APP] Preview size: {self.preview_size}")

        # 注册信号处理
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

        if IS_PI and self.interface == "csi":
            self._run_pi()
        else:
            self._run_dev()

    def _run_pi(self):
        """
        Pi 上的正式运行路径。
        picamera2 使用 DrmPreview，GPU 直通到显示器，
        Qt 只负责渲染透明的 HUD overlay。
        """
        from picamera2 import Picamera2
        from picamera2.previews import QtGlPreview

        if not QT_AVAILABLE:
            print("[APP] Qt not available, falling back to DrmPreview (no UI overlay)")
            self._run_pi_drm_only()
            return

        # 必须先创建 QApplication
        self.qt_app = QApplication(sys.argv)

        # 用 QtGlPreview：GPU 渲染到 Qt 窗口，可以在上面叠加 UI
        # 比 DrmPreview 灵活，比 CPU 路径快
        self.engine.start()
        camera = self.engine._camera

        # 启动 picamera2 自带的 Qt GL 预览窗口
        # 这会创建一个 OpenGL 纹理，把 camera stream 贴上去，帧率可达 60fps
        camera.start_preview(QtGlPreview())

        # 创建 HUD overlay 窗口（Qt 透明窗口，浮在预览上面）
        self.ui = CameraUI(
            engine=self.engine,
            output_dir=self.output_dir,
            on_capture=self._trigger_capture,
            on_quit=self.shutdown,
        )
        self.ui.show()

        # 定时刷新 HUD 参数显示
        timer = QTimer()
        timer.timeout.connect(self.ui.refresh_hud)
        timer.start(500)  # 每 500ms 刷新一次参数显示

        sys.exit(self.qt_app.exec())

    
    def _run_pi_drm_only(self):
        from picamera2.previews import DrmPreview

        cam = self.engine._camera
        # 不手动调 start_preview，让 picamera2 用 show_preview=True 自管理事件循环
        cam.start(show_preview=True)

        print("[APP] DRM preview running. 按回车拍照，Ctrl+C 退出")
        try:
            while True:
                cmd = input()
                if cmd.strip() == "":
                    path = self._make_output_path()
                    self.engine.capture_photo(str(path))
                    print(f"[APP] Captured → {path.name}")
        except KeyboardInterrupt:
            self.shutdown()
    

    def _run_dev(self):
        """
        开发机（macOS/Windows/Linux 无 CSI）路径。
        用 OpenCV USB 摄像头 + Qt 窗口预览。
        """
        if not QT_AVAILABLE:
            print("[APP] Qt not available. Install PyQt6: pip install PyQt6")
            sys.exit(1)

        self.qt_app = QApplication(sys.argv)
        self.engine.start()

        self.ui = CameraUI(
            engine=self.engine,
            output_dir=self.output_dir,
            on_capture=self._trigger_capture,
            on_quit=self.shutdown,
            dev_mode=True,  # dev 模式：UI 自己拉 OpenCV 帧渲染预览
        )
        self.ui.show()

        # dev 模式下启动帧刷新定时器
        frame_timer = QTimer()
        frame_timer.timeout.connect(self.ui.update_preview_frame)
        frame_timer.start(33)  # ~30fps

        hud_timer = QTimer()
        hud_timer.timeout.connect(self.ui.refresh_hud)
        hud_timer.start(500)

        sys.exit(self.qt_app.exec())

    # ─── 拍摄 ─────────────────────────────────────────────────

    def _trigger_capture(self, save_raw: bool = True):
        """UI 层回调，触发一次拍摄"""
        path = self._make_output_path()
        self.engine.capture_photo(str(path), save_raw=save_raw)
        return path

    def _make_output_path(self) -> Path:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        return self.output_dir / f"IMG_{ts}.jpg"

    # ─── 退出 ─────────────────────────────────────────────────

    def shutdown(self):
        print("[APP] Shutting down...")
        self.engine.stop()
        if self.qt_app:
            self.qt_app.quit()

    def _on_signal(self, signum, frame):
        print(f"\n[APP] Signal {signum} received")
        self.shutdown()
