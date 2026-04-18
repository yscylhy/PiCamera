"""
camera.py — CameraEngine
职责：
  - 封装 picamera2（CSI）和 OpenCV（USB）两种接口
  - 管理双流配置：preview stream（GPU 直通）+ still stream（高分辨率拍摄）
  - 暴露手动控制接口：ISO、快门、白平衡、对焦
  - 线程安全的拍摄触发
"""

import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple

# 根据平台条件导入
IS_PI = sys.platform in ("linux", "linux2")

if IS_PI:
    from picamera2 import Picamera2
    from picamera2.controls import Controls
    import libcamera
else:
    # 开发环境 mock，便于在 macOS/Windows 上调试 UI
    Picamera2 = None


@dataclass
class CameraConfig:
    """相机配置，全部参数集中在这里，便于序列化持久化"""
    # 预览流分辨率（会被 GPU 直通到显示器，不走 Python）
    preview_size: Tuple[int, int] = (1920, 1080)
    # 拍摄流分辨率（IMX477 最大 4056x3040）
    still_size: Tuple[int, int] = (4056, 3040)
    # 手动控制（None 表示 Auto）
    iso: Optional[int] = None          # 100~3200，None=Auto
    shutter_speed: Optional[int] = None  # 微秒，None=Auto
    awb_mode: str = "auto"             # auto / daylight / cloudy / tungsten / fluorescent
    # 对焦（IMX477 HQ Camera 是固定焦距，无自动对焦，此项保留给后续镜头）
    focus_mode: str = "fixed"


@dataclass
class CameraState:
    """运行时相机状态，只读，由 CameraEngine 内部维护"""
    is_running: bool = False
    is_capturing: bool = False
    # 来自 camera metadata 的实时读数
    actual_exposure_us: int = 0
    actual_iso: int = 0
    actual_awb_gains: Tuple[float, float] = (1.0, 1.0)
    fps: float = 0.0
    last_capture_path: Optional[str] = None


