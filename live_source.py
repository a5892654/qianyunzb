"""Latest-frame BGRA mailbox for the native source window (Windows only).

One bounded shared buffer, one mutex and one auto-reset event. A slow consumer
never queues frames or blocks the capture thread; the next frame replaces it.
"""
from __future__ import annotations

import ctypes
import mmap
import os
import struct
import uuid
from ctypes import wintypes

_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_kernel.CreateMutexW.restype = wintypes.HANDLE
_kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_kernel.CreateEventW.restype = wintypes.HANDLE
_kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_kernel.OpenMutexW.restype = wintypes.HANDLE
_kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_kernel.OpenEventW.restype = wintypes.HANDLE
_kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel.WaitForSingleObject.restype = wintypes.DWORD
for _name in ("ReleaseMutex", "SetEvent", "CloseHandle"):
    getattr(_kernel, _name).argtypes = [wintypes.HANDLE]
    getattr(_kernel, _name).restype = wintypes.BOOL

_HEADER = struct.Struct("<4sIIIIQ")  # magic, width, height, closed, owner pid, sequence
_PIXELS = 64
_MAGIC = b"QYF1"


class SourceFrames:
    def __init__(self, width: int, height: int, name: str | None = None):
        if not 1 <= width <= 8192 or not 1 <= height <= 8192:
            raise ValueError("Invalid source frame dimensions")
        self.width, self.height = width, height
        self.owner = name is None
        self.name = name or ("Local\\QianyinFrames-" + uuid.uuid4().hex)
        self.size = width * height * 4
        self.sequence = 0
        self.owner_pid = os.getpid()
        self.map = self.mutex = self.event = None
        try:
            if self.owner:
                self.mutex = _kernel.CreateMutexW(None, False, self.name + "-lock")
                self.event = _kernel.CreateEventW(None, False, False, self.name + "-ready")
            else:
                self.mutex = _kernel.OpenMutexW(0x00100001, False, self.name + "-lock")
                self.event = _kernel.OpenEventW(0x00100000, False, self.name + "-ready")
            if not self.mutex or not self.event:
                raise ctypes.WinError(ctypes.get_last_error())
            self.map = mmap.mmap(-1, _PIXELS + self.size, tagname=self.name)
            self.address = ctypes.addressof(ctypes.c_ubyte.from_buffer(self.map)) + _PIXELS
            if self.owner:
                _HEADER.pack_into(self.map, 0, _MAGIC, width, height, 0, self.owner_pid, 0)
            elif _HEADER.unpack_from(self.map)[:3] != (_MAGIC, width, height):
                raise ValueError("Source frame format mismatch")
            else:
                self.owner_pid = _HEADER.unpack_from(self.map)[4]
        except BaseException:
            self.close()
            raise

    def _acquire(self, timeout=0):
        result = _kernel.WaitForSingleObject(self.mutex, timeout)
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        return result in (0, 0x80)  # WAIT_OBJECT_0 / WAIT_ABANDONED

    def publish(self, data: bytes) -> bool:
        if len(data) != self.size:
            raise ValueError("Source frame size mismatch")
        if self.map is None or not self._acquire():
            return False
        try:
            ctypes.memmove(self.address, data, self.size)
            self.sequence += 1
            _HEADER.pack_into(self.map, 0, _MAGIC, self.width, self.height, 0, self.owner_pid, self.sequence)
        finally:
            _kernel.ReleaseMutex(self.mutex)
        _kernel.SetEvent(self.event)
        return True

    def copy_into(self, address: int, after_sequence: int) -> tuple[int, bool]:
        """Copy a consistent latest frame to a caller-owned DIB; never wait."""
        if not self._acquire():
            return after_sequence, False
        try:
            _, _, _, closed, _, sequence = _HEADER.unpack_from(self.map)
            if not closed and sequence > after_sequence:
                ctypes.memmove(address, self.address, self.size)
            return sequence, bool(closed)
        finally:
            _kernel.ReleaseMutex(self.mutex)

    def wait(self, timeout_ms=500):
        return _kernel.WaitForSingleObject(self.event, timeout_ms) == 0

    def close(self):
        if self.map is not None:
            if self.owner and self.mutex and self._acquire(100):
                try:
                    _HEADER.pack_into(self.map, 0, _MAGIC, self.width, self.height, 1, self.owner_pid, self.sequence)
                finally:
                    _kernel.ReleaseMutex(self.mutex)
                if self.event:
                    _kernel.SetEvent(self.event)
            self.map.close()
            self.map = None
        for name in ("mutex", "event"):
            handle = getattr(self, name)
            if handle:
                _kernel.CloseHandle(handle)
                setattr(self, name, None)
