"""Пути к данным проекта. Переопределяются переменной окружения A112_DATA."""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    return Path(os.environ.get("A112_DATA", REPO_ROOT / "data"))
