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
from camera import MyCamera

if sys.platform == 'linux' or sys.platform == 'linux2':
    from picamera.array import PiRGBArray
    from picamera import PiCamera
elif sys.platform == 'win32':
    import psutil
else:
    import psutil


iso_options = ["Auto", "100", "200", "400", "800", "1600", "3200"]
exposure_options = ["Auto", "100", "500", "1000", "4000", "16000"]


class PiCameraGUI:
    def __init__(self, camera, outputPath):
        self.camera = camera
        self.outputPath = outputPath
        self.frame = None
        self.thread = None
        self.stopEvent = None
        # initialize the root window and image panel
        self.root = tk.Tk()
        misc.FullScreenApp(self.root)
        self.root.title("PiCamera")
        self.time_stamp = None

        program_directory = sys.path[0]
        self.root.iconphoto(True, tk.PhotoImage(file=os.path.join(program_directory, "./icons/1.png")))
        # self.root.iconbitmap("./1.png")

        self.left_frame = tk.Frame(self.root)
        self.right_frame = tk.LabelFrame(self.root, text="Control region")
        self.left_frame.place(relx=0.05, rely=0.05, relwidth=0.55, relheight=0.9)
        self.right_frame.place(relx=0.65, rely=0.05, relwidth=0.3, relheight=0.9)

        self.left_picture = None

        self.shoot_button = tk.Button(self.right_frame, text="Picture")
        self.shoot_button.bind('<Button-1>', self.takeSnapshot)
        self.shoot_button.place(relx=0.05, rely=0.05, relwidth=0.4, relheight=0.25)
        self.video_button = tk.Button(self.right_frame, text="5s Video")
        self.video_button.place(relx=0.55, rely=0.05, relwidth=0.4, relheight=0.25)
        self.video_button.bind('<Button-1>', self.takeVideo)

        self.iso_frame = tk.Frame(self.right_frame)
        self.iso_frame.place(relx=0, rely=0.4, relwidth=1, relheight=0.25)
        self.iso_button = tk.Button(self.iso_frame, text="ISO", command=self.setISO)
        self.iso_button.place(relx=0.05, rely=0.05, relwidth=0.4, relheight=0.4)
        self.iso_state = tk.StringVar()
        self.iso_state.set(iso_options[0])
        self.iso_entry = tk.OptionMenu(self.iso_frame, self.iso_state, *iso_options)
        self.iso_entry.place(relx=0.55, rely=0.05, relwidth=0.4, relheight=0.4)
        self.analog_gain = tk.Label(self.iso_frame, text="Analog Gain: {}".format(1))
        self.analog_gain.place(relx=0.05, rely=0.5, relwidth=0.9, relheight=0.4)

        self.expo_frame = tk.Frame(self.right_frame)
        self.expo_frame.place(relx=0, rely=0.7, relwidth=1, relheight=0.25)
        self.expo_button = tk.Button(self.expo_frame, text="EXPO", command=self.setExpo)
        self.expo_button.place(relx=0.05, rely=0.05, relwidth=0.4, relheight=0.4)
        self.expo_state = tk.StringVar()
        self.expo_state.set(exposure_options[0])
        self.expo_entry = tk.OptionMenu(self.expo_frame, self.expo_state, *exposure_options)
        self.expo_entry.place(relx=0.55, rely=0.05, relwidth=0.4, relheight=0.4)
        self.expo_time = tk.Label(self.expo_frame, text="Exposure: {}".format(1000))
        self.expo_time.place(relx=0.05, rely=0.5, relwidth=0.9, relheight=0.4)

        self.stopEvent = threading.Event()
        self.videoLoop()
        self.gain_update_loop()
        self.exposure_update_loop()

        # set a callback to handle when the window is closed
        self.root.wm_title("PyImageSearch PhotoBooth")
        self.root.wm_protocol("WM_DELETE_WINDOW", self.onClose)

        self.root.bind('<Prior>', self.takeSnapshot)
        self.root.bind('<Next>', self.takeVideo)

    def takeSnapshot(self, event):
        ts = datetime.datetime.now()
        filename = "{}.png".format(ts.strftime("%Y-%m-%d_%H-%M-%S"))
        p = os.path.abspath(os.path.sep.join((self.outputPath, filename)))
        ret, self.frame = self.camera.read()
        if self.camera.interface == 'csi':
            cv2.imwrite(p, cv2.cvtColor(self.frame, cv2.COLOR_BGR2RGB))
        else:
            cv2.imwrite(p, self.frame)

        print("[INFO] saved {}".format(filename))

    def takeVideo(self, event):
        ts = datetime.datetime.now()
        folder_name = os.path.abspath(os.path.join(self.outputPath, (ts.strftime("%Y-%m-%d_%H-%M-%S"))))
        os.mkdir(folder_name)
        tic = time.time()
        num = 1
        pre_time = None
        while time.time() - tic < 5:
            p = os.path.join(folder_name, "{}.png".format(num))
            if pre_time != self.time_stamp:
                pre_time = self.time_stamp
                if self.camera.interface == 'csi':
                    cv2.imwrite(p, cv2.cvtColor(self.frame, cv2.COLOR_BGR2RGB))
                else:
                    cv2.imwrite(p, self.frame)

            num += 1
            ret, self.frame = self.camera.read()
            self.time_stamp = time.time()

        print("[INFO] saved {}".format(folder_name))

    def setISO(self):
        iso_value = self.iso_state.get()
        if iso_value == 'Auto':
            self.camera.camera.iso = 0
        else:
            self.camera.camera.iso = int(iso_value)

    def setExpo(self):
        expo_value = self.expo_state.get()
        if expo_value == 'Auto':
            self.camera.camera.shutter_speed = 0
        else:
            self.camera.camera.shutter_speed = int(expo_value)

    def videoLoop(self):
        if not self.stopEvent.is_set():
            ret, self.frame = self.camera.read()
            self.time_stamp = time.time()

            if self.camera.interface == 'usb':
                image = cv2.cvtColor(self.frame, cv2.COLOR_BGR2RGB)
            else:
                image = self.frame

            h_size = int(self.left_frame.winfo_height())
            # w_size = int(self.left_frame.winfo_width())
            w_size = int(h_size*4//3)

            image = Image.fromarray(image).resize((w_size, h_size))
            # image = Image.fromarray(image)
            to_display = ImageTk.PhotoImage(image=image)

            if self.left_picture is None:
                self.left_picture = tk.Label(self.left_frame, image=to_display)
                self.left_picture.image = to_display
                self.left_picture.pack(side="left", padx=10, pady=10)
            else:
                self.left_picture.configure(image=to_display)
                self.left_picture.image = to_display
        self.left_picture.after(10, self.videoLoop)

    def gain_update_loop(self):
        if not self.stopEvent.is_set():
            if os.name == 'nt':
                cpu_usage = psutil.cpu_percent()
                self.analog_gain['text'] = "cpu per: {}".format(cpu_usage)
            else:
                gain = float(self.camera.camera.analog_gain)
                self.analog_gain['text'] = "gain: {}".format(gain)
        self.analog_gain.after(1000, self.gain_update_loop)

    def exposure_update_loop(self):
        if not self.stopEvent.is_set():
            if os.name == 'nt':
                cpu_freq = psutil.cpu_freq().current
                self.expo_time['text'] = "cpu freq: {}".format(cpu_freq)
            else:
                exposure_time = self.camera.camera.exposure_speed
                self.expo_time['text'] = "expo time: {}".format(exposure_time)

        self.expo_time.after(1000, self.exposure_update_loop)

    def onClose(self):
        # set the stop event, cleanup the camera, and allow the rest of
        # the quit process to continue
        print("[INFO] closing...")
        self.stopEvent.set()
        self.camera.release()
        self.root.quit()

