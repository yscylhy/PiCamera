# PiCamera2 App 调试记录

## 硬件环境
- Raspberry Pi 4
- IMX477 HQ Camera（CSI 接口）
- 4 寸 HDMI 显示屏

## 目标架构
picamera2 + DRM/KMS overlay（GPU 直通预览）+ Qt HUD overlay

数据路径：
```
IMX477 → libcamera ISP → DRM/KMS plane（GPU 直通，不走 Python）
                       ↗
         picamera2 控制层（Python）— Qt 透明 HUD 浮在上面
```

---

## 问题一：libcamera ImportError

### 报错
```
ImportError: /lib/aarch64-linux-gnu/libcamera.so.0.3: undefined symbol:
_ZN7libpisp22compute_optimal_strideER24pisp_image_format_config
```

### 原因
- `libcamera0.3`（Nov 2024 新版）和 `python3-libcamera`（Sep 2024 旧版）版本不匹配
- 新版 `.so` 依赖 `libpisp`，旧版 Python bindings 没有对应符号
- 根本原因：`dpkg` 被中断，包处于 `iU`（解压未配置）状态

### 诊断命令
```bash
dpkg -l | grep -E "libcamera|libpisp"
ls -la /lib/aarch64-linux-gnu/libcamera*
```

### 修复步骤
```bash
sudo dpkg --configure -a
sudo apt install -f
sudo apt install --reinstall libcamera0.3 python3-libcamera libcamera-ipa
python3 -c "import picamera2; print('OK')"
```

---

## 问题二：DrmPreview 事件循环冲突

### 报错
```
RuntimeError: An event loop is already running
```

### 原因
`app.py` 的 `_run_pi_drm_only` 方法里手动调了 `camera.start_preview(DrmPreview())`，
但 picamera2 内部已经启动了事件循环，两者冲突。

### 修复
不手动调 `start_preview()`，改用 `cam.start(show_preview=True)`：

```python
def _run_pi_drm_only(self):
    self.engine.start()
    cam = self.engine._camera
    cam.start(show_preview=True)  # picamera2 自己管理事件循环

    print("[APP] 按回车拍照，Ctrl+C 退出")
    try:
        while True:
            cmd = input()
            if cmd.strip() == "":
                path = self._make_output_path()
                self.engine.capture_photo(str(path))
                print(f"[APP] Captured → {path.name}")
    except KeyboardInterrupt:
        self.shutdown()
```

---

## 问题三：`_camera` 为 None

### 报错
```
AttributeError: 'NoneType' object has no attribute 'start'
```

### 原因
`_run_pi_drm_only` 里没有先调 `self.engine.start()`，导致 `_camera` 未初始化。
同时 `_start_csi` 里调了 `cam.start()`，后面 `cam.start(show_preview=True)` 又调一次，双重 start 冲突。

### 修复：把 start 控制权交给 app 层

**`camera.py` — `_start_csi` 方法，删掉末尾的 `cam.start()`：**

```python
def _start_csi(self):
    cam = Picamera2()
    preview_config = cam.create_preview_configuration(
        main={"size": self.config.still_size, "format": "RGB888"},
        lores={"size": self.config.preview_size, "format": "YUV420"},
        display="lores",
        controls=self._build_controls(),
    )
    cam.configure(preview_config)
    # 不在这里 start，由 app 层决定 start 方式
    self._camera = cam
```

**`app.py` — `_run_pi_drm_only` 完整修复版：**

```python
def _run_pi_drm_only(self):
    self.engine.start()           # 初始化 + configure，不 start
    cam = self.engine._camera
    cam.start(show_preview=True)  # 带预览地 start，picamera2 自管事件循环

    print("[APP] 按回车拍照，Ctrl+C 退出")
    try:
        while True:
            cmd = input()
            if cmd.strip() == "":
                path = self._make_output_path()
                self.engine.capture_photo(str(path))
                print(f"[APP] Captured → {path.name}")
    except KeyboardInterrupt:
        self.shutdown()
```

---

## 当前状态（待验证）
- [x] picamera2 import 正常
- [x] IMX477 相机识别成功（libcamera v0.5.2 + imx477.json tuning）
- [ ] DRM 预览正常启动
- [ ] 拍摄保存 JPEG + DNG
- [ ] Qt HUD overlay（需要先装 PyQt6）

## 下一步
```bash
# 安装 Qt（HUD overlay 需要）
sudo apt install python3-pyqt6

# 运行
python main.py
```
