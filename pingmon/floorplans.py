"""Floor-plan image storage: save uploads, convert PDFs to a raster image,
best-effort dimension detection. No hard third-party dependency — PDF support
needs poppler's `pdftoppm` (installed by install.sh) or PyMuPDF; if neither is
present, PDF uploads are rejected with a clear message."""
import os
import shutil
import struct
import subprocess
import tempfile

from . import database

ALLOWED_EXT = {"jpg", "jpeg", "png", "svg", "pdf"}
STORE_EXT = {"jpg": "jpg", "jpeg": "jpg", "png": "png", "svg": "svg", "pdf": "png"}
MIME = {"png": "image/png", "jpg": "image/jpeg", "svg": "image/svg+xml"}


def _dir():
    d = os.path.join(database.DATA_DIR, "floorplans")
    os.makedirs(d, exist_ok=True)
    return d


def image_path(fp_id, ext):
    return os.path.join(_dir(), f"{fp_id}.{ext}")


def ext_of(filename):
    e = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    return e


def _png_dims(path):
    try:
        with open(path, "rb") as f:
            head = f.read(24)
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            return struct.unpack(">II", head[16:24])
    except Exception:
        pass
    return (None, None)


def _jpeg_dims(path):
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"\xff\xd8":
                return (None, None)
            while True:
                b = f.read(1)
                while b and b != b"\xff":
                    b = f.read(1)
                marker = f.read(1)
                while marker == b"\xff":
                    marker = f.read(1)
                if marker in (b"\xc0", b"\xc1", b"\xc2", b"\xc3"):
                    f.read(3)
                    h, w = struct.unpack(">HH", f.read(4))
                    return (w, h)
                seg = f.read(2)
                if len(seg) < 2:
                    return (None, None)
                f.seek(struct.unpack(">H", seg)[0] - 2, 1)
    except Exception:
        return (None, None)


def _dims(path, store_ext):
    if store_ext == "png":
        return _png_dims(path)
    if store_ext == "jpg":
        return _jpeg_dims(path)
    return (None, None)   # svg scales freely


def _convert_pdf(src_pdf, dst_png):
    """First page of a PDF -> PNG. Tries poppler, then PyMuPDF."""
    exe = shutil.which("pdftoppm")
    if exe:
        base = dst_png[:-4] if dst_png.endswith(".png") else dst_png
        try:
            subprocess.run([exe, "-png", "-singlefile", "-r", "150", src_pdf, base],
                           check=True, timeout=60,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            raise RuntimeError("Couldn't read that PDF — it may be corrupt or "
                               "password-protected. Try exporting it as PNG.") from e
        if not os.path.exists(dst_png) and os.path.exists(base + ".png"):
            os.replace(base + ".png", dst_png)
        if not os.path.exists(dst_png):
            raise RuntimeError("PDF conversion produced no image. Try a PNG/JPG.")
        return
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(src_pdf)
        page = doc.load_page(0)
        page.get_pixmap(dpi=150).save(dst_png)
        doc.close()
        return
    except Exception as e:
        raise RuntimeError(
            "PDF support isn't installed on the server. Install poppler-utils "
            "(apt-get install -y poppler-utils) or upload a PNG/JPG/SVG instead."
        ) from e


def save_upload(file_storage, fp_id):
    """Persist an uploaded floor-plan file for the given plan id.
    Returns (store_ext, w, h). Raises ValueError on an unsupported type."""
    raw_ext = ext_of(file_storage.filename)
    if raw_ext not in ALLOWED_EXT:
        raise ValueError("Unsupported file type. Allowed: JPG, PNG, SVG, PDF.")
    store_ext = STORE_EXT[raw_ext]
    dst = image_path(fp_id, store_ext)
    if raw_ext == "pdf":
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            file_storage.save(tmp.name)
            tmp_pdf = tmp.name
        try:
            _convert_pdf(tmp_pdf, dst)
        finally:
            try:
                os.unlink(tmp_pdf)
            except OSError:
                pass
    else:
        file_storage.save(dst)
    w, h = _dims(dst, store_ext)
    return store_ext, w, h


def delete_image(fp_id, ext):
    try:
        p = image_path(fp_id, ext)
        if os.path.exists(p):
            os.unlink(p)
    except OSError:
        pass
