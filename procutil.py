import ctypes
import os
import subprocess
from ctypes import wintypes

CREATE_NO_WINDOW = 0x08000000

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateJobObjectW.restype = wintypes.HANDLE
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))


class _BASIC(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
    ]


class _IO(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _EXT(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BASIC), ("IoInfo", _IO), ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_job = None


def _get_job():
    global _job
    if _job is None:
        job = _k32.CreateJobObjectW(None, None)
        info = _EXT()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        _k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        _job = job
    return _job


def bind_to_app(pid):
    """Child dies automatically when this app exits (even if the console window is closed)."""
    h = _k32.OpenProcess(0x0100 | 0x0001, False, pid)
    if h:
        _k32.AssignProcessToJobObject(_get_job(), h)
        _k32.CloseHandle(h)


class _PROCESS_MEMORY(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in (
            "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
            "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
            "PagefileUsage", "PeakPagefileUsage", "PrivateUsage")
    ]


_k32.K32GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(_PROCESS_MEMORY), wintypes.DWORD)


def private_commit_bytes(pid, *, require_app_job=False):
    """Windows Private Bytes. Venv 的实际服务进程须属于本应用的 Job。"""
    handle = _k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        if require_app_job:
            member = wintypes.BOOL()
            if not _job or not _k32.IsProcessInJob(handle, _job, ctypes.byref(member)) or not member.value:
                return None
        counters = _PROCESS_MEMORY(cb=ctypes.sizeof(_PROCESS_MEMORY))
        if _k32.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return counters.PrivateUsage
        return None
    finally:
        _k32.CloseHandle(wintypes.HANDLE(handle))


def clean_env(extra=None):
    """Strip variables inherited from a Qoder host so qodercli runs as a normal standalone CLI."""
    env = {k: v for k, v in os.environ.items()
           if not (k.upper().startswith("QODER") and k.upper() != "QODER_PERSONAL_ACCESS_TOKEN")
           and k.upper() != "ELECTRON_RUN_AS_NODE"}
    env.update(extra or {})
    return env


def kill_tree(pid):
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW)
