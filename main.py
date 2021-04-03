import camera
import cameraGUI
import sys


if sys.platform is 'linux' or sys.platform is 'linux2':
    camera = camera.MyCamera('csi')
elif sys.platform is 'win32':
    camera = camera.MyCamera('usb')
else:
    camera = camera.MyCamera('usb')

app = cameraGUI.PiCameraGUI(camera, './outputs')
app.root.mainloop()
