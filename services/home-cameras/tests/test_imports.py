try:
    import flask
    print("Flask imported successfully")
except ImportError as e:
    print(f"Failed to import Flask: {e}")

try:
    import cv2
    print("OpenCV imported successfully")
except ImportError as e:
    print(f"Failed to import OpenCV: {e}")

try:
    import numpy
    print("NumPy imported successfully")
except ImportError as e:
    print(f"Failed to import NumPy: {e}")

try:
    import av
    print("PyAV imported successfully")
except ImportError as e:
    print(f"Failed to import PyAV: {e}")

print("Import tests completed")