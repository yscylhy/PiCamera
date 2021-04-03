import tkinter as tk
import cv2
from PIL import Image, ImageTk
import datetime
import imutils
import misc
import threading
import os
import time
import cv2
import sys
from io import BytesIO
from fractions import Fraction
import numpy as np

if sys.platform is 'linux' or sys.platform is 'linux2':
    from picamera import PiCamera


class MyCamera:
    def __init__(self, interface):
        self.interface = interface
        if self.interface == "usb":
            self.camera = cv2.VideoCapture(0)
        elif self.interface == "csi":
            self.camera = PiCamera() # 800*480 for HQ Pi Camera default
            self.camera.resolution = (1024, 768)
            # self.camera.resolution = (2592, 1944) # max for Pi camera v1
            # self.camera.resolution = (3280, 2464) # max for Pi camera v2
            # self.camera.resolution = (4056, 3040) # max for HQ pi Camera

    def read(self):
        if self.interface == "usb":
            return self.camera.read()
        elif self.interface == "csi":
            frame = np.empty((self.camera.resolution.width*self.camera.resolution.height*3, ), dtype=np.uint8)
            self.camera.capture(frame, format="rgb")

            if frame is None:
                return [False, frame]
            else:
                frame = frame.reshape((self.camera.resolution.height, self.camera.resolution.width, 3))
                return [True, frame]

    def release(self):
        if self.interface == "usb":
            self.camera.release()
