# iPhone -> Windows clipboard receiver (text + images, works in Explorer and browsers)
# Setup: pip install flask pyperclip pywin32 pillow pillow-heif striprtf

import sys, os
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import io
import re
import time
import struct
import plistlib
import zipfile
from html.parser import HTMLParser
from flask import Flask, request, abort
import pyperclip
import win32clipboard
from PIL import Image, ImageOps
import pillow_heif
from striprtf.striprtf import rtf_to_text

pillow_heif.register_heif_opener()  # lets Pillow read iPhone HEIC photos

SECRET = "Change-to-your-own"  # must match the X-Token in your iPhone shortcut
PORT = 5000
SAVE_DIR = os.path.join(os.path.expanduser("~"), "Pictures", "iPhone Clipboard")
KEEP_DAYS = 7  # saved images older than this are deleted automatically

app = Flask(__name__)


class _HTMLToText(HTMLParser):
    """Minimal HTML -> plain text converter."""

    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in ("br", "p", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def _plist_strings(obj, out):
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _plist_strings(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _plist_strings(v, out)


def _balanced_rtf(buf):
    """Return just the RTF document at the start of buf (stops at its closing brace)."""
    depth = 0
    i = 0
    n = len(buf)
    while i < n:
        c = buf[i:i + 1]
        if c == b"\\":
            i += 2  # skip escaped characters such as \{ \} \\
            continue
        if c == b"{":
            depth += 1
        elif c == b"}":
            depth -= 1
            if depth == 0:
                return buf[:i + 1]
        i += 1
    return buf


def extract_text(data):
    """Turn whatever the iPhone sent (plain text, RTF, HTML, archived rich text) into plain text.

    Text copied from photos or the camera (Live Text) often arrives as rich text,
    which otherwise pastes as raw code or gibberish.
    """
    head = data[:64].lstrip()

    # RTFD package sent as a zip archive: pull out the RTF file inside
    if data.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for name in z.namelist():
                    if name.lower().endswith(".rtf"):
                        return rtf_to_text(z.read(name).decode("latin-1")).strip()
        except Exception:
            pass

    # Rich Text Format, including RTF embedded inside an RTFD container
    idx = data.find(b"{\\rtf")
    if idx != -1:
        try:
            rtf = _balanced_rtf(data[idx:])
            return rtf_to_text(rtf.decode("latin-1")).strip()
        except Exception:
            pass

    # HTML
    if head[:1] == b"<" and re.search(rb"<\s*(html|body|p|div|span|br)\b", data[:2000], re.I):
        try:
            parser = _HTMLToText()
            parser.feed(data.decode("utf-8", errors="replace"))
            text = "".join(parser.parts)
            return re.sub(r"\n{3,}", "\n\n", text).strip()
        except Exception:
            pass

    # Apple binary property list (archived attributed string): best-effort, take the longest text
    if data.startswith(b"bplist00"):
        try:
            found = []
            _plist_strings(plistlib.loads(data), found)
            found = [s for s in found if s and s != "$null" and not s.startswith("NS")]
            if found:
                return max(found, key=len).strip()
        except Exception:
            pass

    # Unrecognized binary data: keep a copy so the format can be inspected
    if b"\x00" in data[:500] and data[:2] not in (b"\xff\xfe", b"\xfe\xff"):
        try:
            os.makedirs(SAVE_DIR, exist_ok=True)
            with open(os.path.join(SAVE_DIR, "last_unrecognized_clipboard.bin"), "wb") as f:
                f.write(data)
        except Exception:
            pass

    # Plain text (UTF-16 if it has a byte-order mark, otherwise UTF-8)
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8", errors="replace")


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

    text = extract_text(data)
    if not text:
        return "empty", 400
    pyperclip.copy(text)
    return "text ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
