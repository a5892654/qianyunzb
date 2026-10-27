"""说话时把别的程序（主要是游戏）的声音压低，说完还原。

鲸语的混音只管自己这条输出流，压不动另一个进程的声音，所以走 Windows 的音频会话接口，
按 PID 找到游戏的会话直接改主音量。COM 指针不能跨线程使，因此所有调用都交给一个专用线程做。
"""
import logging
import queue
import threading

log = logging.getLogger("duck")

_q = None
_thread = None
_orig = {}
_start_lock = threading.Lock()


def _loop(q):
    import comtypes
    from pycaw.pycaw import AudioUtilities
    comtypes.CoInitialize()
    while True:
        job = q.get()
        if job is None:
            break
        op, pids, level = job
        if not pids:
            continue
        try:
            for s in AudioUtilities.GetAllSessions():
                if s.ProcessId not in pids:
                    continue
                vol = s.SimpleAudioVolume
                if op == "duck":
                    _orig.setdefault(s.ProcessId, vol.GetMasterVolume())
                    vol.SetMasterVolume(level, None)
                else:
                    if s.ProcessId in _orig:
                        vol.SetMasterVolume(_orig.pop(s.ProcessId), None)
        except Exception as e:
            # 压音失败不该影响说话，最多就是游戏声还是那么大
            log.debug("音频会话操作失败：%s", e)


def _send(op, pids, level=0.0):
    global _q, _thread
    if not pids:
        return
    with _start_lock:
        if _thread is None or not _thread.is_alive():
            _q = queue.Queue()
            _thread = threading.Thread(target=_loop, args=(_q,), daemon=True, name="duckaudio")
            _thread.start()
    _q.put((op, set(pids), level))


def duck(pids, level):
    _send("duck", pids, level)


def restore(pids):
    _send("restore", pids)
