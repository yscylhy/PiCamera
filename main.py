import camera
import cameraGUI
import sys
import os


if sys.platform == 'linux' or sys.platform == 'linux2':
    camera = camera.MyCamera('csi')
elif sys.platform == 'win32':
    camera = camera.MyCamera('usb')
else:
    camera = camera.MyCamera('usb')

app = cameraGUI.PiCameraGUI(camera, os.path.join(os.path.dirname(__file__), 'outputs'))
app.root.mainloop()