class CameraEngine:
    """
    相机核心引擎。

    预览架构（CSI）：
        IMX477 → libcamera ISP → DRM/KMS overlay（GPU 直通，不走 Python）
        picamera2 通过 QtPreview 或 DrmPreview 把 preview stream 直接渲染到
        显示器的一个 DRM plane，帧率可达 30~60fps，CPU 占用极低。

    拍摄架构：
        拍摄时 picamera2 切换到 still stream，完整分辨率捕获，
        同时可以拿到 raw bayer 数据（DNG）。

    USB 降级：
        在没有 CSI camera 的环境（开发机）用 OpenCV 模拟，
        预览走 numpy→Qt，帧率约 30fps。
    """

    def __init__(self, config: CameraConfig, interface: str = "csi"):
        self.config = config
        self.interface = interface
        self.state = CameraState()
        self._lock = threading.Lock()
        self._camera = None
        self._capture_event = threading.Event()
        self._stop_event = threading.Event()
        self._metadata_thread: Optional[threading.Thread] = None

    # ─── 生命周期 ──────────────────────────────────────────────

    def start(self):
        """初始化相机，配置双流，启动预览"""
        if self.interface == "csi" and IS_PI:
            self._start_csi()
        else:
            self._start_usb()
        self.state.is_running = True
        # 启动 metadata 轮询线程（读取实际 ISO/曝光/AWB）
        self._metadata_thread = threading.Thread(
            target=self._metadata_loop, daemon=True
        )
        self._metadata_thread.start()

    def stop(self):
        """释放相机资源"""
        self._stop_event.set()
        if self._camera is not None:
            if self.interface == "csi" and IS_PI:
                self._camera.stop()
                self._camera.close()
            else:
                self._camera.release()
        self.state.is_running = False

    # ─── CSI（picamera2）路径 ─────────────────────────────────

    def _start_csi(self):
        """
        配置 picamera2 双流：
          - "preview": 送给 DRM/KMS overlay 直接显示，YUV420 格式
          - "main": 高分辨率 still 拍摄，RGB888 格式
        """
        cam = Picamera2()

        # 双流配置
        # preview 流分辨率对应屏幕分辨率（4寸屏通常 800x480 或 1024x600）
        # 如果 4K 显示器则设 3840x2160
        preview_config = cam.create_preview_configuration(
            main={
                "size": self.config.still_size,
                "format": "RGB888",
            },
            lores={
                "size": self.config.preview_size,
                "format": "YUV420",
            },
            display="lores",  # 让 DRM/KMS 显示 lores 流，不走 Python
            controls=self._build_controls(),
        )
        cam.configure(preview_config)
        cam.start()
        self._camera = cam

    def _build_controls(self) -> dict:
        """根据 CameraConfig 构建 libcamera controls 字典"""
        controls = {}
        cfg = self.config

        if cfg.iso is not None:
            # libcamera 用 AnalogueGain 而不是 ISO
            # ISO ≈ AnalogueGain × 100（IMX477 基础 ISO 100）
            controls["AnalogueGain"] = cfg.iso / 100.0

        if cfg.shutter_speed is not None:
            controls["ExposureTime"] = cfg.shutter_speed
            controls["AeEnable"] = False  # 关闭自动曝光
        else:
            controls["AeEnable"] = True

        awb_map = {
            "auto": libcamera.controls.AwbModeEnum.Auto,
            "daylight": libcamera.controls.AwbModeEnum.Daylight,
            "cloudy": libcamera.controls.AwbModeEnum.Cloudy,
            "tungsten": libcamera.controls.AwbModeEnum.Tungsten,
            "fluorescent": libcamera.controls.AwbModeEnum.Fluorescent,
        }
        if cfg.awb_mode in awb_map:
            controls["AwbMode"] = awb_map[cfg.awb_mode]

        return controls

    # ─── USB（OpenCV）降级路径 ────────────────────────────────

    def _start_usb(self):
        import cv2
        cap = cv2.VideoCapture(0)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.preview_size[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.preview_size[1])
        cap.set(cv2.CAP_PROP_FPS, 30)
        self._camera = cap

    # ─── 帧读取（UI 层调用，仅 USB 路径需要）─────────────────

    def read_frame(self):
        """
        读取当前帧（RGB numpy array）。
        注意：CSI 路径的预览帧由 GPU 直接渲染，不需要调用此方法。
        此方法主要用于 USB 降级路径，或者需要做图像处理时。
        """
        if not self.state.is_running:
            return None

        if self.interface == "csi" and IS_PI:
            # 从 main 流抓一帧（用于显示处理后的 overlay 或做分析）
            # 正常预览不走这里
            return self._camera.capture_array("main")
        else:
            ret, frame = self._camera.read()
            if ret:
                import cv2
                return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            return None

    # ─── 拍摄 ─────────────────────────────────────────────────

    def capture_photo(self, output_path: str, save_raw: bool = True):
        """
        触发拍摄。在后台线程执行，不阻塞 UI。
        save_raw=True 时同时保存 DNG（仅 CSI 路径）。
        """
        if self.state.is_capturing:
            return  # 防止重复触发

        thread = threading.Thread(
            target=self._do_capture,
            args=(output_path, save_raw),
            daemon=True,
        )
        thread.start()

    def _do_capture(self, output_path: str, save_raw: bool):
        with self._lock:
            self.state.is_capturing = True
            try:
                if self.interface == "csi" and IS_PI:
                    self._capture_csi(output_path, save_raw)
                else:
                    self._capture_usb(output_path)
                self.state.last_capture_path = output_path
                print(f"[CAPTURE] Saved: {output_path}")
            except Exception as e:
                print(f"[CAPTURE ERROR] {e}")
            finally:
                self.state.is_capturing = False

    def _capture_csi(self, output_path: str, save_raw: bool):
        """
        picamera2 高质量拍摄：
        - 先切换到 still 配置（最大分辨率）
        - 捕获 JPEG + 可选 DNG
        - 恢复 preview 配置
        """
        cam = self._camera

        # 切换到 still 配置
        still_config = cam.create_still_configuration(
            main={"size": self.config.still_size, "format": "RGB888"},
            controls=self._build_controls(),
        )
        cam.switch_mode(still_config)

        # 捕获
        if save_raw:
            # capture_file 自动根据扩展名决定格式
            # .dng 触发 RAW bayer 保存
            raw_path = output_path.replace(".jpg", ".dng")
            cam.capture_file(raw_path)

        cam.capture_file(output_path)

        # 恢复预览配置
        preview_config = cam.create_preview_configuration(
            main={"size": self.config.still_size, "format": "RGB888"},
            lores={"size": self.config.preview_size, "format": "YUV420"},
            display="lores",
            controls=self._build_controls(),
        )
        cam.switch_mode(preview_config)

    def _capture_usb(self, output_path: str):
        import cv2
        frame = self.read_frame()
        if frame is not None:
            cv2.imwrite(output_path, cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))

    # ─── 手动控制 ─────────────────────────────────────────────

    def set_iso(self, iso: Optional[int]):
        """iso=None 恢复 Auto"""
        self.config.iso = iso
        self._apply_controls()

    def set_shutter_speed(self, speed_us: Optional[int]):
        """speed_us=None 恢复 Auto（AE）"""
        self.config.shutter_speed = speed_us
        self._apply_controls()

    def set_awb_mode(self, mode: str):
        self.config.awb_mode = mode
        self._apply_controls()

    def _apply_controls(self):
        """运行时热更新 camera controls（不重启）"""
        if self.interface == "csi" and IS_PI and self._camera:
            controls = self._build_controls()
            self._camera.set_controls(controls)

    # ─── Metadata 轮询 ────────────────────────────────────────

    def _metadata_loop(self):
        """
        每秒从 picamera2 读取一次 metadata，更新 CameraState。
        metadata 包含实际的 ExposureTime、AnalogueGain、ColourGains 等。
        """
        _fps_frames = 0
        _fps_ts = time.time()

        while not self._stop_event.is_set():
            try:
                if self.interface == "csi" and IS_PI and self._camera:
                    meta = self._camera.capture_metadata()
                    self.state.actual_exposure_us = meta.get("ExposureTime", 0)
                    gain = meta.get("AnalogueGain", 1.0)
                    self.state.actual_iso = int(gain * 100)
                    gains = meta.get("ColourGains", (1.0, 1.0))
                    self.state.actual_awb_gains = gains

                    # 估算 fps
                    _fps_frames += 1
                    now = time.time()
                    elapsed = now - _fps_ts
                    if elapsed >= 1.0:
                        self.state.fps = _fps_frames / elapsed
                        _fps_frames = 0
                        _fps_ts = now

            except Exception:
                pass

            time.sleep(0.1)
