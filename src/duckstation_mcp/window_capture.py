from __future__ import annotations

import ctypes
import time
from pathlib import Path
from typing import Any


user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
gdiplus = ctypes.WinDLL("gdiplus", use_last_error=True)
ole32 = ctypes.OleDLL("ole32")


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", ctypes.c_uint32),
        ("DebugEventCallback", ctypes.c_void_p),
        ("SuppressBackgroundThread", ctypes.c_bool),
        ("SuppressExternalCodecs", ctypes.c_bool),
    ]


EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
EnumChildWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
user32.EnumWindows.argtypes = [EnumWindowsProc, ctypes.c_void_p]
user32.EnumWindows.restype = ctypes.c_bool
user32.EnumChildWindows.argtypes = [ctypes.c_void_p, EnumChildWindowsProc, ctypes.c_void_p]
user32.EnumChildWindows.restype = ctypes.c_bool
user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
user32.IsWindowVisible.restype = ctypes.c_bool
user32.IsIconic.argtypes = [ctypes.c_void_p]
user32.IsIconic.restype = ctypes.c_bool
user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = ctypes.c_bool
user32.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
user32.PrintWindow.restype = ctypes.c_bool
user32.GetWindowDC.argtypes = [ctypes.c_void_p]
user32.GetWindowDC.restype = ctypes.c_void_p
user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.ReleaseDC.restype = ctypes.c_int

gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteObject.restype = ctypes.c_bool
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.restype = ctypes.c_bool

gdiplus.GdiplusStartup.argtypes = [
    ctypes.POINTER(ctypes.c_ulonglong),
    ctypes.POINTER(GdiplusStartupInput),
    ctypes.c_void_p,
]
gdiplus.GdiplusStartup.restype = ctypes.c_uint32
gdiplus.GdiplusShutdown.argtypes = [ctypes.c_ulonglong]
gdiplus.GdiplusShutdown.restype = None
gdiplus.GdipCreateBitmapFromHBITMAP.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p),
]
gdiplus.GdipCreateBitmapFromHBITMAP.restype = ctypes.c_uint32
gdiplus.GdipSaveImageToFile.argtypes = [
    ctypes.c_void_p,
    ctypes.c_wchar_p,
    ctypes.POINTER(GUID),
    ctypes.c_void_p,
]
gdiplus.GdipSaveImageToFile.restype = ctypes.c_uint32
gdiplus.GdipDisposeImage.argtypes = [ctypes.c_void_p]
gdiplus.GdipDisposeImage.restype = ctypes.c_uint32
ole32.CLSIDFromString.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(GUID)]
ole32.CLSIDFromString.restype = ctypes.c_long

PW_RENDERFULLCONTENT = 0x00000002
PNG_ENCODER_CLSID = "{557CF406-1A04-11D3-9A73-0000F81EF32E}"


def _last_error(message: str) -> OSError:
    return OSError(ctypes.get_last_error(), message)


