"""The phone's Files tab: browse the PC, preview, download (one file or a zip
of many), and upload into a short list of ordinary folders.

The rules, in one place, because they are the whole point:

  Reading   Anything the Windows account can read, EXCEPT this app's own
            data (the login secret, sessions, keys) and the assistant's
            folder. A signed-in phone could otherwise copy out the very
            secret that lets it sign in.
  Previews  Pictures are re-encoded here (never served as-is), text is sent
            as JSON for the phone to show as text, and video/audio/PDF go
            out with a sandbox header. SVG, HTML and friends are never shown
            inline - same-origin script would be the phone app itself.
  Writing   Only uploads - no delete, rename, move or overwrite anywhere.
            Only into the user's own folders (not AppData, so not Startup)
            or a non-system drive, never over an existing file, never a
            shortcut or a file Explorer acts on by itself. And the app asks
            for Face ID / the PIN before each batch.
"""
import ctypes
import ctypes.wintypes as wt
import hashlib
import io
import os
import re
import secrets
import threading
import time
import zipfile

import paths

# -------------------------------------------------------------------- places

_FOLDERS = [
    ("Desktop", "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}", "desktop"),
    ("Documents", "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}", "documents"),
    ("Downloads", "{374DE290-123F-4565-9164-39C4925E467B}", "downloads"),
    ("Pictures", "{33E28130-4E1E-4676-835A-98395C3BC3BB}", "pictures"),
    ("Videos", "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}", "videos"),
    ("Music", "{4BD8D571-6D19-48D3-BE97-422220080E43}", "music"),
]


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


_shell32 = ctypes.WinDLL("shell32")
_ole32 = ctypes.WinDLL("ole32")
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32")


def _guid(s):
    g = _GUID()
    _ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
    return g


def known_folder(guid):
    """Where Windows really keeps Desktop etc. - OneDrive often moves them."""
    p = ctypes.c_wchar_p()
    try:
        if _shell32.SHGetKnownFolderPath(ctypes.byref(_guid(guid)), 0, None, ctypes.byref(p)) == 0:
            return p.value
    except Exception:
        pass
    finally:
        if p:
            _ole32.CoTaskMemFree(p)
    return None


def places():
    out = []
    for name, guid, icon in _FOLDERS:
        p = known_folder(guid) or os.path.join(os.path.expanduser("~"), name)
        if os.path.isdir(p):
            out.append({"name": name, "path": p, "icon": icon})
    home = os.path.expanduser("~")
    if os.path.isdir(home):
        out.append({"name": os.path.basename(home) or "Home", "path": home, "icon": "home"})
    return out


def drives():
    out = []
    mask = _kernel32.GetLogicalDrives()
    sysdrive = (os.environ.get("SystemDrive") or "C:").upper()[:1]
    for i in range(26):
        if not mask & (1 << i):
            continue
        root = "%s:\\" % chr(65 + i)
        kind = _kernel32.GetDriveTypeW(root)
        if kind not in (2, 3, 4):         # removable, fixed, network
            continue
        label = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(261)
        ok = _kernel32.GetVolumeInformationW(root, label, 261, None, None, None, fs, 261)
        if not ok:
            continue                       # e.g. an empty card reader
        free, total = ctypes.c_ulonglong(), ctypes.c_ulonglong()
        _kernel32.GetDiskFreeSpaceExW(root, None, ctypes.byref(total), ctypes.byref(free))
        out.append({"name": label.value or ("Local Disk" if kind == 3 else "Drive"),
                    "path": root, "letter": chr(65 + i),
                    "icon": "usb" if kind == 2 else "net" if kind == 4 else "drive",
                    "system": chr(65 + i) == sysdrive,
                    "free": free.value, "total": total.value})
    return out


# -------------------------------------------------------------------- rules

def _norm(p):
    return os.path.normcase(os.path.abspath(p)).rstrip("\\/") + os.sep


def _denied_roots():
    d = [paths.DATA_DIR, r"C:\assistant"]
    if not paths.FROZEN:
        d.append(paths.ASSET_DIR)          # a checkout keeps auth.json beside the code
    return [_norm(x) for x in d if x]


def readable(p):
    """May the phone read this path (list / download / preview)?"""
    try:
        n = _norm(p)
    except Exception:
        return False
    return not any(n.startswith(r) for r in _denied_roots())


