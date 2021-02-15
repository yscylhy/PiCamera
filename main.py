"""
Tutorial links:
1. https://www.pyimagesearch.com/2016/05/30/displaying-a-video-feed-with-opencv-and-tkinter/
2.
"""


import tkinter as tk
import numpy as pi
import cv2
from PIL import Image, ImageTk
import datetime
import imutils
import misc
import my_utils


camera = my_utils.MyCamera('usb')
# camera = my_utils.MyCamera('csi')

app = my_utils.PiCameraGUI(camera, './output')
app.root.mainloop()