def _window_text(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(ctypes.c_void_p(hwnd), buffer, len(buffer))
    return buffer.value


def _class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(512)
    user32.GetClassNameW(ctypes.c_void_p(hwnd), buffer, len(buffer))
    return buffer.value


def _rect(hwnd: int) -> RECT:
    rect = RECT()
    if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
        raise _last_error(f"GetWindowRect failed for 0x{hwnd:X}")
    return rect


def list_windows(pid: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        window_pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(window_pid))
        if pid is not None and int(window_pid.value) != pid:
            return True
        if not user32.IsWindowVisible(ctypes.c_void_p(hwnd)):
            return True
        rect = RECT()
        if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
            return True
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return True
        rows.append(
            {
                "hwnd": f"0x{hwnd:X}",
                "pid": int(window_pid.value),
                "title": _window_text(hwnd),
                "class_name": _class_name(hwnd),
                "left": rect.left,
                "top": rect.top,
                "right": rect.right,
                "bottom": rect.bottom,
                "width": width,
                "height": height,
                "minimized": bool(user32.IsIconic(ctypes.c_void_p(hwnd))),
            }
        )
        return True

    if not user32.EnumWindows(EnumWindowsProc(callback), None):
        raise _last_error("EnumWindows failed")
    return rows


def find_window(pids: list[int]) -> dict[str, Any]:
    windows = [window for pid in pids for window in list_windows(pid)]
    if not windows:
        raise ValueError("no visible DuckStation window found")
    excluded = ("debugger", "디버거", "log", "로그", "console")

    def score(window: dict[str, Any]) -> tuple[bool, bool, int]:
        title = window["title"].lower()
        class_name = window["class_name"].lower()
        is_excluded = any(token in title for token in excluded)
        identifies_duckstation = "duckstation" in title or "duckstation" in class_name
        return is_excluded, not identifies_duckstation, -(window["width"] * window["height"])

    windows.sort(key=score)
    return windows[0]


def find_input_window(hwnd: int) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []

    def callback(child: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(ctypes.c_void_p(child)):
            return True
        rect = RECT()
        if not user32.GetWindowRect(ctypes.c_void_p(child), ctypes.byref(rect)):
            return True
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return True
        candidates.append(
            {
                "hwnd": f"0x{child:X}",
                "title": _window_text(child),
                "class_name": _class_name(child),
                "width": width,
                "height": height,
            }
        )
        return True

    user32.EnumChildWindows(ctypes.c_void_p(hwnd), EnumChildWindowsProc(callback), None)
    if not candidates:
        return {
            "hwnd": f"0x{hwnd:X}",
            "title": _window_text(hwnd),
            "class_name": _class_name(hwnd),
            "target": "top-level",
        }
    candidates.sort(key=lambda row: -(row["width"] * row["height"]))
    return {**candidates[0], "target": "largest-native-child"}


def _save_hbitmap_png(hbitmap: int, output_path: Path) -> None:
    token = ctypes.c_ulonglong()
    startup = GdiplusStartupInput(1, None, False, False)
    status = gdiplus.GdiplusStartup(ctypes.byref(token), ctypes.byref(startup), None)
    if status:
        raise RuntimeError(f"GdiplusStartup failed: {status}")
    bitmap = ctypes.c_void_p()
    try:
        status = gdiplus.GdipCreateBitmapFromHBITMAP(ctypes.c_void_p(hbitmap), None, ctypes.byref(bitmap))
        if status:
            raise RuntimeError(f"GdipCreateBitmapFromHBITMAP failed: {status}")
        clsid = GUID()
        if ole32.CLSIDFromString(PNG_ENCODER_CLSID, ctypes.byref(clsid)):
            raise RuntimeError("CLSIDFromString failed for PNG encoder")
        status = gdiplus.GdipSaveImageToFile(bitmap, str(output_path), ctypes.byref(clsid), None)
        if status:
            raise RuntimeError(f"GdipSaveImageToFile failed: {status}")
    finally:
        if bitmap:
            gdiplus.GdipDisposeImage(bitmap)
        gdiplus.GdiplusShutdown(token)


def capture_window(hwnd: int, output_path: Path) -> dict[str, Any]:
    if output_path.suffix.lower() != ".png":
        raise ValueError("inactive window capture supports PNG output only")
    rect = _rect(hwnd)
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise ValueError(f"invalid window size {width}x{height}")

    window_dc = user32.GetWindowDC(ctypes.c_void_p(hwnd))
    if not window_dc:
        raise _last_error("GetWindowDC failed")
    mem_dc = None
    hbitmap = None
    old_object = None
    try:
        mem_dc = gdi32.CreateCompatibleDC(window_dc)
        if not mem_dc:
            raise _last_error("CreateCompatibleDC failed")
        hbitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
        if not hbitmap:
            raise _last_error("CreateCompatibleBitmap failed")
        old_object = gdi32.SelectObject(mem_dc, hbitmap)
        if not old_object:
            raise _last_error("SelectObject failed")
        if not user32.PrintWindow(ctypes.c_void_p(hwnd), mem_dc, PW_RENDERFULLCONTENT):
            raise _last_error("PrintWindow failed")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _save_hbitmap_png(hbitmap, output_path)
    finally:
        if old_object and mem_dc:
            gdi32.SelectObject(mem_dc, old_object)
        if hbitmap:
            gdi32.DeleteObject(hbitmap)
        if mem_dc:
            gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(ctypes.c_void_p(hwnd), window_dc)

    return {
        "path": str(output_path),
        "hwnd": f"0x{hwnd:X}",
        "width": width,
        "height": height,
        "minimized": bool(user32.IsIconic(ctypes.c_void_p(hwnd))),
        "method": "PrintWindow(PW_RENDERFULLCONTENT)",
        "foreground_activation": False,
        "file_size": output_path.stat().st_size,
    }


def default_output_path() -> Path:
    root = Path(__file__).resolve().parents[2]
    return root / "logs" / "screenshots" / f"duckstation-{time.strftime('%Y%m%d-%H%M%S')}.png"
