# The status logic lives in the zara-led system service (/opt/zara-assistant/zara_led.py),
# which answers the sketch's Bridge calls through arduino-router. This app exists only so
# App Lab can build and flash the sketch; its Python side does nothing.
from arduino.app_utils import App

App.run()
