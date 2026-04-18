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
| ↑ / ↓ | 调整 ISO |
| ← / → | 调整快门速度 |
| Q / Esc | 退出 |

## 开机自启（systemd）

```ini
# /etc/systemd/system/picamera.service
[Unit]
Description=PiCamera2 App
After=graphical.target

[Service]
User=pi
WorkingDirectory=/home/pi/picamera2_app
ExecStart=/usr/bin/python3 /home/pi/picamera2_app/main.py
Restart=always
RestartSec=3
Environment=DISPLAY=:0

[Install]
WantedBy=graphical.target
```

```bash
sudo systemctl enable picamera
sudo systemctl start picamera
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
