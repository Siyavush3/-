"""Модель распознавания речи Vosk для тренажёра — загрузка на машине сборки (с интернетом) в data/asr.

    python tools/fetch_asr.py [--dest data/asr] [--check]

В изолированный контур модель приезжает в дистрибутиве; там эта команда с --check только сверяет
контрольную сумму архива. Источник — каталог моделей Vosk (alphacephei.com/vosk/models), лицензия
малой русской модели — Apache 2.0.
"""
import argparse
import hashlib
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL = "vosk-model-small-ru-0.22"
URL = "https://alphacephei.com/vosk/models/%s.zip" % MODEL
SHA256 = "961d5ff98a17f4aa6de69864d0aa71fa5bac682301d2b5d17a3f24c5c99a46d4"   # архив, с которым тренажёр испытан


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default=str(ROOT / "data" / "asr"))
    ap.add_argument("--check", action="store_true", help="только проверить уже лежащую модель")
    args = ap.parse_args()
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest / (MODEL + ".zip")
    if not args.check and not (archive.is_file() and (SHA256 is None or sha256(archive) == SHA256)):
        print("загрузка", URL, "…", flush=True)
        urllib.request.urlretrieve(URL, str(archive))
    if not archive.is_file():
        print("%s: НЕТ архива" % MODEL)
        return 1
    digest = sha256(archive)
    if SHA256 and digest != SHA256:
        print("%s: архив повреждён (SHA-256 %s)" % (MODEL, digest))
        return 1
    target = dest / MODEL
    if not (target / "am" / "final.mdl").is_file():
        shutil.rmtree(target, ignore_errors=True)
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
    good = (target / "am" / "final.mdl").is_file()
    print("%s: %s, SHA-256 архива %s" % (MODEL, "в порядке" if good else "НЕ РАСПАКОВАНА", digest))
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
