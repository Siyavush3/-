"""Режим «карточки» (узел 2): пакет вызовов, заявитель-бот, оценка карточки 112, ход занятия."""
import json

import pytest

from arm112.paths import data_dir
from arm112.reference.fsm import ResponseFsm
from arm112.reference.territory import Territory
from domain.calls import (Caller, CallPack, CardTiming, card_score, detect_intent, evaluate_card, group_flags,
                          load_rubric112, questionnaire)
from domain.engine import Engine
from domain.evaluation import Rubric
from domain.lesson import Actor, ActorKind
from domain.scenario import CardViews, Router

TEACHER = Actor("u1", ActorKind.TEACHER)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture(scope="module")
def pack(classifier):
    p = CallPack.load(data_dir() / "scenarios" / "calls_bilety.json")
    p.validate(classifier)
    return p


@pytest.fixture(scope="module")
def rubric():
    return load_rubric112()


@pytest.fixture(scope="module")
def router(classifier):
    with open(data_dir() / "classifier" / "ui_services.json", encoding="utf-8") as f:
        return Router(classifier, Territory.load(), json.load(f))


def perfect(call: dict, number: str = "1") -> dict:
    """Карточка, заполненная ровно по эталону."""
    g = call["gold"]
    address = {k: v for k, v in g["address"].items() if k != "kw"}
    if g["address"].get("kw"):
        address["descriptive"] = " ".join(g["address"]["kw"])
    if address.get("subject") is None:
        address["subject"] = "Москва"
    return {"number": number, "code": g["codes"][0], "flags": [f for f in g["flags"] if f != "ПП"],
            "victims": True if g["victims"] is None else g["victims"],
            "caller": {"name": g["name"] or call["caller"]["name"], "status": (g["statuses"] or ["очевидец"])[0]},
            "address": address, "description": ". ".join(f[0] for f in g["facts"]) + "."}


def evaluate(call, card, classifier, router, rubric, answered=5.0, saved=60.0, spelling=None):
    flags = list(card.get("flags") or []) + (["ПП"] if card.get("victims") else [])
    auto = [p.name for p in router.plaques(card["code"], flags, None) if p.row == 1] if card.get("code") else []
    return evaluate_card(call, card, CardTiming(0.0, answered, saved), classifier, rubric, auto, 80, 15, spelling)


def cats(findings):
    return sorted(f.category for f in findings)


# ---------- пакет и заявитель ----------
def test_pack_covers_tickets(pack, classifier):
    assert len(pack.calls) == 94                           # 96 вызовов билетов, два повтора объединены
    groups = pack.groups(classifier)
    assert {"1", "2", "15", "17", "22"} <= set(groups)     # пожары, ДТП, правопорядок, человек в опасности, 103
    assert all(c["dialogue"]["opening"].startswith("Алло") for c in pack.calls)


@pytest.mark.parametrize("text,intent", [
    ("Назовите адрес", "address"), ("Уточните адрес, пожалуйста", "address_clarified"),
    ("Как вас зовут?", "name"), ("Есть пострадавшие?", "victims"), ("Кем вы приходитесь?", "status"),
    ("Ваш номер телефона", "phone"), ("Дом газифицирован?", "gas"), ("Расскажите подробнее", "details"),
    ("абракадабра", None)])
def test_detect_intent(text, intent):
    assert detect_intent(text) == intent


def test_caller_clarifies_address_on_second_question(pack):
    call = pack.by_key["b01-2"]
    caller = Caller(call)
    first, second = caller.answer("address"), caller.answer("address")
    assert "посольств" in first and second == call["dialogue"]["address_clarified"]
    assert caller.answer(None) == Caller.UNKNOWN


def test_questionnaire_from_classifier(classifier):
    q = questionnaire(classifier, "1", "Происшествие 101").to_dict()
    assert any(i["code"] == "1050602" and i["attr2"] == "мусоропровод" for i in q["incidents"])
    assert {"УЛ", "ГАЗ"} <= {x["flag"] for x in q["questions"]}   # пожару важны угроза людям и газификация
    assert "ПП" in group_flags(classifier, "1")


