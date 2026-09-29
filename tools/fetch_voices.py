"""Голоса piper для озвучки тренажёра — загрузка на машине сборки (с интернетом) в data/voices.

    python tools/fetch_voices.py [--dest data/voices] [--check]

В изолированный контур голоса приезжают в дистрибутиве; там эта команда с --check только сверяет
контрольные суммы уже лежащих файлов. Источник — репозиторий голосов piper (rhasspy/piper-voices).
Лицензии голосов (лицензии обучающих данных) — в MODEL_CARD каждого голоса, файл сохраняется рядом.
"""
import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/%s/medium/"
VOICES = {  # голос: SHA-256 модели (.onnx), проверено на файлах, с которыми тренажёр испытан
    "dmitri": "f073356ebc4bd0f80c5af58df2953a5988bd5bdab1eb38635ce960b071fbefcb",
    "irina": "8ff38212d23da300bbe3705c645e6e5b9475f0bfde01558eb17813e22acaaaaa",
    "ruslan": "72a5f88e0b20928064eb45d88e1daa21f8af62d18613580d32cbb4aed48dcf7f",
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default=str(ROOT / "data" / "voices"))
    ap.add_argument("--check", action="store_true", help="только проверить уже лежащие файлы")
    args = ap.parse_args()
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    ok = True
    for voice, digest in VOICES.items():
        name = "ru_RU-%s-medium" % voice
        model = dest / (name + ".onnx")
        if not args.check:
            for fname in (name + ".onnx", name + ".onnx.json", "MODEL_CARD"):
                target = dest / (name + ".MODEL_CARD.md" if fname == "MODEL_CARD" else fname)
                if target.is_file() and (fname != name + ".onnx" or sha256(target) == digest):
                    continue
                print("загрузка", fname, "…", flush=True)
                urllib.request.urlretrieve(BASE % voice + fname, str(target))
        good = model.is_file() and sha256(model) == digest and (dest / (name + ".onnx.json")).is_file()
        ok &= good
        print("%-22s %s" % (name, "в порядке" if good else "НЕТ или повреждён"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
