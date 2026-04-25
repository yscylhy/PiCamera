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

# picamera2 依赖 PyQt5，必须用 PyQt5 才能嵌入 QPicamera2 widget
try:
    from PyQt5.QtWidgets import QApplication
    from PyQt5.QtCore import Qt, QTimer
    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False

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
        用 QPicamera2 widget 把预览嵌入 Qt 窗口，HUD 上下排布。
        """
        if not QT_AVAILABLE:
            print("[APP] Qt not available, falling back to DrmPreview (no UI overlay)")
            self._run_pi_drm_only()
            return

        ssh_session = self._setup_display_env()
        has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        if not has_display:
            print("[APP] No display detected, falling back to DrmPreview (no UI overlay)")
            self._run_pi_drm_only()
            return

        # picamera2 sets QT_QPA_PLATFORM='xcb' on import; override unconditionally
        # to wayland whenever a Wayland socket is available.
        if os.environ.get("WAYLAND_DISPLAY"):
            os.environ["QT_QPA_PLATFORM"] = "wayland"

        # qt5ct platform theme hangs QApplication on Pi's labwc Wayland session.
        # Our app uses its own QStyleSheet so we don't need the system theme.
        os.environ.pop("QT_QPA_PLATFORMTHEME", None)

        self.qt_app = QApplication(sys.argv)
        self.qt_app.setAttribute(Qt.AA_SynthesizeMouseForUnhandledTouchEvents, True)

        from picamera2.previews.qt import QPicamera2

        self.engine.start()
        camera = self.engine._camera

        preview_widget = QPicamera2(camera, keep_ar=True)

        self.ui = CameraUI(
            engine=self.engine,
            output_dir=self.output_dir,
            on_capture=self._trigger_capture,
            on_quit=self.shutdown,
            preview_widget=preview_widget,
        )
        self.ui.showFullScreen()

        camera.start()

        timer = QTimer()
        timer.timeout.connect(self.ui.refresh_hud)
        timer.start(500)

        sys.exit(self.qt_app.exec())

    
    def _setup_display_env(self):
        """SSH 等环境下 WAYLAND_DISPLAY 可能没有继承，尝试自动探测。
        返回 True 表示是自动探测到的（SSH 场景），False 表示环境里已有。"""
        uid = os.getuid()
        xdg = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
        if not os.environ.get("XDG_RUNTIME_DIR"):
            os.environ["XDG_RUNTIME_DIR"] = xdg

        if not os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
            for name in ["wayland-0", "wayland-1"]:
                if os.path.exists(os.path.join(xdg, name)):
                    os.environ["WAYLAND_DISPLAY"] = name
                    print(f"[APP] Auto-detected Wayland display: {name}")
                    return True  # 自动探测到
        return False  # 环境里已有

    def _run_pi_drm_only(self):
        self.engine.start()
        cam = self.engine._camera
        # show_preview=True 让 picamera2 自己管事件循环，不需要手动 start_preview
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
        self.qt_app.setAttribute(Qt.AA_SynthesizeMouseForUnhandledTouchEvents, True)
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
