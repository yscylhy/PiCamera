# Qt 窗口不显示 — 调试记录

## 问题描述
从树莓派桌面终端运行 `python main.py`，app 打印前4行后无窗口出现：
```
[APP] Starting PiCamera2 app
[APP] Interface: csi
[APP] Output dir: photos
[APP] Preview size: (1920, 1080)
← 这里停止，无任何窗口
```

## 已知信息

### 能正常工作的情况
- 从 **SSH session** 运行时，窗口正常出现在树莓派屏幕上
- SSH 场景：`WAYLAND_DISPLAY` 未继承 → 代码自动探测 → 设 `QT_WAYLAND_CLIENT_BUFFER_INTEGRATION=shm` → Qt 用 wayland-egl → 窗口出现

### 不能工作的情况
- 从**树莓派桌面终端**直接运行时，无窗口，app 似乎挂起

### 关键架构
- 桌面：labwc (Wayland compositor)，无 X11/XWayland
- Qt：PyQt5（必须用 PyQt5，因为 picamera2 依赖它）
- 预览：QPicamera2 widget 嵌入 Qt 窗口
- Wayland socket：`/run/user/1000/wayland-0`

### 代码逻辑（`app.py` `_run_pi()`）
```python
ssh_session = self._setup_display_env()  # 自动探测 WAYLAND_DISPLAY
# ssh_session=True  → 未继承 WAYLAND_DISPLAY，自动探测到
# ssh_session=False → 环境里已有 WAYLAND_DISPLAY（桌面 session）

if WAYLAND_DISPLAY and QT_QPA_PLATFORM not set:
    os.environ["QT_QPA_PLATFORM"] = "wayland"

if ssh_session:  # 只在 SSH 场景设此 workaround
    os.environ.setdefault("QT_WAYLAND_CLIENT_BUFFER_INTEGRATION", "shm")

QApplication(sys.argv)  # ← 桌面场景可能在此挂起或崩溃（无输出）
```

## 待调查

### 第一步：确认环境变量
在树莓派桌面终端运行：
```bash
env | grep -E "WAYLAND|DISPLAY|QT_QPA|XDG|DBUS"
```
重点看：
- `WAYLAND_DISPLAY` 是否已设置
- `QT_QPA_PLATFORM` 是否已设置（可能已被桌面 session 设成别的值）
- `DBUS_SESSION_BUS_ADDRESS` 是否存在

### 第二步：测试 Qt 基本能否工作
```bash
python3 -c "
import os
os.environ['QT_QPA_PLATFORM'] = 'wayland'
from PyQt5.QtWidgets import QApplication, QLabel
app = QApplication([])
w = QLabel('hello')
w.show()
app.exec_()
"
```
如果这个出窗口，说明 Qt Wayland 基本没问题，问题在我们的代码。
如果不出，说明 Qt Wayland 本身有问题。

### 第三步：如果第二步正常，测试 QPicamera2
```bash
python3 -c "
import os
os.environ['QT_QPA_PLATFORM'] = 'wayland'
from PyQt5.QtWidgets import QApplication
from picamera2 import Picamera2
from picamera2.previews.qt import QPicamera2

app = QApplication([])
cam = Picamera2()
cam.configure(cam.create_preview_configuration())
w = QPicamera2(cam, keep_ar=True)
w.show()
cam.start()
app.exec_()
"
```

### 第四步：如果以上都正常，问题在 `CameraUI`
在 `app.py` 的 `_run_pi()` 里，在 `QApplication` 之后加一行：
```python
self.qt_app = QApplication(sys.argv)
print("[DEBUG] QApplication created OK")  # ← 看这行有没有出现
```
如果没出现，说明 QApplication 本身崩溃了。

## 可能的根本原因
1. 桌面 session 里 `QT_QPA_PLATFORM` 已经被设成了别的值（如 `xcb`），我们的代码因为检测到已设置而不覆盖它
2. Qt Wayland 在桌面 session 里需要不同的初始化
3. `showFullScreen()` 在 labwc 上有问题，窗口创建了但不可见

## 快速验证命令
```bash
# 强制指定所有关键环境变量再跑
QT_QPA_PLATFORM=wayland \
XDG_RUNTIME_DIR=/run/user/1000 \
python main.py 2>&1
```
