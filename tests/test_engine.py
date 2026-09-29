"""Движок занятия: целое занятие на поддельных часах — от старта до оценки."""
import json

import pytest

from arm112.paths import data_dir
from arm112.reference.fsm import ResponseFsm
from arm112.reference.territory import Territory
from domain.engine import Engine
from domain.evaluation import Rubric
from domain.lesson import Actor, ActorKind, InvalidRole, State
from domain.scenario import CardViews, Pack, Router

TEACHER = Actor("t1", ActorKind.TEACHER)
ROLE = "Поселение Щукино"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def world(classifier):
    with open(data_dir() / "classifier" / "ui_services.json", encoding="utf-8") as f:
        router = Router(classifier, Territory.load(), json.load(f))
    pack = Pack.load(data_dir() / "scenarios" / "dds_shchukino_v0.json")
    clock = Clock()
    engine = Engine(router, CardViews.load(classifier), ResponseFsm.load(), Rubric.load(),
                    {pack.id: pack}, clock=clock, wall=lambda: 1_758_000_000.0)
    rt = engine.create_lesson(TEACHER, pack.id)
    for pid in ("s1", "s2"):
        engine.join(rt.lesson.id, pid, pid.upper())
        engine.assign_role(TEACHER, rt.lesson.id, pid, ROLE)
    engine.start(TEACHER, rt.lesson.id)
    return engine, rt, clock


def at(engine, clock, t):
    """Перевести часы на t секунд от старта и обработать всё, что созрело."""
    clock.now = 1000.0 + t
    return engine.tick()


def msgs(outs, target, kind):
    return [m for (to, m) in outs if to == target and m["t"] == kind]


def status(engine, rt, pid, card_key, status_, comment="", seq=0):
    number = rt.numbers[card_key]
    return engine.handle(rt.lesson.id, pid, {"t": "set_status", "card": number, "status": status_,
                                             "comment": comment, "seq": seq})


def test_full_lesson(world):
    engine, rt, clock = world
    L = rt.lesson.id

    outs = at(engine, clock, 10)                         # карточка I («пожар: квартира»)
    assert len(msgs(outs, "s1", "card")) == 1 and len(msgs(outs, "s2", "card")) == 1
    card = msgs(outs, "s1", "card")[0]["card"]
    assert card["own_service"] == ROLE and card["allowed"] == ["Принята", "Не принята"]
    assert "Служба 101" in [p["name"] for p in card["plaques"]]

    engine.handle(L, "s1", {"t": "open_card", "card": rt.numbers["I"]})
    status(engine, rt, "s1", "I", "Принята", "направлены сантехник и электрик", seq=1)
    assert msgs(status(engine, rt, "s1", "I", "Принята", "", seq=1), "s1", "status_ack")[0]["duplicate"]

    engine.disconnect(L, "s2")                           # s2 пропадает с 10-й по 100-ю секунду

    outs = at(engine, clock, 56)                         # доклад: выехали (15 + 40 с)
    assert msgs(outs, "s1", "call")[0]["text"].startswith("Направлены")
    status(engine, rt, "s1", "I", "Начало реагирования", "выехали сантехник и электрик", seq=2)
    rejected = status(engine, rt, "s1", "I", "Проведение работ", "x", seq=3)
    assert msgs(rejected, "s1", "status_rejected")      # после «Начало» — только «Прибытие» и завершающие

    at(engine, clock, 100)
    engine.connect(L, "s2")
    at(engine, clock, 130)                               # карточки A и C
    status(engine, rt, "s1", "C", "Не принята", "Дом УК «ПИК», информация передана в их диспетчерскую", seq=4)

    outs = engine.finish(TEACHER, L)
    assert rt.lesson.state is State.REPORTED
    results = {r["participant"]: r for r in msgs(outs, "teacher:t1", "results")[0]["results"]["participants"]}
    s1 = sorted((f["category"], f["card"]) for f in results["s1"]["findings"])
    # s1: по I не отмечено «Прибытие» после доклада (№6), по A нет статуса (№1); C — без замечаний
    assert s1 == sorted([(6, rt.numbers["I"]), (1, rt.numbers["A"])])
    assert results["s1"]["score"] == 100 - 5 - 10
    # s2: ни одного статуса по трём карточкам (№1 ×3) и 90 с без связи (№7)
    assert sorted(f["category"] for f in results["s2"]["findings"]) == [1, 1, 1, 7]


def test_monitor_counts_overdue(world):
    engine, rt, clock = world
    at(engine, clock, 10)
    at(engine, clock, 45)                                # 35 с без первичного статуса
    row = {r["participant"]: r for r in engine.monitor(rt.lesson.id)["participants"]}
    assert row["s1"]["overdue"] == 1 and row["s1"]["waiting"] == 1


def test_role_must_match_pack(world, classifier):
    engine, rt, clock = world
    fresh = engine.create_lesson(TEACHER, rt.pack.id)
    engine.join(fresh.lesson.id, "s9", "S9")
    with pytest.raises(InvalidRole):
        engine.assign_role(TEACHER, fresh.lesson.id, "s9", "Мослифт")


def test_brigade_stops_after_terminal_status(world):
    engine, rt, clock = world
    at(engine, clock, 310)                               # к этому времени пришла карточка D (провод)
    status(engine, rt, "s1", "D", "Принята", "направлен электрик", seq=1)
    status(engine, rt, "s1", "D", "Работы завершены", "всё", seq=2)   # закрыл раньше бригады
    outs = at(engine, clock, 500)
    assert not [m for m in msgs(outs, "s1", "call") if m["card"] == rt.numbers["D"]]
