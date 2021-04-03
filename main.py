import camera
import cameraGUI
import sys


if sys.platform == 'linux' or sys.platform == 'linux2':
    camera = camera.MyCamera('csi')
elif sys.platform == 'win32':
    camera = camera.MyCamera('usb')
else:
    camera = camera.MyCamera('usb')

app = cameraGUI.PiCameraGUI(camera, './outputs')
app.root.mainloop()
