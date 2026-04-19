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

## Key Behavioral Details

- On Pi, preview frames never pass through Python — libcamera ISP sends them directly to a DRM KMS plane. `read_frame()` on CSI calls `capture_array("main")` which is a separate software capture, not the live preview.
- Capture is always async (background thread + lock). `state.is_capturing` guards against double-trigger.
- `IS_PI` is `sys.platform in ("linux", "linux2")` — all Pi-specific code paths gate on this.
- Still size defaults to 4056×3040 (IMX477 HQ Camera max resolution).

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| Space / Enter | Capture photo |
| ↑ / ↓ | Adjust ISO |
| ← / → | Adjust shutter speed |
| Q / Esc | Quit |
