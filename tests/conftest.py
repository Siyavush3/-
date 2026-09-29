import os
import tempfile

import pytest

# Тесты не запускают фоновую озвучку и не пишут в var/tts: у каждого прогона свой каталог звука.
os.environ.setdefault("A112_TTS", "off")
os.environ.setdefault("A112_ASR", "off")  # модель распознавания не грузится заранее; тесты речи подключают её сами
os.environ.setdefault("A112_TTS_DIR", tempfile.mkdtemp(prefix="a112-tts-"))
os.environ.setdefault("A112_VAR", tempfile.mkdtemp(prefix="a112-var-"))  # журнал, резервные копии, эталон целостности

import compile_ekp
from arm112.paths import data_dir
from arm112.reference.routing import Classifier


def _read_json(path):
    import json
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="session")
def classifier():
    return Classifier.load()


@pytest.fixture(scope="session")
def ekp_table(tmp_path_factory):
    """Таблица ядра, собранная из текущих данных — тесты не зависят от CMake-шага."""
    base = data_dir() / "classifier"
    blob = compile_ekp.build(_read_json(base / "classifier.json"), _read_json(base / "ui_services.json"))
    path = tmp_path_factory.mktemp("ekp") / "classifier.ekp"
    path.write_bytes(blob)
    return path


@pytest.fixture(scope="session")
def native():
    """C-ядро. Локально без сборки тесты ядра пропускаются, в CI (A112_REQUIRE_NATIVE=1) — падают."""
    from arm112 import _native
    try:
        return _native.load()
    except OSError as exc:
        if os.environ.get("A112_REQUIRE_NATIVE") == "1":
            pytest.fail(str(exc))
        pytest.skip(str(exc))


@pytest.fixture(scope="session")
def ekp(native, ekp_table):
    from arm112.ekp import Ekp
    table = Ekp(ekp_table)
    yield table
    table.close()
