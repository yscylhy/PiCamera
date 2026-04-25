# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the App

```bash
# Normal run (CSI HQ Camera, Raspberry Pi)
python main.py

# With custom output directory
python main.py --output /home/pi/Pictures

# USB webcam fallback (dev machine)
python main.py --interface usb

# Custom preview resolution (e.g. 4-inch screen)
python main.py --preview-size 800x480
```

## System Dependencies (Pi only — not pip-installable)

```bash
sudo apt install -y python3-picamera2 python3-libcamera python3-pyqt5
pip install -r requirements.txt
```

Note: The code imports `PyQt5` (not PyQt6). `requirements.txt` lists PyQt6 but that's outdated — the actual runtime uses PyQt5.

## Architecture

The app has three layers that map to three files:

**`main.py`** — CLI entry point, argument parsing only.

**`app.py` (`CameraApp`)** — Lifecycle manager. Decides which run path to take:
- `_run_pi()`: Pi with display → `QPicamera2` widget embedded in Qt window + HUD overlay
- `_run_pi_drm_only()`: Pi without display → DRM/KMS preview, stdin-driven capture
- `_run_dev()`: Non-Pi or USB → OpenCV frames pulled into a Qt `QLabel` at ~30fps

The `_setup_display_env()` method auto-detects Wayland sockets under `/run/user/<uid>/` for SSH sessions where `WAYLAND_DISPLAY` isn't inherited. It also sets `QT_QPA_PLATFORM=wayland` and the `QT_WAYLAND_CLIENT_BUFFER_INTEGRATION=shm` workaround for SSH (but not desktop sessions, where forcing shm causes crashes).

**`camera.py` (`CameraEngine`)** — Wraps picamera2 (CSI) or OpenCV (USB).
- CSI dual-stream config: `lores` stream (YUV420) → GPU display plane; `main` stream (RGB888) → high-res still capture
- Capture switches mode to `still_configuration` (with raw stream for DNG), saves JPEG + DNG via `capture_request()`, then switches back to preview config
- `_metadata_loop()` runs as a daemon thread polling `capture_metadata()` every 100ms to populate `CameraState` (actual ISO, exposure, AWB gains, FPS)
- Controls (`set_iso`, `set_shutter_speed`, `set_awb_mode`) hot-apply via `set_controls()` without restarting the camera

**`ui.py` (`CameraUI`)** — PyQt5 HUD overlay.
- `TopHUD`: displays ISO / shutter / FPS / AWB; gold color = manual mode, white = auto
- `BottomControls`: combo boxes for ISO/shutter/AWB, shutter button (56×56 red circle)
- `PreviewWidget`: dev mode only — converts numpy RGB array to `QPixmap`
- Keyboard: Space/Enter = capture, Q/Esc = quit, ↑↓ = ISO, ←→ = shutter speed

## 预览延迟 / 果冻感排查

**根本原因**：preview 配置的 `main` 流分辨率决定传感器模式，进而决定帧率上限。

| 配置 | 传感器模式 | 帧率 |
|------|-----------|------|
| main = 4056×3040（原始错误配置） | 全分辨率 | ~10fps |
| main = 2028×1520 + 无 FrameRate | 半分辨率 | ~16fps |
| main = 2028×1520 + FrameRate=30 | 半分辨率 | 30fps ✓ |

**结论**：
- preview 时 `main` 流只需设为 `2028x1520`（半分辨率），display 用 `lores`，视觉质量无损
- 必须在 controls 里显式指定 `FrameRate: 30`，否则 picamera2 默认会限速
- 拍摄时切换到 `still_configuration` 仍使用 `4056x3040` 全分辨率，画质不受影响

## Key Behavioral Details

- On Pi, preview frames never pass through Python — libcamera ISP sends them directly to a DRM KMS plane. `read_frame()` on CSI calls `capture_array("main")` which is a separate software capture, not the live preview.
- Capture is always async (background thread + lock). `state.is_capturing` guards against double-trigger.
- `IS_PI` is `sys.platform in ("linux", "linux2")` — all Pi-specific code paths gate on this.
- Still size defaults to 4056×3040 (IMX477 HQ Camera max resolution).

## tmux / Wayland Workflow

标准工作流：在树莓派本地启动 tmux 并运行 Claude Code，Mac 通过 SSH attach 同一个 session 进行远程操作：

```bash
# 树莓派本地（或第一次连接时）
tmux new -s dev
claude

# Mac 上 SSH 连接后 attach
ssh pi@10.0.0.197
tmux attach -t dev
```

这样 Claude Code 和所有子命令都在树莓派本地运行，Wayland 窗口可以正常显示在树莓派屏幕上。

tmux 不继承桌面的 Wayland 环境变量，所有需要显示窗口的命令都必须手动加以下前缀：

```bash
WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/1000
```

### rpicam-hello 预览摄像头

```bash
WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/1000 QT_QPA_PLATFORM=wayland rpicam-hello --qt-preview -t 0
```

- `--qt-preview`：强制使用 Qt 窗口（默认的 EGL/DRM 后端在此环境下不可用）
- `-t 0`：不超时，一直运行直到 Ctrl+C
- 必须在 tmux pane 里直接输入运行，不能通过 Claude Code 的 `!` 前缀执行（`!` 的子 shell 与 Wayland socket 连接不稳定）

### 永久解决（可选）

在 `~/.bashrc` 末尾添加：

```bash
export WAYLAND_DISPLAY=wayland-0
export XDG_RUNTIME_DIR=/run/user/1000
```

这样 tmux 新开的 pane 会自动继承，无需每次手动加前缀。

### 屏幕旋转（横竖屏切换）

屏幕物理分辨率为 480×800（竖屏），通过 Compositor 层旋转实现横屏显示。
GPU 直通预览会跟着整体旋转，无需修改代码。

查看当前输出和旋转状态：
```bash
WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/1000 wlr-randr
```

输出名为 `HDMI-A-1`，Transform 含义：

| Transform 值 | 效果 |
|-------------|------|
| 0 | 原始竖屏（480×800） |
| 90 | 逆时针旋转（横屏 800×480）✓ 当前设置 |
| 180 | 倒置竖屏 |
| 270 | 顺时针旋转（横屏） |

切换横屏（800×480）：
```bash
WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/1000 wlr-randr --output HDMI-A-1 --transform 90
```

注意：IMX477 硬件不支持需要转置的旋转（90°/270°），`rpicam-hello --rotation 90` 会报错
`transforms requiring transpose not supported`，必须在 Compositor 层处理。

### 摄像头被占用排查

如果出现 `failed to acquire camera`，查找占用进程：

```bash
fuser /dev/video*
ps aux | grep -E "python|picamera|rpicam"
```

然后 `kill <PID>` 释放，或一步到位：

```bash
fuser -k /dev/video*
```

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| Space / Enter | Capture photo |
| ↑ / ↓ | Adjust ISO |
| ← / → | Adjust shutter speed |
| Q / Esc | Quit |
