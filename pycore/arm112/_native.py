"""Загрузка libarm112 через ctypes.

ctypes.CDLL отпускает GIL на время вызова функции библиотеки, так что вызовы ядра
из разных потоков идут параллельно (ARCHITECTURE.md §12).

Где ищется библиотека: переменная окружения A112_LIB, иначе build/bin в корне репозитория.
"""
import ctypes
import os
from pathlib import Path

from arm112.paths import REPO_ROOT

LIB_NAMES = ("arm112.dll", "libarm112.dll", "libarm112.so", "libarm112.dylib")

CPU_FLAGS = (("sse4.2", 1 << 0), ("avx2", 1 << 1), ("fma", 1 << 2), ("avx512f", 1 << 3))

A112_OK = 0
A112_ERR_NOT_FOUND = -4


class Route(ctypes.Structure):
    """Зеркало struct a112_route из arm112.h."""
    _fields_ = [
        ("services", ctypes.c_uint64 * 4),
        ("territorial", ctypes.c_uint32),
        ("n_services", ctypes.c_uint32),
    ]


_lib = None


def _candidates():
    env = os.environ.get("A112_LIB")
    if env:
        return [Path(env)]
    return [REPO_ROOT / "build" / "bin" / name for name in LIB_NAMES]


def _declare(lib):
    h, u32, cstr = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p
    signatures = {
        "a112_version": ([], cstr),
        "a112_strerror": ([ctypes.c_int], cstr),
        "a112_cpu_features": ([], u32),
        "a112_ekp_open": ([cstr, ctypes.POINTER(h)], ctypes.c_int),
        "a112_ekp_close": ([h], None),
        "a112_ekp_source_version": ([h], cstr),
        "a112_ekp_flag_count": ([h], u32),
        "a112_ekp_flag_name": ([h, u32], cstr),
        "a112_ekp_service_count": ([h], u32),
        "a112_ekp_service_name": ([h, u32], cstr),
        "a112_ekp_territorial_count": ([h], u32),
        "a112_ekp_territorial_name": ([h, u32], cstr),
        "a112_ekp_incident_count": ([h], u32),
        "a112_ekp_incident_code": ([h, u32], u32),
        "a112_ekp_route": ([h, u32, u32, ctypes.POINTER(Route)], ctypes.c_int),
    }
    for name, (args, res) in signatures.items():
        fn = getattr(lib, name)
        fn.argtypes = args
        fn.restype = res


def load():
    """Загружает библиотеку один раз на процесс. OSError — если её нет."""
    global _lib
    if _lib is not None:
        return _lib
    tried = []
    for path in _candidates():
        if path.is_file():
            lib = ctypes.CDLL(str(path))
            _declare(lib)
            _lib = lib
            return lib
        tried.append(str(path))
    raise OSError("libarm112 не найдена (соберите ядро через CMake или задайте A112_LIB); "
                  "проверены: " + ", ".join(tried))


def version(lib) -> str:
    return lib.a112_version().decode("utf-8")


def strerror(lib, code: int) -> str:
    return lib.a112_strerror(code).decode("utf-8")


def cpu_features(lib) -> list:
    mask = lib.a112_cpu_features()
    return [name for name, bit in CPU_FLAGS if mask & bit]