def writable_dir(p):
    """May the phone upload into this folder?"""
    if not p or not os.path.isdir(p) or not readable(p):
        return False
    n = _norm(p)
    home = _norm(os.path.expanduser("~"))
    if n.startswith(home):
        # Your own folders - but not AppData (which is where Startup lives,
        # and every app's settings).
        return not n.startswith(_norm(os.path.join(home, "AppData")))
    drive = os.path.splitdrive(n)[0].upper()[:1]
    sysdrive = (os.environ.get("SystemDrive") or "C:").upper()[:1]
    if not drive or drive == sysdrive or n.startswith("\\\\"):
        return False
    if _kernel32.GetDriveTypeW(drive + ":\\") not in (2, 3):
        return False
    low = n.lower()
    return not any(("\\" + x + "\\") in low for x in (
        "windows", "program files", "program files (x86)", "programdata",
        "$recycle.bin", "system volume information"))


# Files Explorer acts on just by showing the folder, or that run on a click
# looking like something else.
_BAD_NAMES = {"desktop.ini", "autorun.inf", "thumbs.db"}
_BAD_EXT = {".lnk", ".url", ".scf", ".library-ms", ".searchconnector-ms", ".pif"}
_RESERVED = {"con", "prn", "aux", "nul"} | {"com%d" % i for i in range(1, 10)} | \
            {"lpt%d" % i for i in range(1, 10)}


def clean_name(name):
    name = os.path.basename(str(name or "").replace("\\", "/")).strip()
    name = "".join(c for c in name if c >= " " and c not in '<>:"/\\|?*')
    name = name.strip(" .")[:180]
    if not name:
        return None
    stem, ext = os.path.splitext(name)
    if name.lower() in _BAD_NAMES or ext.lower() in _BAD_EXT:
        return None
    if stem.lower() in _RESERVED:
        name = "_" + name
    return name


def free_name(folder, name):
    stem, ext = os.path.splitext(name)
    cand, i = name, 1
    while os.path.exists(os.path.join(folder, cand)) or \
            os.path.exists(os.path.join(folder, cand + ".aether-part")):
        cand = "%s (%d)%s" % (stem, i, ext)
        i += 1
    return cand


# -------------------------------------------------------------------- kinds

KINDS = {
    "image": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".ico", ".heic"},
    "video": {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".wmv", ".flv", ".ts"},
    "audio": {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma"},
    "pdf": {".pdf"},
    "text": {".txt", ".md", ".log", ".csv", ".json", ".xml", ".yml", ".yaml", ".ini",
             ".cfg", ".conf", ".py", ".js", ".ts", ".css", ".html", ".htm", ".bat",
             ".ps1", ".sh", ".c", ".cpp", ".h", ".cs", ".java", ".go", ".rs", ".sql",
             ".toml", ".srt", ".tex", ".lua", ".rb", ".php", ".svg"},
    "archive": {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".iso"},
    "doc": {".doc", ".docx", ".odt", ".rtf", ".pages"},
    "sheet": {".xls", ".xlsx", ".ods", ".numbers"},
    "slides": {".ppt", ".pptx", ".odp", ".key"},
    "app": {".exe", ".msi", ".appx", ".msix", ".apk"},
}
_EXT_KIND = {e: k for k, es in KINDS.items() for e in es}

# What the phone can play/show straight from the file. Everything here goes
# out with a sandbox CSP; nothing that can carry script is on the list.
INLINE_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".wav": "audio/wav",
    ".flac": "audio/flac", ".ogg": "audio/ogg", ".opus": "audio/ogg",
    ".pdf": "application/pdf",
}


def kind_of(name):
    return _EXT_KIND.get(os.path.splitext(name)[1].lower(), "file")


FILE_ATTRIBUTE_HIDDEN, FILE_ATTRIBUTE_SYSTEM = 0x2, 0x4
MAX_LIST = 5000


def _entry(de):
    st = de.stat(follow_symlinks=False)
    attrs = getattr(st, "st_file_attributes", 0)
    if attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM):
        return None
    isdir = de.is_dir(follow_symlinks=False)
    if isdir and de.name.startswith("$"):
        return None
    return {"name": de.name, "path": de.path, "dir": isdir,
            "size": None if isdir else st.st_size, "mtime": int(st.st_mtime),
            "kind": "folder" if isdir else kind_of(de.name)}


