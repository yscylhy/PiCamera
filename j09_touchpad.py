#!/usr/bin/env python3
"""
j09_touchpad.py — J09 蓝牙触摸板转发守护进程

设备节点：
  event9  J09                   触摸板（ABS_X/Y + BTN_TOUCH）
  event8  J09 Consumer Control  三个物理按键

按键映射（转成 F-key 避免 Wayland 拦截媒体键）：
  I  (KEY_BACK=158)       → KEY_F1
  O  (KEY_VOLUMEDOWN=114) → KEY_F2
  II (KEY_SLEEP=142)      → KEY_F3

滑动手势（位移 >= SWIPE_MIN_DIST）→ 方向键
鼠标移动在 --app-mode 下禁用
"""

import sys
import time
import glob
import select
import evdev
from evdev import UInput, ecodes

SPEED = 0.1
TAP_MAX_MS = 150
TAP_MAX_DIST = 10
SWIPE_MIN_DIST = 80
# 当两轴位移接近时偏向水平判定（手指自然滑动常带 Y 抖动）
H_BIAS = 0.7

BTN_MAP = {
    ecodes.KEY_BACK:        ecodes.KEY_F1,
    ecodes.KEY_VOLUMEDOWN:  ecodes.KEY_F2,
    ecodes.KEY_SLEEP:       ecodes.KEY_F3,
}


def find_device(name):
    for path in sorted(glob.glob('/dev/input/event*')):
        try:
            dev = evdev.InputDevice(path)
            if dev.name == name:
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


def create_virtual_keyboard():
    cap = {
        ecodes.EV_KEY: [
            ecodes.KEY_LEFT, ecodes.KEY_RIGHT,
            ecodes.KEY_UP, ecodes.KEY_DOWN,
            ecodes.KEY_F1, ecodes.KEY_F2, ecodes.KEY_F3,
        ],
    }
    return UInput(cap, name='J09 Virtual Keyboard', version=0x1)


def emit_key(vdev, key):
    vdev.write(ecodes.EV_KEY, key, 1)
    vdev.write(ecodes.EV_SYN, ecodes.SYN_REPORT, 0)
    vdev.write(ecodes.EV_KEY, key, 0)
    vdev.write(ecodes.EV_SYN, ecodes.SYN_REPORT, 0)


