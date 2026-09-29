"""Ограничение ресурсов процесса озвучки: ядра, память, приоритет.

Озвучка идёт фоном на той же машине, что и занятия, поэтому ей выделяется немного: одно ядро,
пониженный приоритет и потолок памяти. На Windows — объект задания (Job Object): ограничение
держит ядро ОС, обойти его изнутри процесса нельзя. На Linux — привязка к ядрам, nice и RLIMIT_AS.
"""
from __future__ import annotations

import os
from typing import Optional

PROFILES = {
    # сервер класса: занятия важнее, озвучка берёт одно ядро и ждёт, пока процессор свободен
    "background": {"cpus": 1, "threads": 1, "memory_mb": 1024, "low_priority": True},
    # демо-стенд: ноутбук на i5 8–10 поколения — 4 ядра, 8 ГБ, на нём же сервер и браузеры;
    # озвучке из этого — один поток и 1 ГБ памяти
    "demo": {"cpus": 4, "threads": 1, "memory_mb": 1024, "low_priority": True},
    # установка или сборка дистрибутива: занятий нет, можно все 4 ядра
    "install": {"cpus": 4, "threads": 4, "memory_mb": 2048, "low_priority": False},
}

_job = None  # объект задания должен жить, пока жив процесс


def apply(cpus: Optional[int] = None, memory_mb: Optional[int] = None, low_priority: bool = True) -> dict:
    """Ограничить текущий процесс. Возвращает то, что реально применено."""
    if os.name == "nt":
        return _windows(cpus, memory_mb, low_priority)
    return _posix(cpus, memory_mb, low_priority)


def _windows(cpus, memory_mb, low_priority) -> dict:
    import ctypes
    from ctypes import wintypes
    global _job

    class IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                      "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.GetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]

    proc_mask, sys_mask = ctypes.c_size_t(), ctypes.c_size_t()
    k32.GetProcessAffinityMask(k32.GetCurrentProcess(), ctypes.byref(proc_mask), ctypes.byref(sys_mask))
    available = [i for i in range(64) if proc_mask.value >> i & 1]
    info = ExtendedLimit()
    flags = 0
    applied = {"platform": "windows", "cpus_total": len(available)}
    if cpus:
        chosen = available[:max(1, int(cpus))]
        info.BasicLimitInformation.Affinity = sum(1 << i for i in chosen)
        flags |= 0x10                                        # JOB_OBJECT_LIMIT_AFFINITY
        applied["cpus"] = len(chosen)
    if low_priority:
        info.BasicLimitInformation.PriorityClass = 0x4000    # BELOW_NORMAL_PRIORITY_CLASS
        flags |= 0x20                                        # JOB_OBJECT_LIMIT_PRIORITY_CLASS
        applied["priority"] = "ниже обычного"
    if memory_mb:
        info.ProcessMemoryLimit = int(memory_mb) * 2 ** 20
        flags |= 0x100                                       # JOB_OBJECT_LIMIT_PROCESS_MEMORY
        applied["memory_mb"] = int(memory_mb)
    info.BasicLimitInformation.LimitFlags = flags
    job = k32.CreateJobObjectW(None, None)
    if not job or not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)) \
            or not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
        raise OSError(ctypes.get_last_error(), "не удалось ограничить ресурсы процесса озвучки")
    _job = job
    return applied


def _posix(cpus, memory_mb, low_priority) -> dict:
    applied = {"platform": os.name}
    if cpus and hasattr(os, "sched_setaffinity"):
        have = sorted(os.sched_getaffinity(0))
        os.sched_setaffinity(0, set(have[:max(1, int(cpus))]))
        applied.update(cpus=min(int(cpus), len(have)), cpus_total=len(have))
    if low_priority:
        os.nice(10)
        applied["priority"] = "nice 10"
    if memory_mb:
        import resource
        limit = int(memory_mb) * 2 * 2 ** 20  # виртуальной памяти процессу нужно примерно вдвое больше резидентной
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        applied["memory_mb"] = int(memory_mb)
    return applied


def peak_memory_mb() -> Optional[float]:
    """Пик памяти процесса, МБ (для отчёта процесса озвучки)."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + \
                           [(n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                                           "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                                           "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
            c = Counters()
            c.cb = ctypes.sizeof(c)
            psapi = ctypes.WinDLL("psapi")
            k32 = ctypes.WinDLL("kernel32")
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
            psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb)
            return round(c.PeakWorkingSetSize / 2 ** 20, 1)
        import resource
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:
        return None
