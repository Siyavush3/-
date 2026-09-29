"""Список сторонних библиотек и компонентов с версиями и лицензиями (поставка, узел 8).

    python tools/licenses.py            → docs/LIBRARIES.md

Python-пакеты берутся из requirements/*.txt вместе со всеми зависимостями — по метаданным установленных
пакетов (importlib.metadata). Прочие компоненты (голоса, C-ядро, СУБД, браузеры) описаны ниже вручную.
"""
import re
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PURPOSE = {
    "fastapi": "веб-сервер: REST API", "starlette": "основа FastAPI: маршрутизация, статика, WebSocket",
    "pydantic": "проверка входных данных API", "pydantic-core": "ядро pydantic", "uvicorn": "HTTP/WebSocket-сервер",
    "websockets": "протокол WebSocket для uvicorn", "psycopg": "драйвер PostgreSQL", "psycopg-binary": "драйвер PostgreSQL (сборка)",
    "pymorphy3": "морфология русского языка: орфография и словоформы", "pymorphy3-dicts-ru": "словарь OpenCorpora для pymorphy3",
    "dawg2-python": "словарь pymorphy3", "dawg-python": "словарь pymorphy3", "docopt-ng": "зависимость pymorphy3",
    "openpyxl": "выгрузка отчётов в Excel", "et-xmlfile": "зависимость openpyxl", "psutil": "мониторинг нагрузки",
    "piper-tts": "синтез речи (отдельный процесс озвучки)", "onnxruntime": "выполнение нейросети голоса на процессоре",
    "numpy": "обработка звука", "flatbuffers": "зависимость onnxruntime", "protobuf": "зависимость onnxruntime",
    "coloredlogs": "зависимость onnxruntime", "humanfriendly": "зависимость onnxruntime", "sympy": "зависимость onnxruntime",
    "mpmath": "зависимость sympy", "packaging": "зависимость onnxruntime", "h11": "HTTP для uvicorn", "click": "командная строка uvicorn",
    "anyio": "асинхронный ввод-вывод Starlette", "idna": "зависимость anyio", "sniffio": "зависимость anyio",
    "typing-extensions": "совместимость типов", "annotated-types": "зависимость pydantic", "typing-inspection": "зависимость pydantic",
    "python-docx": "сборка документации .docx (tools/build_docs.py)", "lxml": "зависимость python-docx",
    "vosk": "распознавание речи оператора (Kaldi)", "cffi": "зависимость vosk", "pycparser": "зависимость cffi",
    "requests": "зависимость vosk (загрузка моделей, не используется)", "urllib3": "зависимость requests",
    "charset-normalizer": "зависимость requests", "certifi": "зависимость requests", "tqdm": "зависимость vosk",
    "srt": "зависимость vosk (субтитры, не используется)",
    "colorama": "цвет в консоли (Windows)", "pyreadline3": "зависимость humanfriendly (Windows)", "tzdata": "часовые пояса (Windows)",
}


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


ORDER = ["server.txt", "tts.txt", "asr.txt", "toolchain.txt", "dev.txt"]  # пакет относится к первому файлу, где встретился


def requirements():
    names = []
    files = sorted((ROOT / "requirements").glob("*.txt"), key=lambda f: ORDER.index(f.name) if f.name in ORDER else 99)
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line.startswith("-"):
                continue  # «-r server.txt» и прочие ключи pip
            m = re.match(r"([A-Za-z0-9_.-]+)", line)
            if m:
                names.append((m.group(1), f.name))
    return names


def license_of(dist) -> str:
    md = dist.metadata
    lic = (md.get("License-Expression") or "").strip()
    if not lic:
        raw = (md.get("License") or "").strip()
        lic = raw.splitlines()[0][:60] if raw and len(raw) < 200 else ""
    classes = [c.split("::")[-1].strip() for c in md.get_all("Classifier") or [] if c.startswith("License ::")]
    if classes and (not lic or lic.upper() in ("UNKNOWN", "")):
        lic = ", ".join(dict.fromkeys(classes))
    return lic or "см. пакет"


def walk(roots):
    """Все пакеты с зависимостями. Файлы требований обходятся по очереди (ORDER), каждый — целиком:
    зависимость относится к первому файлу, которому она нужна (idna нужна серверу раньше, чем vosk)."""
    from collections import deque
    from packaging.requirements import Requirement
    seen = {}
    for source in dict.fromkeys(s for _, s in roots):
        stack = deque((n, s) for n, s in roots if s == source)
        while stack:
            name, _ = stack.popleft()
            key = norm(name)
            if key in seen:
                continue
            try:
                dist = metadata.distribution(name)
            except metadata.PackageNotFoundError:
                seen[key] = (name, None, source)
                continue
            seen[key] = (dist.metadata["Name"], dist, source)
            for req in dist.requires or []:
                r = Requirement(req)
                if r.marker and not r.marker.evaluate({"extra": ""}):
                    continue  # зависимость для другой платформы, версии Python или необязательной возможности
                stack.append((r.name, source))
    return seen