def listing(p):
    if not p:
        return {"path": "", "up": None, "home": True, "places": places(),
                "drives": drives(), "entries": []}
    p = os.path.abspath(p)
    up = os.path.dirname(p.rstrip("\\"))
    up = up if up and up != p else ""
    if not readable(p):
        return {"path": p, "up": up, "entries": [], "error": "That folder is off limits to the phone."}
    if not os.path.isdir(p):
        return {"path": p, "up": up, "entries": [], "error": "That folder doesn't exist any more."}
    out = []
    try:
        with os.scandir(p) as it:
            for de in it:
                try:
                    e = _entry(de)
                except OSError:
                    continue
                if e and readable(e["path"]):
                    out.append(e)
                if len(out) >= MAX_LIST:
                    break
    except PermissionError:
        return {"path": p, "up": up, "entries": [], "error": "Windows won't let this account read that folder."}
    except OSError as e:
        return {"path": p, "up": up, "entries": [], "error": str(e)}
    return {"path": p, "up": up, "name": os.path.basename(p.rstrip("\\")) or p,
            "entries": out, "capped": len(out) >= MAX_LIST, "writable": writable_dir(p)}


def search(p, q, limit=300, budget=5.0):
    """Name search under a folder - breadth first, so near things come first,
    and bounded so a search of C:\\ answers in seconds, not minutes."""
    p = os.path.abspath(p)
    q = q.strip().lower()
    if not q or not os.path.isdir(p) or not readable(p):
        return {"path": p, "q": q, "entries": [], "done": True}
    t0 = time.time()
    found, queue, done = [], [p], True
    while queue:
        if time.time() - t0 > budget or len(found) >= limit:
            done = False
            break
        d = queue.pop(0)
        try:
            with os.scandir(d) as it:
                for de in it:
                    try:
                        e = _entry(de)
                    except OSError:
                        continue
                    if not e or not readable(e["path"]):
                        continue
                    if q in de.name.lower():
                        e["where"] = os.path.relpath(d, p) if d != p else ""
                        found.append(e)
                    if e["dir"]:
                        queue.append(e["path"])
        except OSError:
            continue
    return {"path": p, "q": q, "entries": found[:limit], "done": done}


# -------------------------------------------------------------------- previews

THUMB_DIR = paths.data("thumbs")
_thumb_lock = threading.Lock()


def _thumb_key(p, size):
    st = os.stat(p)
    raw = "%s|%d|%d|%d" % (os.path.normcase(p), st.st_mtime_ns, st.st_size, size)
    return hashlib.sha1(raw.encode("utf-8", "surrogatepass")).hexdigest()


def _pil_image(p, size):
    from PIL import Image, ImageOps
    with Image.open(p) as im:
        im.draft("RGB", (size * 2, size * 2))
        im = ImageOps.exif_transpose(im)
        im.thumbnail((size, size))
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (24, 24, 32))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        return im.convert("RGB")


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _BITMAP(ctypes.Structure):
    _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long),
                ("bmHeight", ctypes.c_long), ("bmWidthBytes", ctypes.c_long),
                ("bmPlanes", wt.WORD), ("bmBitsPixel", wt.WORD), ("bmBits", ctypes.c_void_p)]


class _BIH(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


_SHCreateItem = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_wchar_p, ctypes.c_void_p,
                                   ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))(
    ("SHCreateItemFromParsingName", _shell32))
_GetObjectW = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p)(
    ("GetObjectW", _gdi32))
_GetDIBits = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint)(
    ("GetDIBits", _gdi32))
_DeleteObject = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p)(("DeleteObject", _gdi32))
_CreateCompatibleDC = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(("CreateCompatibleDC", _gdi32))
_DeleteDC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p)(("DeleteDC", _gdi32))
_IID_IMGFACTORY = _guid("{bcc18b79-ba16-442f-80c4-8a59c30c463b}")


