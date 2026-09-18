# icons.py - pull the BIG icon out of a program.
#
# The obvious way (System.Drawing.Icon.ExtractAssociatedIcon) only ever
# returns 32x32, which is why the app tiles looked terrible blown up to 40px
# on a retina phone. Windows actually keeps a 256x256 "jumbo" version of every
# shell icon; you get at it through the shell image list, not the file.
#
#   SHGetFileInfo(path, SHGFI_SYSICONINDEX)  -> icon index
#   SHGetImageList(SHIL_JUMBO)               -> the 256px image list
#   IImageList::GetIcon(index)               -> HICON
#   DrawIconEx onto a 32-bit DIB             -> BGRA pixels (alpha intact)
#
# Falls back down the sizes if a program has no jumbo icon.

import ctypes
import os
from ctypes import POINTER, Structure, byref, c_int, c_void_p, c_wchar, wintypes

shell32 = ctypes.windll.shell32
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
ole32 = ctypes.windll.ole32

SHGFI_SYSICONINDEX = 0x4000
SHGFI_USEFILEATTRIBUTES = 0x10

SHIL_JUMBO = 0x4      # 256
SHIL_EXTRALARGE = 0x2  # 48
SHIL_LARGE = 0x0      # 32

SIZES = [(SHIL_JUMBO, 256), (SHIL_EXTRALARGE, 48), (SHIL_LARGE, 32)]

DI_NORMAL = 0x0003
BI_RGB = 0
DIB_RGB_COLORS = 0


class SHFILEINFO(Structure):
    _fields_ = [("hIcon", c_void_p), ("iIcon", c_int), ("dwAttributes", wintypes.DWORD),
                ("szDisplayName", c_wchar * 260), ("szTypeName", c_wchar * 80)]


class GUID(Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, s):
        super().__init__()
        ole32.CLSIDFromString(ctypes.c_wchar_p(s), byref(self))


IID_IImageList = GUID("{46EB5926-582E-4017-9FDF-E8998DAA0950}")


class BITMAPINFOHEADER(Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _icon_index(path):
    info = SHFILEINFO()
    flags = SHGFI_SYSICONINDEX
    if not os.path.exists(path):
        flags |= SHGFI_USEFILEATTRIBUTES
    r = shell32.SHGetFileInfoW(ctypes.c_wchar_p(path), 0, byref(info),
                               ctypes.sizeof(info), flags)
    return info.iIcon if r else None


def _hicon(index, shil):
    lst = c_void_p()
    hr = shell32.SHGetImageList(shil, byref(IID_IImageList), byref(lst))
    if hr != 0 or not lst:
        return None, None
    # IImageList vtable: 0-2 IUnknown, 3 Add, 4 ReplaceIcon, 5 SetOverlayImage,
    # 6 Replace, 7 AddMasked, 8 Draw, 9 Remove, 10 GetIcon. Slot 24 is
    # DragLeave - calling that returns E_FAIL and silently yields no icon,
    # which is exactly how this failed the first time.
    vtbl = ctypes.cast(lst, POINTER(POINTER(c_void_p)))[0]
    GetIcon = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, c_int, ctypes.c_uint,
                                 POINTER(c_void_p))(vtbl[10])
    hicon = c_void_p()
    if GetIcon(lst, index, DI_NORMAL, byref(hicon)) != 0 or not hicon:
        return None, lst
    return hicon, lst


def _hicon_to_rgba(hicon, size):
    """Draw the icon onto a 32bpp DIB so the alpha channel survives."""
    hdc = user32.GetDC(0)
    memdc = gdi32.CreateCompatibleDC(hdc)

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = size
    bmi.bmiHeader.biHeight = -size          # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB

    bits = c_void_p()
    dib = gdi32.CreateDIBSection(memdc, byref(bmi), DIB_RGB_COLORS,
                                 byref(bits), None, 0)
    if not dib:
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(0, hdc)
        return None

    old = gdi32.SelectObject(memdc, dib)
    user32.DrawIconEx(memdc, 0, 0, hicon, size, size, 0, None, DI_NORMAL)
    gdi32.SelectObject(memdc, old)

    buf = ctypes.string_at(bits, size * size * 4)

    gdi32.DeleteObject(dib)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(0, hdc)
    return buf


def extract(path, out_png, min_size=48):
    """Write the largest available icon for `path` to out_png. True on success."""
    try:
        from PIL import Image
    except ImportError:
        return False

    ole32.CoInitialize(None)      # the shell image list is COM

    idx = _icon_index(path)
    if idx is None:
        return False

    for shil, size in SIZES:
        if size < min_size:
            break
        hicon, lst = _hicon(idx, shil)
        if not hicon:
            continue
        try:
            buf = _hicon_to_rgba(hicon, size)
            if not buf:
                continue
            img = Image.frombuffer("RGBA", (size, size), buf, "raw", "BGRA", 0, 1)

            # A jumbo slot often holds a small icon padded into a big canvas.
            # Crop to what is actually drawn so it does not render tiny.
            box = img.getbbox()
            if box:
                w, h = box[2] - box[0], box[3] - box[1]
                if w < size * 0.35 and shil == SHIL_JUMBO:
                    # Genuinely a low-res icon - let the next size down win.
                    continue
                img = img.crop(box)

            if img.getbbox() is None:
                continue
            img.save(out_png, "PNG")
            return True
        finally:
            user32.DestroyIcon(hicon)

    return False


if __name__ == "__main__":
    import sys
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else "icon.png"
    print("ok" if extract(src, dst) else "failed")
