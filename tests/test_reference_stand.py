"""Эталонная маршрутизация против кадров боевого стенда (ARCHITECTURE.md §10.8)."""
import json

import routing_reference
from arm112.paths import data_dir


def test_reference_matches_stand(classifier):
    with open(data_dir() / "fixtures" / "stand_routing.json", encoding="utf-8") as f:
        fixture = json.load(f)
    verdicts = [v for _, v, _ in routing_reference.check(classifier, fixture)]
    assert verdicts.count("fail") == 0
    # Если эталон начнёт объяснять «Деп. ЖКХ» — это надо увидеть и обновить фикстуру осознанно.
    assert verdicts.count("exact") == 14
    assert verdicts.count("known") == 2


def test_classifier_covers_all_groups(classifier):
    groups = {i["group"] for i in classifier.incidents.values()}
    assert groups == {str(g) for g in range(1, 25)}  # 24 группы, 24-я — БПЛА
    assert len(classifier.incidents) == 1283