def _shell_thumb(p, size):
    """Explorer's own thumbnail (video frames, PDF first pages, ...), or None.
    THUMBNAILONLY: a generic file-type icon is no use as a preview."""
    from PIL import Image
    hr_init = _ole32.CoInitializeEx(None, 0x2)
    item = ctypes.c_void_p()
    try:
        if _SHCreateItem(p, None, ctypes.byref(_IID_IMGFACTORY), ctypes.byref(item)) != 0 or not item:
            return None
        vt = ctypes.cast(item, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        GetImage = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, _SIZE, ctypes.c_int,
                                      ctypes.POINTER(ctypes.c_void_p))(vt[3])
        Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vt[2])
        hbm = ctypes.c_void_p()
        try:
            hr = GetImage(item, _SIZE(size, size), 0x8 | 0x1, ctypes.byref(hbm))  # THUMBNAILONLY|BIGGERSIZEOK
        finally:
            Release(item)
        if hr != 0 or not hbm:
            return None
        try:
            bm = _BITMAP()
            _GetObjectW(hbm, ctypes.sizeof(bm), ctypes.byref(bm))
            w, h = bm.bmWidth, abs(bm.bmHeight)
            if not w or not h or w * h > 4096 * 4096:
                return None
            bih = _BIH(ctypes.sizeof(_BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
            buf = ctypes.create_string_buffer(w * h * 4)
            dc = _CreateCompatibleDC(None)
            try:
                if not _GetDIBits(dc, hbm, 0, h, buf, ctypes.byref(bih), 0):
                    return None
            finally:
                _DeleteDC(dc)
            im = Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1)
            a = im.getchannel("A")
            if a.getextrema()[1] == 0:
                im = im.convert("RGB")          # no alpha at all: it's opaque
            else:
                bg = Image.new("RGB", im.size, (24, 24, 32))
                bg.paste(im.convert("RGB"), mask=a)
                im = bg
            im.thumbnail((size, size))
            return im
        finally:
            _DeleteObject(hbm)
    except Exception:
        return None
    finally:
        if hr_init in (0, 1):
            _ole32.CoUninitialize()


def thumbnail(p, size=240):
    """JPEG bytes for a file's thumbnail, cached on disk, or None."""
    size = max(64, min(int(size or 240), 1024))
    if not os.path.isfile(p) or not readable(p):
        return None
    try:
        key = _thumb_key(p, size)
    except OSError:
        return None
    cache = os.path.join(THUMB_DIR, key[:2], key + ".jpg")
    if os.path.isfile(cache):
        with open(cache, "rb") as f:
            return f.read()
    k = kind_of(p)
    im = None
    try:
        if k == "image":
            try:
                im = _pil_image(p, size)
            except Exception:
                im = None
        if im is None and k in ("image", "video", "pdf", "doc", "sheet", "slides", "audio"):
            im = _shell_thumb(p, size)
    except Exception:
        im = None
    if im is None:
        return None
    out = io.BytesIO()
    im.save(out, "JPEG", quality=82)
    data = out.getvalue()
    with _thumb_lock:
        try:
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache + ".tmp", "wb") as f:
                f.write(data)
            os.replace(cache + ".tmp", cache)
        except OSError:
            pass
    return data


def view_image(p, size=2048):
    """A picture for the full-screen viewer: re-encoded, rotated upright,
    small enough for a phone."""
    if not os.path.isfile(p) or not readable(p) or kind_of(p) != "image":
        return None
    try:
        im = _pil_image(p, size)
    except Exception:
        im = _shell_thumb(p, min(size, 1024))
    if im is None:
        return None
    out = io.BytesIO()
    im.save(out, "JPEG", quality=88)
    return out.getvalue()


TEXT_MAX = 256 * 1024


def text_preview(p):
    if not os.path.isfile(p) or not readable(p):
        return None
    with open(p, "rb") as f:
        raw = f.read(TEXT_MAX + 1)
    if b"\x00" in raw[:4096]:
        return {"binary": True}
    encs = ("utf-16",) if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig", "cp1252")
    for enc in encs:
        try:
            txt = raw[:TEXT_MAX].decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        txt = raw[:TEXT_MAX].decode("utf-8", "replace")
    return {"text": txt, "truncated": len(raw) > TEXT_MAX, "size": os.path.getsize(p)}


# -------------------------------------------------------------------- zips

_zip_lock = threading.Lock()
_zips = {}
ZIP_TTL = 300
ZIP_MAX_FILES = 20000


def prepare_zip(items):
    """Check a selection and park it under a one-time token. The download
    itself is a plain GET (so the phone's own download manager handles it)
    that must present the token AND the session."""
    clean = []
    for p in items[:500]:
        p = os.path.abspath(str(p))
        if not readable(p) or not os.path.exists(p):
            raise ValueError("can't include %s" % os.path.basename(p))
        clean.append(p)
    if not clean:
        raise ValueError("nothing selected")
    tok = secrets.token_urlsafe(18)
    now = time.time()
    with _zip_lock:
        for k in [k for k, v in _zips.items() if now - v["at"] > ZIP_TTL]:
            _zips.pop(k, None)
        _zips[tok] = {"items": clean, "at": now}
    base = os.path.basename(os.path.dirname(clean[0]).rstrip("\\")) or "PC"
    name = (os.path.basename(clean[0]) if len(clean) == 1 else base) + ".zip"
    return tok, re.sub(r'[\\/:*?"<>|]', "_", name)


