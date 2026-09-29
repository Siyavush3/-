#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сборка таблицы маршрутизации ядра: classifier.json + ui_services.json → classifier.ekp.

Вызывается из CMake. Совместим с Python 3.8 (сборка в контейнере Ubuntu 20.04).
Сам компилятор — arm112.ekp_compiler.

Запуск:  python tools/compile_ekp.py <classifier.json> <ui_services.json> <out.ekp>
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pycore"))

from arm112.ekp_compiler import build  # noqa: E402  (реэкспорт — им пользуются тесты)


def main(argv):
    if len(argv) != 4:
        sys.stderr.write(__doc__)
        return 2
    with open(argv[1], encoding="utf-8") as f:
        classifier = json.load(f)
    with open(argv[2], encoding="utf-8") as f:
        ui = json.load(f)
    blob = build(classifier, ui)
    with open(argv[3], "wb") as f:
        f.write(blob)
    print("classifier.ekp: %d bytes" % len(blob))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
