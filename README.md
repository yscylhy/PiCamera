# PiCamera2 App

Pi 4 + IMX477 HQ Camera 的微单相机应用，基于 picamera2 + Qt 构建。

## 架构要点

- **预览**：picamera2 QtGlPreview，GPU 直接渲染，帧率 30~60fps，CPU 占用极低
- **拍摄**：切换到 still 流，最大 4056×3040，同时保存 JPEG + DNG（RAW）
- **UI**：Qt 透明 HUD overlay，浮在预览上方，类微单取景器风格
- **控制**：ISO、快门速度、白平衡，支持键盘快捷键

## 安装

### 1. 系统依赖（Pi 上）

```bash
sudo apt update
sudo apt install -y python3-picamera2 python3-libcamera python3-pyqt6
```

### 2. Python 依赖

```bash
pip install -r requirements.txt
```

## 运行

```bash
# 正常运行（CSI HQ Camera）
python main.py

# 指定输出目录
python main.py --output /home/pi/Pictures

# 调试模式（USB 摄像头，开发机）
python main.py --interface usb

# 指定预览分辨率（适配 4 寸屏 800x480）
python main.py --preview-size 800x480
```

## 键盘快捷键

| 按键 | 功能 |
|------|------|
| 空格 / 回车 | 拍照 |
| Q / Esc | 退出 |

## J09 蓝牙触摸板控制

J09 设备有一个小触摸板和三个物理按键（I / O / II），通过 `j09_touchpad.py`
守护进程接入系统。该脚本会：

- grab `event9`（触摸板），手指滑动按位移识别为方向键，tap 转为鼠标左键单击
- grab `event8`（按键），把 KEY_BACK/KEY_VOLUMEDOWN/KEY_SLEEP 映射为 F1/F2/F3（绕过 Wayland 媒体键拦截）
- `--app-mode` 模式下关闭鼠标光标移动，仅保留手势 → 方向键 + tap

启动：开机由 systemd 服务 `j09-touchpad.service` 自动拉起（见下方
「开机自启」），主 app 由 labwc autostart 启动。临时调试：

```bash
sudo systemctl stop j09-touchpad        # 停服务
sudo python j09_touchpad.py --app-mode  # 前台跑，看输出
sudo systemctl start j09-touchpad       # 调完恢复
```

### 三模式状态机

应用有 NORMAL / MENU / ADJUST 三个模式，由 `ui.py` `CameraUI.keyPressEvent` 管理。

| 操作 | NORMAL | MENU（白边框） | ADJUST（青边框 + 浮动竖向选项条） |
|------|--------|---------------|-----------------------------------|
| **O**（F2） | 拍照 | 拍照 | 拍照 |
| **I**（F1） | → MENU | → ADJUST | → MENU |
| **II**（F3） | — | → NORMAL | → MENU |
| **左/右滑** | — | 切换参数 | — |
| **上/下滑** | — | — | 当前参数 ±1 步 |
| **tap** | 鼠标左键单击（如未在 `--app-mode`） |  |  |

工作流：I 进 MENU → 左右滑切到目标参数 → I 进 ADJUST → 上下滑调值 → II/I 回 MENU → 继续切换或 II 回 NORMAL。

### 视觉反馈

- **MENU**：底部参数组白色边框
- **ADJUST**：边框变青色（`#00FFCC`），同时在当前参数**正上方**浮出竖向选项条，当前值金色加粗高亮。条宽根据最长选项动态计算，长字符（如 `FLUORESCENT`）不会截断
- 退出 ADJUST/MENU 时选项条隐藏

### 触摸板手势细节

`j09_touchpad.py` 中：

- `SPEED = 0.1`：鼠标模式下光标速度
- `TAP_MAX_MS = 150` + `TAP_MAX_DIST = 10`：tap 判定阈值
- `SWIPE_MIN_DIST = 80`：滑动手势最小位移（触摸板范围 0-4095，约 2%）
- 判定时机：**手指抬起瞬间**，根据起点-终点 X/Y 位移决定方向；位移绝对值大的轴胜出
- 支持单轴滑动（只 X 或只 Y 动也能识别）

### 清理残留进程

`j09_touchpad.py` 被 Ctrl+Z 挂起时设备会被独占，常见症状是再次启动报
`grab failed: [Errno 16] Device or resource busy`：

```bash
sudo fuser -k /dev/input/event8 /dev/input/event9
sudo pkill -9 -f j09_touchpad.py
```

或加 alias：

```bash
alias kilj09='sudo fuser -k /dev/input/event8 /dev/input/event9 2>/dev/null; sudo pkill -9 -f j09_touchpad.py 2>/dev/null; true'
```

## 开机自启

两件事分开管：

1. **J09 守护进程（systemd，root）**：要 root 才能 grab evdev 和创建
   uinput 虚拟设备，所以走 systemd 而不是 labwc autostart。
2. **主 app（labwc autostart，用户态）**：要继承 Wayland 环境变量，
   只能在 compositor 起来后跑。

### J09 守护进程：systemd service

```ini
# /etc/systemd/system/j09-touchpad.service
[Unit]
Description=J09 Bluetooth Touchpad Daemon
After=bluetooth.target
Wants=bluetooth.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /home/pi/PiCamera/j09_touchpad.py --app-mode
Restart=always
RestartSec=2
StandardOutput=append:/home/pi/PiCamera/j09.log
StandardError=append:/home/pi/PiCamera/j09.log

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now j09-touchpad
```

常用命令：

```bash
sudo systemctl status j09-touchpad     # 状态
sudo systemctl restart j09-touchpad    # 重启
sudo journalctl -u j09-touchpad -f     # 跟踪日志
```

### 主 app：labwc autostart

`~/.config/labwc/autostart`：

```bash
wlr-randr --output HDMI-A-1 --transform 90
sleep 1 && cd /home/pi/PiCamera && python main.py >> /home/pi/PiCamera/app.log 2>&1 &
```

## 文件结构

```
picamera2_app/
├── main.py       # 入口，参数解析
├── app.py        # 应用层，生命周期管理
├── camera.py     # 相机引擎，picamera2 封装
├── ui.py         # Qt HUD overlay
└── requirements.txt
```

## 下一步计划

- [ ] 直方图显示
- [ ] 对焦辅助（峰值对焦 focus peaking）
- [ ] 曝光补偿 ±3EV
- [ ] 拍摄后缩略图预览（2 秒）
- [ ] 视频录制模式
