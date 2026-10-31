"""Borderless GDI source window. No browser, image codec or network transport."""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes as wt

from live_source import SourceFrames

_u = ctypes.WinDLL("user32", use_last_error=True)
_g = ctypes.WinDLL("gdi32", use_last_error=True)
_k = ctypes.WinDLL("kernel32", use_last_error=True)
_LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class _WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", _WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class _PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wt.HDC), ("fErase", wt.BOOL), ("rcPaint", wt.RECT),
                ("fRestore", wt.BOOL), ("fIncUpdate", wt.BOOL), ("rgbReserved", wt.BYTE * 32)]


def _signature(dll, name, result, *args):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = result, list(args)
    return fn


_signature(_k, "GetModuleHandleW", wt.HMODULE, wt.LPCWSTR)
_signature(_k, "OpenProcess", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
_signature(_k, "CloseHandle", wt.BOOL, wt.HANDLE)
_signature(_u, "DefWindowProcW", _LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
_signature(_u, "RegisterClassW", wt.ATOM, ctypes.POINTER(_WNDCLASS))
_signature(_u, "UnregisterClassW", wt.BOOL, wt.LPCWSTR, wt.HINSTANCE)
_signature(_u, "CreateWindowExW", wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p)
_signature(_u, "BeginPaint", wt.HDC, wt.HWND, ctypes.POINTER(_PAINTSTRUCT))
_signature(_u, "EndPaint", wt.BOOL, wt.HWND, ctypes.POINTER(_PAINTSTRUCT))
_signature(_u, "GetClientRect", wt.BOOL, wt.HWND, ctypes.POINTER(wt.RECT))
_signature(_u, "DestroyWindow", wt.BOOL, wt.HWND)
_signature(_u, "InvalidateRect", wt.BOOL, wt.HWND, ctypes.c_void_p, wt.BOOL)
_signature(_u, "UpdateWindow", wt.BOOL, wt.HWND)
_signature(_u, "ShowWindow", wt.BOOL, wt.HWND, ctypes.c_int)
_signature(_u, "PostQuitMessage", None, ctypes.c_int)
_signature(_u, "PeekMessageW", wt.BOOL, ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT)
_signature(_u, "TranslateMessage", wt.BOOL, ctypes.POINTER(wt.MSG))
_signature(_u, "DispatchMessageW", _LRESULT, ctypes.POINTER(wt.MSG))
_signature(_u, "MsgWaitForMultipleObjects", wt.DWORD, wt.DWORD, ctypes.POINTER(wt.HANDLE),
           wt.BOOL, wt.DWORD, wt.DWORD)
_signature(_g, "CreateCompatibleDC", wt.HDC, wt.HDC)
_signature(_g, "CreateDIBSection", wt.HBITMAP, wt.HDC, ctypes.POINTER(_BITMAPINFOHEADER),
           wt.UINT, ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD)
_signature(_g, "SelectObject", wt.HANDLE, wt.HDC, wt.HANDLE)
_signature(_g, "DeleteObject", wt.BOOL, wt.HANDLE)
_signature(_g, "DeleteDC", wt.BOOL, wt.HDC)
_signature(_g, "StretchBlt", wt.BOOL, wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
           ctypes.c_int, wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.DWORD)
_signature(_g, "GdiFlush", wt.BOOL)


def run(name, title, width, height, x, y, frame_width=1920, frame_height=1080):
    # Only this display subprocess is DPI-aware. The capture process must keep
    # its existing DPI mode for WGC stability. Otherwise GDI first shrinks the
    # 1080p DIB to logical pixels and Windows enlarges it again at 150% scaling.
    _signature(_u, "SetProcessDpiAwarenessContext", wt.BOOL, wt.HANDLE)(ctypes.c_void_p(-4))
    width, height = frame_width, frame_height
    frames = SourceFrames(frame_width, frame_height, name)
    dc = bitmap = old = hwnd = parent = None
    instance = _k.GetModuleHandleW(None)
    class_name = f"QianyinNativeSource-{os.getpid()}"
    registered = False
    sequence = 0
    # Keep the DPI behavior of the existing capture process: caller sizes in
    # logical pixels and verifies the final physical client area separately.
    try:
        dc = _g.CreateCompatibleDC(None)
        info = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), frame_width, -frame_height, 1, 32)
        bits = ctypes.c_void_p()
        bitmap = _g.CreateDIBSection(dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
        if not dc or not bitmap or not bits.value:
            raise ctypes.WinError(ctypes.get_last_error())
        old = _g.SelectObject(dc, bitmap)
        ctypes.memset(bits, 0, frame_width * frame_height * 4)

        @_WNDPROC
        def window_proc(window, message, wp, lp):
            if message == 0x000F:  # WM_PAINT
                paint, rect = _PAINTSTRUCT(), wt.RECT()
                target = _u.BeginPaint(window, ctypes.byref(paint))
                try:
                    _u.GetClientRect(window, ctypes.byref(rect))
                    _g.StretchBlt(target, 0, 0, rect.right, rect.bottom, dc,
                                  0, 0, frame_width, frame_height, 0x00CC0020)
                    # The DIB is reused on the next event: finish queued GDI
                    # reads before replacing its pixels (also when occluded).
                    _g.GdiFlush()
                finally:
                    _u.EndPaint(window, ctypes.byref(paint))
                return 0
            if message == 0x0014:  # WM_ERASEBKGND: full-frame paint, no flicker
                return 1
            if message == 0x0084:  # WM_NCHITTEST: drag the frameless canvas
                return 2  # HTCAPTION
            if message == 0x0002:  # WM_DESTROY
                _u.PostQuitMessage(0)
                return 0
            return _u.DefWindowProcW(window, message, wp, lp)

        cls = _WNDCLASS(0, window_proc, 0, 0, instance, None, None, None, None, class_name)
        if not _u.RegisterClassW(ctypes.byref(cls)):
            raise ctypes.WinError(ctypes.get_last_error())
        registered = True
        hwnd = _u.CreateWindowExW(0x00040000, class_name, title, 0x80000000,
                                  x, y, width, height, None, None, instance, None)
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        _u.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE: keep focus in the game
        parent = _k.OpenProcess(0x00100000, False, frames.owner_pid)
        if not parent:
            raise ctypes.WinError(ctypes.get_last_error())
        handles = (wt.HANDLE * 2)(frames.event, parent)
        msg = wt.MSG()
        while True:
            sequence_now, closed = frames.copy_into(bits.value, sequence)
            if closed:
                break
            if sequence_now > sequence:
                sequence = sequence_now
                _u.InvalidateRect(hwnd, None, False)
                _u.UpdateWindow(hwnd)  # paint even when covered by the game
            while _u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                if msg.message == 0x0012:  # WM_QUIT
                    return 0
                _u.TranslateMessage(ctypes.byref(msg))
                _u.DispatchMessageW(ctypes.byref(msg))
            result = _u.MsgWaitForMultipleObjects(2, handles, False, 1000, 0x04FF)
            if result == 1:  # producer exited, including a crash / forced kill
                break
            if result == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
        return 0
    finally:
        if hwnd:
            _u.DestroyWindow(hwnd)
        if registered:
            _u.UnregisterClassW(class_name, instance)
        if old:
            _g.SelectObject(dc, old)
        if bitmap:
            _g.DeleteObject(bitmap)
        if dc:
            _g.DeleteDC(dc)
        if parent:
            _k.CloseHandle(parent)
        frames.close()
