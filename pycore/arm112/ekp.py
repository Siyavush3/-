"""Маршрутизация через C-ядро (модуль ekp libarm112).

Возвращает тот же тип, что эталон (arm112.reference.routing.Route), — результаты
можно сравнивать напрямую; тесты требуют полного совпадения.
"""
import ctypes
from typing import FrozenSet, Iterable, List

from arm112 import _native
from arm112.reference.routing import Route


class EkpError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__("%s (код %d)" % (message, code))
        self.code = code


class Ekp:
    """Открытая таблица classifier.ekp. Потокобезопасна: после открытия только читается."""

    def __init__(self, path):
        self._lib = _native.load()
        handle = ctypes.c_void_p()
        # Путь передаётся в UTF-8: ядро само переводит его в UTF-16 на Windows.
        rc = self._lib.a112_ekp_open(str(path).encode("utf-8"), ctypes.byref(handle))
        if rc != _native.A112_OK:
            raise EkpError(rc, _native.strerror(self._lib, rc))
        self._handle = handle
        lib, h = self._lib, handle
        self.source_version: str = lib.a112_ekp_source_version(h).decode("utf-8")
        self.flags: List[str] = [lib.a112_ekp_flag_name(h, i).decode("utf-8")
                                 for i in range(lib.a112_ekp_flag_count(h))]
        self.services: List[str] = [lib.a112_ekp_service_name(h, i).decode("utf-8")
                                    for i in range(lib.a112_ekp_service_count(h))]
        self.territorial: List[str] = [lib.a112_ekp_territorial_name(h, i).decode("utf-8")
                                       for i in range(lib.a112_ekp_territorial_count(h))]
        self._flag_bit = {name: i for i, name in enumerate(self.flags)}

    def codes(self) -> List[str]:
        lib, h = self._lib, self._handle
        return [str(lib.a112_ekp_incident_code(h, i)) for i in range(lib.a112_ekp_incident_count(h))]

    def route(self, code, flags: Iterable[str] = ()) -> Route:
        mask = 0
        for name in flags:
            if name not in self._flag_bit:
                raise ValueError("неизвестный флаг: %s" % name)
            mask |= 1 << self._flag_bit[name]
        out = _native.Route()
        rc = self._lib.a112_ekp_route(self._handle, int(code), mask, ctypes.byref(out))
        if rc == _native.A112_ERR_NOT_FOUND:
            raise KeyError(code)
        if rc != _native.A112_OK:
            raise EkpError(rc, _native.strerror(self._lib, rc))
        services: FrozenSet[str] = frozenset(
            name for i, name in enumerate(self.services) if (out.services[i >> 6] >> (i & 63)) & 1)
        territorial: FrozenSet[str] = frozenset(
            name for i, name in enumerate(self.territorial) if (out.territorial >> i) & 1)
        return Route(services, territorial)

    def close(self):
        if getattr(self, "_handle", None):
            self._lib.a112_ekp_close(self._handle)
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        self.close()