# ---------- оценка карточки ----------
@pytest.mark.parametrize("key", ["b01-1", "b01-2", "b02-1", "b05-2", "b06-1", "b14-2", "b30-3", "b01-3"])
def test_perfect_card_has_no_findings(pack, classifier, router, rubric, key):
    findings = evaluate(pack.by_key[key], perfect(pack.by_key[key]), classifier, router, rubric)
    assert findings == [], [f.detail for f in findings]
    assert card_score(findings, rubric) == 100


def test_type_errors(pack, classifier, router, rubric):
    call = pack.by_key["b02-1"]                           # задымление мусоропровода
    card = dict(perfect(call), code="1050101")            # та же группа, но «пожар: квартира»
    assert cats(evaluate(call, card, classifier, router, rubric)) == [2]
    card = dict(perfect(call), code="2010000")            # ДТП — другая группа
    assert cats(evaluate(call, card, classifier, router, rubric)) == [1]


def test_flags_and_victims(pack, classifier, router, rubric):
    call = pack.by_key["b05-1"]                           # люди на балконе кричат — угроза людям
    card = dict(perfect(call), flags=[])
    assert cats(evaluate(call, card, classifier, router, rubric)) == [3]
    call = pack.by_key["b06-1"]                           # пострадавших нет
    assert cats(evaluate(call, dict(perfect(call), victims=True), classifier, router, rubric)) == [3]


def test_address_errors(pack, classifier, router, rubric):
    call = pack.by_key["b02-1"]                           # ул. Берзарина, 21, корп. 1, под. 3, домофон 68
    good = perfect(call)
    wrong_house = dict(good, address=dict(good["address"], house="12"))
    assert cats(evaluate(call, wrong_house, classifier, router, rubric)) == [4]
    no_details = dict(good, address={"subject": "Москва", "street": "Берзарина", "house": "21"})
    findings = evaluate(call, no_details, classifier, router, rubric)
    assert cats(findings) == [5] and "корпус" in findings[0].detail and "подъезд" in findings[0].detail
    call = pack.by_key["b01-3"]                           # Волгоградская область — не Москва
    wrong_region = dict(perfect(call), address=dict(perfect(call)["address"], subject="Москва"))
    assert cats(evaluate(call, wrong_region, classifier, router, rubric)) == [4]


def test_mandatory_fields(pack, classifier, router, rubric):
    call = pack.by_key["b01-1"]
    card = dict(perfect(call), caller={"name": "", "status": ""}, victims=None)
    findings = evaluate(call, card, classifier, router, rubric)
    assert cats(findings) == [6] and all(w in findings[0].detail for w in ("ФИО", "статус", "пострадавшие"))
    card = dict(perfect(call), caller={"name": "Петров Пётр", "status": "очевидец"})
    assert cats(evaluate(call, card, classifier, router, rubric)) == [6]   # назвался Сидоровым


def test_description_facts_and_first_100_chars(pack, classifier, router, rubric):
    call = pack.by_key["b01-2"]                           # массовая драка: 103 в списке служб
    card = dict(perfect(call), description="Дерутся на тротуаре у посольства.")
    findings = evaluate(call, card, classifier, router, rubric)
    assert cats(findings) == [7] and "палк" in findings[0].detail
    tail = "Очевидец сообщает, что у посольства на тротуаре собралась большая группа людей, шумят и ругаются. " \
           "Потом все дерутся палками, есть пострадавшие с травмами."
    findings = evaluate(call, dict(perfect(call), description=tail), classifier, router, rubric)
    assert cats(findings) == [7] and "первых 100 символах" in findings[0].detail


def test_timing_and_services(pack, classifier, router, rubric):
    call = pack.by_key["b01-1"]
    findings = evaluate(call, perfect(call), classifier, router, rubric, answered=20.0, saved=140.0)
    assert cats(findings) == [9, 11]                      # принят через 20 с, заполнен за 120 с
    card = dict(perfect(call), services_added=["ЦОДД"])
    assert cats(evaluate(call, card, classifier, router, rubric)) == [10]


