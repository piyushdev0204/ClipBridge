# iPhone -> Windows clipboard receiver (text + images, works in Explorer and browsers)
# Setup: pip install flask pyperclip pywin32 pillow pillow-heif

import sys, os
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import io
import time
import struct
from flask import Flask, request, abort
import pyperclip
import win32clipboard
from PIL import Image, ImageOps
import pillow_heif

pillow_heif.register_heif_opener()  # lets Pillow read iPhone HEIC photos

SECRET = "change-this-to-your-own-secret"  # must match the X-Token in your iPhone shortcut
PORT = 5000
SAVE_DIR = os.path.join(os.path.expanduser("~"), "Pictures", "iPhone Clipboard")
KEEP_DAYS = 7  # saved images older than this are deleted automatically

app = Flask(__name__)


def try_open_image(data):
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img
    except Exception:
        return None


def prune_old_files():
    cutoff = time.time() - KEEP_DAYS * 86400
    try:
        for name in os.listdir(SAVE_DIR):
            path = os.path.join(SAVE_DIR, name)
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
    except Exception:
        pass


def set_clipboard_image(img):
    img = ImageOps.exif_transpose(img)

    # Save a PNG file (used for File Explorer / file-paste and as the browser format)
    os.makedirs(SAVE_DIR, exist_ok=True)
    prune_old_files()
    png_img = img.convert("RGBA") if img.mode in ("RGBA", "LA", "P") else img.convert("RGB")
    png_buf = io.BytesIO()
    png_img.save(png_buf, "PNG")
    png_bytes = png_buf.getvalue()
    filepath = os.path.join(SAVE_DIR, time.strftime("clip_%Y%m%d_%H%M%S.png"))
    with open(filepath, "wb") as f:
        f.write(png_bytes)

    # Bitmap data (used by Paint, Word, chat apps)
    bmp_buf = io.BytesIO()
    img.convert("RGB").save(bmp_buf, "BMP")
    dib = bmp_buf.getvalue()[14:]  # strip the 14-byte BMP file header to get a DIB

    # File list (used by File Explorer paste): DROPFILES header + UTF-16 path
    hdrop = struct.pack("<IiiII", 20, 0, 0, 0, 1) + (filepath + "\0").encode("utf-16-le") + b"\0\0"

    png_format = win32clipboard.RegisterClipboardFormat("PNG")  # what browsers look for

    for _ in range(5):  # retry in case another app is holding the clipboard
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib)
                win32clipboard.SetClipboardData(png_format, png_bytes)
                win32clipboard.SetClipboardData(win32clipboard.CF_HDROP, hdrop)
            finally:
                win32clipboard.CloseClipboard()
            return True
        except Exception:
            time.sleep(0.2)
    return False


@app.route("/Clip", methods=["POST"])
@app.route("/clip", methods=["POST"])
def clip():
    if request.headers.get("X-Token") != SECRET:
        abort(403)
    data = request.get_data()
    if not data:
        return "empty", 400

    img = try_open_image(data)
    if img is not None:
        return ("image ok", 200) if set_clipboard_image(img) else ("clipboard busy", 503)

    pyperclip.copy(data.decode("utf-8", errors="replace"))
    return "text ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)