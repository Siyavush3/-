# -*- coding: utf-8 -*-
"""Сверка эталонной маршрутизации с кадрами боевого стенда.

Эталон — pycore/arm112/reference/routing.py, фикстура — data/fixtures/stand_routing.json.
Территориальные службы в фикстуре не сравниваются: адрес на кадрах — только «Москва».

Запуск:  python tools/routing_reference.py      (код возврата 1 — есть ошибки)
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pycore"))

from arm112.paths import data_dir  # noqa: E402
from arm112.reference.routing import Classifier  # noqa: E402


def check(classifier, fixture):
    """Возвращает список (кейс, вердикт, получено), вердикт: exact | known | fail."""
    out = []
    for case in fixture["cases"]:
        expected = set(case["expected"])
        got = set(classifier.route(case["code"], case["flags"]).services) if case["code"] else set()
        note = case.get("known_discrepancy")
        allowed = set(note["on_stand_not_derived"]) if note else set()
        if got == expected:
            verdict = "exact"
        elif got == expected - allowed:
            verdict = "known"
        else:
            verdict = "fail"
        out.append((case, verdict, got))
    return out


def main():
    classifier = Classifier.load()
    with open(data_dir() / "fixtures" / "stand_routing.json", encoding="utf-8") as f:
        fixture = json.load(f)
    results = check(classifier, fixture)
    marks = {"exact": "OK  ", "known": "KNWN", "fail": "FAIL"}
    for case, verdict, got in results:
        print("%s кадр %02d  код %-8s флаги %s" % (marks[verdict], case["frame"], case["code"] or "—",
                                                   ",".join(case["flags"]) or "—"))
        if verdict == "known":
            note = case["known_discrepancy"]
            print("       на стенде, но не выводится: %s — %s" % (sorted(note["on_stand_not_derived"]), note["why"]))
        elif verdict == "fail":
            expected = set(case["expected"])
            print("       лишние: %s" % sorted(got - expected))
            print("       нет:    %s" % sorted(expected - got))
    count = {v: sum(1 for _, x, _ in results if x == v) for v in marks}
    print("\nточное совпадение со стендом: %d из %d; известных расхождений: %d; ошибок: %d"
          % (count["exact"], len(results), count["known"], count["fail"]))
    return 1 if count["fail"] else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
