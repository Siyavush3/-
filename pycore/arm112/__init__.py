"""arm112 — Python-сторона ядра тренажёра АРМ-112.

- ``arm112.reference`` — эталонные («золотые») модели на чистом Python.
  Задают семантику; C-ядро обязано совпадать с ними на тех же входах.
- ``arm112.ekp`` — маршрутизация через C-ядро ``libarm112`` (ctypes).
"""

__version__ = "0.1.0"


def core_info():
    """Состояние C-ядра для диагностики (например, /api/health).

    Не бросает исключений: сервер должен уметь сообщить, что ядро не найдено.
    """
    try:
        from arm112 import _native
        lib = _native.load()
    except OSError as exc:
        return {"loaded": False, "error": str(exc)}
    return {
        "loaded": True,
        "version": _native.version(lib),
        "cpu": _native.cpu_features(lib),
        "path": getattr(lib, "_name", None),
    }
