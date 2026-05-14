#!/usr/bin/env python3
"""
PiCamera2 App — Entry point
用法：python main.py [--interface usb|csi] [--output ./photos]
"""

import argparse
import sys
from app import CameraApp


def parse_args():
    parser = argparse.ArgumentParser(description="PiCamera2 mirrorless camera app")
    parser.add_argument(
        "--interface",
        choices=["csi", "usb"],
        default="csi",
        help="Camera interface: csi (HQ Camera) or usb (webcam)",
    )
    parser.add_argument(
        "--output",
        default="/home/pi/PiCamera/photos",
        help="Output directory for captured photos",
    )
    parser.add_argument(
        "--preview-size",
        default="640x480",
        help="Preview resolution WxH (default: 640x480, 4:3 matches sensor)",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # 解析预览分辨率
    try:
        w, h = map(int, args.preview_size.split("x"))
        preview_size = (w, h)
    except ValueError:
        print(f"[ERROR] Invalid preview size: {args.preview_size}")
        sys.exit(1)

    app = CameraApp(
        interface=args.interface,
        output_dir=args.output,
        preview_size=preview_size,
    )
    app.run()


if __name__ == "__main__":
    main()