def run_once(src_touch, src_btn, vdev_mouse, vdev_kbd, app_mode=False):
    prev_x = None
    prev_y = None
    touching = False
    accum_x = 0.0
    accum_y = 0.0
    touch_start_time = 0.0
    touch_start_x = None
    touch_start_y = None
    touch_max_dist = 0

    fds = [src_touch.fd, src_btn.fd]

    try:
        while True:
            r, _, _ = select.select(fds, [], [], 2.0)
            if not r:
                continue

            for fd in r:
                if fd == src_btn.fd:
                    for event in src_btn.read():
                        if event.type == ecodes.EV_KEY and event.value == 1:
                            mapped = BTN_MAP.get(event.code)
                            if mapped:
                                emit_key(vdev_kbd, mapped)

                elif fd == src_touch.fd:
                    for event in src_touch.read():
                        if event.type == ecodes.EV_KEY and event.code == ecodes.BTN_TOUCH:
                            if event.value == 1:
                                touching = True
                                prev_x = None
                                prev_y = None
                                accum_x = 0.0
                                accum_y = 0.0
                                touch_start_time = time.monotonic()
                                touch_start_x = None
                                touch_start_y = None
                                touch_max_dist = 0
                            else:
                                touching = False
                                duration_ms = (time.monotonic() - touch_start_time) * 1000

                                dx = (prev_x - touch_start_x) if (touch_start_x is not None and prev_x is not None) else 0
                                dy = (prev_y - touch_start_y) if (touch_start_y is not None and prev_y is not None) else 0
                                dist = max(abs(dx), abs(dy))

                                if dist >= SWIPE_MIN_DIST:
                                    if abs(dx) >= abs(dy):
                                        key = ecodes.KEY_RIGHT if dx > 0 else ecodes.KEY_LEFT
                                        kname = 'RIGHT' if dx > 0 else 'LEFT'
                                    else:
                                        key = ecodes.KEY_DOWN if dy > 0 else ecodes.KEY_UP
                                        kname = 'DOWN' if dy > 0 else 'UP'
                                    print(f'[J09] swipe dx={dx} dy={dy} dur={duration_ms:.0f}ms → {kname}', flush=True)
                                    emit_key(vdev_kbd, key)
                                elif duration_ms < TAP_MAX_MS and touch_max_dist < TAP_MAX_DIST:
                                    print(f'[J09] tap dist={touch_max_dist} dur={duration_ms:.0f}ms', flush=True)
                                    emit_key(vdev_mouse, ecodes.BTN_LEFT)
                                else:
                                    print(f'[J09] ignored dx={dx} dy={dy} dist={dist} max={touch_max_dist} dur={duration_ms:.0f}ms', flush=True)

                                prev_x = None
                                prev_y = None
                                accum_x = 0.0
                                accum_y = 0.0

                        elif event.type == ecodes.EV_ABS and touching:
                            if event.code == ecodes.ABS_X:
                                if prev_x is not None:
                                    accum_x += (event.value - prev_x) * SPEED
                                touch_start_x = touch_start_x if touch_start_x is not None else event.value
                                if touch_start_x != event.value:
                                    touch_max_dist = max(touch_max_dist, abs(event.value - touch_start_x))
                                prev_x = event.value
                            elif event.code == ecodes.ABS_Y:
                                if prev_y is not None:
                                    accum_y += (event.value - prev_y) * SPEED
                                touch_start_y = touch_start_y if touch_start_y is not None else event.value
                                if touch_start_y != event.value:
                                    touch_max_dist = max(touch_max_dist, abs(event.value - touch_start_y))
                                prev_y = event.value

                        elif event.type == ecodes.EV_SYN and touching and not app_mode:
                            dx = round(accum_x)
                            dy = round(accum_y)
                            if dx or dy:
                                vdev_mouse.write(ecodes.EV_REL, ecodes.REL_X, dx)
                                vdev_mouse.write(ecodes.EV_REL, ecodes.REL_Y, dy)
                                vdev_mouse.write(ecodes.EV_SYN, ecodes.SYN_REPORT, 0)
                                accum_x -= dx
                                accum_y -= dy

    except OSError:
        print('[J09] Device disconnected')


def main():
    app_mode = any(a in ('--app-mode', '--app_mode') for a in sys.argv[1:])

    vdev_mouse = create_virtual_mouse()
    vdev_kbd = create_virtual_keyboard()
    print(f'[J09] Virtual mouse: {vdev_mouse.device.path}')
    print(f'[J09] Virtual keyboard: {vdev_kbd.device.path}')
    if app_mode:
        print('[J09] App mode: mouse movement disabled')

    while True:
        print('[J09] Waiting for J09 devices...')
        src_touch = src_btn = None
        while src_touch is None or src_btn is None:
            src_touch = src_touch or find_device('J09')
            src_btn = src_btn or find_device('J09 Consumer Control')
            if src_touch is None or src_btn is None:
                time.sleep(2)

        print(f'[J09] Touch: {src_touch.path}  Buttons: {src_btn.path}')
        try:
            src_touch.grab()
            src_btn.grab()
        except Exception as e:
            print(f'[J09] grab failed: {e}')
            time.sleep(2)
            continue

        run_once(src_touch, src_btn, vdev_mouse, vdev_kbd, app_mode=app_mode)

        for src in (src_touch, src_btn):
            try:
                src.ungrab()
            except Exception:
                pass

        time.sleep(2)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('[J09] Stopped')
