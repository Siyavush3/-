"""Автомат статусов реагирования: живой трек со стенда и правила памятки."""
import json

import pytest

from arm112.paths import data_dir
from arm112.reference.fsm import ResponseFsm, Step


def seconds(hhmmss):
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return h * 3600 + m * 60 + s


@pytest.fixture(scope="module")
def fsm():
    return ResponseFsm.load()


def test_stand_track(fsm):
    """Трек со стенда проходит автомат; единственное нарушение — норматив 30 с (9 мин 17 с)."""
    with open(data_dir() / "fixtures" / "status_track.json", encoding="utf-8") as f:
        fixture = json.load(f)
    track = [Step(s["status"], seconds(s["time"]), s.get("comment", "")) for s in fixture["track"]]
    violations = fsm.check(track)
    assert [(v.kind, v.status) for v in violations] == [
        (e["kind"], e["status"]) for e in fixture["expected_violations"]]
    assert violations[0].detail.startswith("557 с")


def test_dropdown_matches_stand_frame(fsm):
    # кадр sdds_14: после «Начало реагирования» — ровно эти три пункта
    assert fsm.allowed_next("Начало реагирования") == ["Прибытие", "Отказ от выполнения работ", "Работы завершены"]
    assert fsm.allowed_next(None) == ["Принята", "Не принята"]
    assert fsm.allowed_next("Получена службой") == ["Принята", "Не принята"]
    assert fsm.allowed_next("Не принята") == ["Принята"]
    assert fsm.allowed_next("Работы завершены") == []


def test_refusal_without_comment(fsm):
    violations = fsm.check([Step("Добавлена", 0), Step("Не принята", 10, "  ")])
    assert [(v.kind, v.status) for v in violations] == [("комментарий", "Не принята")]


def test_skipping_arrival_is_not_allowed(fsm):
    track = [Step("Добавлена", 0), Step("Принята", 5), Step("Начало реагирования", 60),
             Step("Проведение работ", 120)]
    assert [v.kind for v in fsm.check(track)] == ["переход"]


def test_nothing_after_terminal_status(fsm):
    track = [Step("Добавлена", 0), Step("Принята", 5), Step("Работы завершены", 60), Step("Прибытие", 70)]
    assert [v.kind for v in fsm.check(track)] == ["после завершения"]


def test_no_status_at_all(fsm):
    assert [v.kind for v in fsm.check([Step("Добавлена", 0), Step("Получена службой", 40)])] == ["нет статуса"]