def main() -> int:
    rows = []
    for key, (name, dist, source) in sorted(walk(requirements()).items()):
        if dist is None:
            rows.append("| %s | — | не установлен | %s | %s |" % (name, source, PURPOSE.get(key, "")))
            continue
        rows.append("| %s | %s | %s | %s | %s |" % (name, dist.version, license_of(dist), source, PURPOSE.get(key, "зависимость")))
    text = """# Сторонние библиотеки и компоненты

Список Python-пакетов собран автоматически по метаданным установленных пакетов: `python tools/licenses.py`.
`server.txt` — сервер тренажёра, `tts.txt` — необязательная озвучка (отдельный процесс), `asr.txt` —
необязательное распознавание речи оператора,
`toolchain.txt` — сборка C-ядра, `dev.txt` — тесты и сборка документации; последние два в поставку
не входят.

## Python-пакеты

| Пакет | Версия | Лицензия | Файл зависимостей | Назначение |
|---|---|---|---|---|
%s

## Прочие компоненты

| Компонент | Лицензия | Как используется |
|---|---|---|
| Python 3.11 | PSF License | среда выполнения сервера; в `ARM112-Setup.exe` — встроенный Python 3.11.9 (python.org) |
| Inno Setup 6 | Inno Setup License (бесплатно, в том числе для коммерческого использования) | сборка установщика `ARM112-Setup.exe`; в поставке — только его программа установки |
| Microsoft Visual C++ Redistributable 2015–2022 (x64) | лицензия Microsoft на распространяемые компоненты | внутри `ARM112-Setup.exe`; ставится, только если в системе нет `msvcp140.dll` (нужна onnxruntime) |
| SQLite | общественное достояние | база данных по умолчанию (входит в Python) |
| PostgreSQL ≥ 12 | PostgreSQL License | база данных учебного класса (необязательно) |
| C-ядро `libarm112` | собственная разработка | маршрутизация по классификатору и справочнику служб; без него — эталон на Python |
| Веб-интерфейс (HTML, CSS, JS) | собственная разработка | без внешних библиотек и шрифтов: контур изолирован |
| espeak-ng (внутри piper-tts) | GPL-3.0 | фонемизация для синтеза речи; работает только в отдельном процессе озвучки |
| Голос `ru_RU-dmitri-medium` (piper) | данные: CC0; базовая модель lessac — исследовательская лицензия Blizzard 2013 | заявители (мужской голос) |
| Голос `ru_RU-ruslan-medium` (piper) | данные: CC BY-NC-SA 4.0 (некоммерческая); базовая модель lessac — исследовательская | старший бригады, заявители |
| Голос `ru_RU-irina-medium` (piper) | данные RHVoice — лицензия не указана; базовая модель lessac — исследовательская | заявители (женский голос) |
| Модель распознавания `vosk-model-small-ru-0.22` (Vosk) | Apache 2.0 (каталог моделей Vosk) | распознавание вопросов оператора 112 |
| Браузеры Chrome, Firefox, Яндекс.Браузер | лицензии производителей | рабочие места; в поставку не входят |

**Голоса.** Все русские голоса piper дообучены с английской модели lessac, данные которой выданы по
исследовательской лицензии (Research Licence Agreement, Blizzard Challenge 2013); у голоса ruslan данные
ещё и некоммерческие (CC BY-NC-SA 4.0). Для демонстрации на хакатоне это допустимо; для эксплуатации у
заказчика голоса нужно заменить моделью с подходящей лицензией — например, обучить piper с нуля на данных
CC0 (датасеты dmitri и denis) или использовать лицензированный синтез. Голоса лежат отдельно (`data/voices`),
замена не требует изменений в коде; без голосов тренажёр работает, реплики показываются текстом.

**GPL.** piper-tts 1.8 и espeak-ng распространяются по GPL-3.0. Сервер тренажёра их не импортирует:
озвучка — отдельная программа (`python -m speech.render`), сервер читает только готовые WAV-файлы.
""" % "\n".join(rows)
    out = ROOT / "docs" / "LIBRARIES.md"
    out.write_text(text, encoding="utf-8")
    print("%s: пакетов %d" % (out, len(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
