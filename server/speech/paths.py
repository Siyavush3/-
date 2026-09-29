"""Пути к данным для библиотек на C, которые не открывают файлы по пути с кириллицей."""
from __future__ import annotations

import ctypes
import os
import shutil
import tempfile
from pathlib import Path


def ascii_dir(path: Path, name: str = "espeak-ng-data") -> Path:
    """espeak-ng (внутри piper) и Kaldi (внутри Vosk) не открывают данные по пути с кириллицей — например,
    из папки «ЛЦТ». Путь заменяется коротким именем Windows (8.3), а если его нет — копией в
    ProgramData/arm112/<name> (у каждого набора данных своя папка)."""
    s = str(path)
    if s.isascii():
        return path
    if os.name == "nt":
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(s, buf, 1024) and buf.value.isascii():
            return Path(buf.value)
    base = Path(os.environ.get("PROGRAMDATA") or tempfile.gettempdir()) / "arm112" / name
    if not str(base).isascii():
        base = Path(os.path.abspath(os.sep)) / ("arm112-" + name)
    marker = base / ".source"
    if not marker.is_file() or marker.read_text(encoding="utf-8") != s:
        shutil.copytree(path, base, dirs_exist_ok=True)
        marker.write_text(s, encoding="utf-8")
    return base
