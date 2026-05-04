#!/usr/bin/env python3
"""
j09_touchpad.py — J09 蓝牙触摸板转发守护进程

J09 上报绝对坐标 + BTN_TOOL_PEN，libinput 因 tablet 能力不完整而拒绝。
本脚本将绝对坐标转换为相对位移，创建标准虚拟鼠标供系统使用。
"""

import sys
import time
import glob
import evdev
from evdev import UInput, ecodes

SPEED = 0.15   # 调节光标速度：值越小越慢


def find_j09_device():
    for path in sorted(glob.glob('/dev/input/event*')):
        try:
            dev = evdev.InputDevice(path)
            if dev.name == 'J09':
                return dev
        except Exception:
            continue
    return None


def create_virtual_mouse():
    cap = {
        ecodes.EV_KEY: [ecodes.BTN_LEFT, ecodes.BTN_RIGHT],
        ecodes.EV_REL: [ecodes.REL_X, ecodes.REL_Y],
    }
    return UInput(cap, name='J09 Virtual Mouse', version=0x1)


def main():
    print('[J09] Looking for J09 device...')
    src = None
    for _ in range(20):
        src = find_j09_device()
        if src:
            break
        time.sleep(1)

    if not src:
        print('[J09] Device not found, exiting')
        sys.exit(1)

    print(f'[J09] Found: {src.path}')
    src.grab()

    vdev = create_virtual_mouse()
    print(f'[J09] Virtual mouse created: {vdev.device.path}')

    prev_x = None
    prev_y = None
    touching = False
    accum_x = 0.0
    accum_y = 0.0

    try:
        for event in src.read_loop():
            if event.type == ecodes.EV_KEY:
                if event.code == ecodes.BTN_TOUCH:
                    touching = event.value == 1
                    if not touching:
                        prev_x = None
                        prev_y = None
                        accum_x = 0.0
                        accum_y = 0.0
                    # BTN_TOUCH press → BTN_LEFT click
                    if event.value == 1:
                        vdev.write(ecodes.EV_KEY, ecodes.BTN_LEFT, 1)
                        vdev.write(ecodes.EV_SYN, ecodes.SYN_REPORT, 0)
                    else:
                        vdev.write(ecodes.EV_KEY, ecodes.BTN_LEFT, 0)
                        vdev.write(ecodes.EV_SYN, ecodes.SYN_REPORT, 0)

            elif event.type == ecodes.EV_ABS:
                if event.code == ecodes.ABS_X:
                    if prev_x is not None and touching:
                        accum_x += (event.value - prev_x) * SPEED
                    prev_x = event.value
                elif event.code == ecodes.ABS_Y:
                    if prev_y is not None and touching:
                        accum_y += (event.value - prev_y) * SPEED
                    prev_y = event.value

            elif event.type == ecodes.EV_SYN:
                dx = int(accum_x)
                dy = int(accum_y)
                if dx or dy:
                    vdev.write(ecodes.EV_REL, ecodes.REL_X, dx)
                    vdev.write(ecodes.EV_REL, ecodes.REL_Y, dy)
                    vdev.write(ecodes.EV_SYN, ecodes.SYN_REPORT, 0)
                    accum_x -= dx
                    accum_y -= dy

    except KeyboardInterrupt:
        pass
    finally:
        src.ungrab()
        vdev.close()
        print('[J09] Stopped')


if __name__ == '__main__':
    main()