def test_spelling_findings_are_capped(pack, classifier, router, rubric):
    call = pack.by_key["b01-1"]
    errors = [{"word": "гарит%d" % i, "suggestions": ["горит"]} for i in range(5)]
    findings = evaluate(call, perfect(call), classifier, router, rubric, spelling=errors)
    assert cats(findings) == [8] * 5
    assert card_score(findings, rubric) == 100 - 3 * 5    # не больше трёх штрафов за орфографию


# ---------- ход занятия ----------
def test_cards_lesson_flow(pack, classifier, router):
    clock = Clock()
    engine = Engine(router, CardViews.load(classifier), ResponseFsm.load(), Rubric.load(), {pack.id: pack},
                    clock=clock, wall=lambda: 1_758_000_000.0)
    rt = engine.create_lesson(TEACHER, pack.id, config={"count": 2})
    L = rt.lesson.id
    engine.join(L, "u2", "Иванов")
    engine.assign_role(TEACHER, L, "u2", "любая строка — роль всегда оператор 112")
    engine.start(TEACHER, L)
    assert rt.desk and len(rt.desk.keys) == 2

    clock.now += 3
    outs = engine.tick()
    incoming = [m for to, m in outs if to == "u2" and m["t"] == "incoming"]
    assert len(incoming) == 1
    number = incoming[0]["call"]["number"]
    call = pack.by_key[rt.desk.by_number[number]]

    clock.now += 4                                        # принят через 4 с
    say = engine.handle(L, "u2", {"t": "answer"})
    assert say[0][1]["line"]["text"] == call["dialogue"]["opening"]
    ask = engine.handle(L, "u2", {"t": "ask", "text": "Назовите точный адрес"})
    assert ask[0][1]["line"]["text"] == call["dialogue"]["address_clarified"]

    clock.now += 40
    outs = engine.handle(L, "u2", {"t": "save_card", "card": perfect(call, number), "seq": 1})
    result = next(m for to, m in outs if m["t"] == "card_result")["result"]
    assert result["score"] == 100 and result["fill_s"] == 40 and result["gold"]["code"] == call["gold"]["codes"][0]
    assert engine.handle(L, "u2", {"t": "save_card", "card": perfect(call, number), "seq": 1})[0][1]["duplicate"]

    clock.now += 5
    second = next(m for to, m in engine.tick() if m["t"] == "incoming")    # следующий звонок через 5 с
    assert second["call"]["number"] != number
    snap = engine.snapshot(L, "u2")
    assert snap["desk"]["current"]["number"] == second["call"]["number"] and len(snap["desk"]["done"]) == 1
    row = engine.monitor(L)["participants"][0]
    assert row["calls_done"] == 1 and row["call_state"] == "звонок"

    outs = engine.finish(TEACHER, L)                      # второй вызов так и не принят
    res = next(m for to, m in outs if m["t"] == "result")["result"]
    assert res["cards"] == 2 and res["score"] == 50
    assert [f["category"] for f in res["findings"]] == [12]
    assert res["timing"]["avg_fill_s"] == 40 and len(res["categories"]) == 12


def test_groups_filter_limits_calls(pack, classifier, router):
    engine = Engine(router, CardViews.load(classifier), ResponseFsm.load(), Rubric.load(), {pack.id: pack},
                    clock=Clock(), wall=lambda: 1_758_000_000.0)
    rt = engine.create_lesson(TEACHER, pack.id, config={"count": 50, "groups": ["2"]})
    engine.join(rt.lesson.id, "u2", "Иванов")
    engine.assign_role(TEACHER, rt.lesson.id, "u2", "")
    engine.start(TEACHER, rt.lesson.id)
    codes = [pack.by_key[k]["gold"]["codes"][0] for k in rt.desk.keys]
    assert codes and all(classifier.incidents[c]["group"] == "2" for c in codes)   # только ДТП
