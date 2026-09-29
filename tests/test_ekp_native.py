"""C-ядро (ekp) против эталона на Python: результат обязан совпадать на всех входах."""
import itertools
import json
import random
import shutil
from concurrent.futures import ThreadPoolExecutor

import pytest

from arm112.ekp import Ekp, EkpError
from arm112.paths import data_dir

MAX_EXHAUSTIVE = 10  # до 2^10 комбинаций на код — полный перебор


def relevant_flags(incident):
    """Флаги, от которых маршрут этого кода вообще может зависеть."""
    names = set()
    for entry in incident["routing"]:
        cond = entry.get("cond") or {}
        names.update(cond.get("all_of", ()), cond.get("none_of", ()))
    return sorted(names)


def flag_sets(incident, all_flags, rng):
    names = relevant_flags(incident)
    if len(names) <= MAX_EXHAUSTIVE:
        for n in range(len(names) + 1):
            yield from itertools.combinations(names, n)
    else:
        for _ in range(1 << MAX_EXHAUSTIVE):
            yield tuple(f for f in names if rng.random() < 0.5)
    for _ in range(4):  # и немного случайных наборов из всех флагов
        yield tuple(f for f in all_flags if rng.random() < 0.3)


def test_tables_describe_same_classifier(ekp, classifier):
    assert ekp.flags == classifier.flags
    assert ekp.source_version == classifier.version
    assert sorted(ekp.codes(), key=int) == sorted(classifier.incidents, key=int)


def test_every_code_every_relevant_flag_set(ekp, classifier):
    rng = random.Random(112)
    compared = 0
    for code, incident in classifier.incidents.items():
        for flags in flag_sets(incident, classifier.flags, rng):
            assert ekp.route(code, flags) == classifier.route(code, flags), (code, flags)
            compared += 1
    assert compared > 10_000


def test_stand_cases(ekp, classifier):
    with open(data_dir() / "fixtures" / "stand_routing.json", encoding="utf-8") as f:
        cases = [c for c in json.load(f)["cases"] if c["code"]]
    for case in cases:
        assert ekp.route(case["code"], case["flags"]) == classifier.route(case["code"], case["flags"])


def test_unknown_code_and_flag(ekp):
    with pytest.raises(KeyError):
        ekp.route("1")
    with pytest.raises(ValueError):
        ekp.route("1010101", ["НЕТ_ТАКОГО"])


def test_cyrillic_path(native, ekp_table, tmp_path):
    target = tmp_path / "кириллица" / "таблица.ekp"
    target.parent.mkdir()
    shutil.copy(ekp_table, target)
    with Ekp(target) as table:
        assert table.route("1010101", ["НД"]).services == {"Служба 101", "ЦОДД", "ОАТИ"}


def test_corrupted_table_is_rejected(native, ekp_table, tmp_path):
    data = bytearray(ekp_table.read_bytes())
    data[len(data) // 2] ^= 0x5A
    bad = tmp_path / "bad.ekp"
    bad.write_bytes(bytes(data))
    with pytest.raises(EkpError) as err:
        Ekp(bad)
    assert err.value.code == -3  # A112_ERR_FORMAT


def test_parallel_calls_agree(ekp, classifier):
    codes = list(classifier.incidents)[:200]
    expected = [classifier.route(c, ["УЛ", "НД"]) for c in codes]
    with ThreadPoolExecutor(max_workers=8) as pool:
        got = list(pool.map(lambda c: ekp.route(c, ["УЛ", "НД"]), codes))
    assert got == expected