def take_zip(tok):
    with _zip_lock:
        z = _zips.pop(tok or "", None)
    if not z or time.time() - z["at"] > ZIP_TTL:
        return None
    return z["items"]


def _walk(items):
    n = 0
    for p in items:
        if os.path.isfile(p):
            yield p, os.path.basename(p)
            n += 1
            continue
        top = os.path.dirname(p.rstrip("\\"))
        for d, dirs, fs in os.walk(p):
            dirs[:] = [x for x in dirs if readable(os.path.join(d, x))]
            for f in fs:
                full = os.path.join(d, f)
                if not readable(full):
                    continue
                n += 1
                if n > ZIP_MAX_FILES:
                    return
                yield full, os.path.relpath(full, top)


class _Out:
    """zipfile writes into this; it has no tell/seek, so zipfile streams
    (data descriptors) instead of going back to patch headers."""
    def __init__(self, w):
        self.w, self.n = w, 0

    def write(self, b):
        self.w.write(b)
        self.n += len(b)
        return len(b)

    def flush(self):
        pass


def stream_zip(items, wfile):
    out = _Out(wfile)
    # Stored, not deflated: phones' photos and videos don't shrink, and a
    # PC CPU shouldn't be the bottleneck of a download.
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for full, arc in _walk(items):
            try:
                z.write(full, arc)
            except (PermissionError, FileNotFoundError):
                continue
    return out.n


# -------------------------------------------------------------------- uploads

_up_lock = threading.Lock()
_tickets = {}
TICKET_TTL = 15 * 60
UPLOAD_MAX = 8 * 1024 ** 3


def upload_ticket(folder, session):
    folder = os.path.abspath(folder)
    if not writable_dir(folder):
        raise ValueError("Uploads can only go into your own folders or a non-system drive.")
    tok = secrets.token_urlsafe(18)
    with _up_lock:
        now = time.time()
        for k in [k for k, v in _tickets.items() if now - v["at"] > TICKET_TTL]:
            _tickets.pop(k, None)
        _tickets[tok] = {"dir": folder, "at": now, "session": session}
    return tok


def check_ticket(tok, session):
    with _up_lock:
        t = _tickets.get(tok or "")
    if not t or time.time() - t["at"] > TICKET_TTL or t["session"] != session:
        return None
    return t["dir"]


def receive(folder, name, length, rfile):
    """Write `length` bytes from rfile into folder/name - a new file, never
    over an old one. Returns the name used."""
    if not writable_dir(folder):
        raise PermissionError("not an upload folder")
    name = clean_name(name)
    if not name:
        raise ValueError("That file name isn't allowed.")
    if length < 0 or length > UPLOAD_MAX:
        raise ValueError("Files up to 8 GB.")
    free = ctypes.c_ulonglong()
    _kernel32.GetDiskFreeSpaceExW(folder, ctypes.byref(free), None, None)
    if free.value and length > free.value - 512 * 1024 * 1024:
        raise ValueError("Not enough space on that drive.")
    with _up_lock:
        final = free_name(folder, name)
        part = os.path.join(folder, final + ".aether-part")
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
    ok = False
    try:
        with os.fdopen(fd, "wb") as f:
            left = length
            while left:
                chunk = rfile.read(min(1 << 20, left))
                if not chunk:
                    raise ConnectionError("the upload stopped part way")
                f.write(chunk)
                left -= len(chunk)
        with _up_lock:
            final2 = final
            while True:
                try:
                    os.rename(part, os.path.join(folder, final2))   # never replaces
                    break
                except FileExistsError:
                    final2 = free_name(folder, name)
        ok = True
        return final2
    finally:
        if not ok:
            try:
                os.remove(part)
            except OSError:
                pass


def prune_thumbs(max_files=4000):
    try:
        allf = []
        for d, _, fs in os.walk(THUMB_DIR):
            for f in fs:
                p = os.path.join(d, f)
                allf.append((os.path.getmtime(p), p))
        if len(allf) > max_files:
            allf.sort()
            for _, p in allf[:len(allf) - max_files]:
                os.remove(p)
    except OSError:
        pass

